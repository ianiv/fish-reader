"""Fish Audio Drama 3 generation core (copied from fish-ui/backend/fish_core.py at fish-ui 619a307).

fish-reader runs Drama 3 itself (server/engine.py) so only one server is needed. Keep it in step with fish-ui
by hand when either changes; the only local edits are this paragraph and the FAKE switches below.

Pure functions plus one thin wrapper over the model: the model table, sampling presets, request validation
(build_items), render() (optionally streaming) and continuation() (previous takes as history). Nothing here
touches HTTP; engine.py builds on it.

Drama 3 facts this module relies on (mlx_audio 0.6.7, mlx_audio.tts.models.fish_drama3, verified by the model
smoke test in fish-ui):
  - generate(text, ref_audio, ref_text, max_tokens, temperature, top_k, top_p, stream, streaming_interval,
    first_chunk_frames, speed=1.0, instruct=None, **kwargs). speed != 1 and any instruct raise ValueError;
    unknown kwargs are silently ignored (so language etc. never reach it).
  - It chunks text itself (~300 UTF-8 bytes, <= 3 speakers per chunk, chunking.split_text); each chunk sees the
    first and the previous chunk as history. We never pre-split; segment_idx in results counts chunks.
  - ref_audio: one path, or a list (one per voiced speaker) with ref_text tagged "<|speaker:N|> transcript".
    It keeps an LRU of 4 encoded voices, so there's no reference-code cache here.
  - stream=True yields every streaming_interval seconds; first_chunk_frames=4 gives first audio after ~0.16 s.
  - max_tokens is frames per chunk (default 1024) and the model stops at EOS: leave it None.
  - Main-token top_k caps make takes without a reference fall silent: the preset keeps top_k off (None).
  - Continuation: there's no previous_takes argument. continuation() prepends (text, codes) turns to the
    history that build_frames() gets; this is the only use of private model API (pinned to mlx-audio 0.6.7).
  - mx.random.seed(n) gives bit-identical output.

FISH_READER_FAKE_MODEL=1 (or FISH_UI_FAKE_MODEL=1) swaps the model for FakeModel: the same generate() interface and errors, one tone per
chunk (chunks from a mirror of chunking.split_text), streamed in ~0.32 s pieces, never imports mlx.
"""
from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

FAKE = (os.environ.get("FISH_READER_FAKE_MODEL") or os.environ.get("FISH_UI_FAKE_MODEL") or "") not in ("", "0", "false", "no")

MODEL_KEY = "drama3"
MODELS = {MODEL_KEY: "mlx-community/fish-audio-drama-3-4bit"}
LEGACY_MODELS = {"s2pro": "Fish Audio S2 Pro"}  # old takes keep this key; replaying them maps to MODEL_KEY
SAMPLE_RATE = 44100
FRAMES_PER_SECOND = 21.53  # codec frames per second of audio
FRAMES_PER_BYTE = 1.5  # measured: ~1.5 frames per UTF-8 byte of text (for the max_seconds warning)
PAUSE_SECONDS = 1.0  # extra expected seconds per [pause] (ETA only)

MAX_SPEAKERS = 16  # <|speaker:0|> .. <|speaker:15|>
MAX_JOB_CHARS = 3000  # per job (all lines / the whole script, tags excluded for dialogue)
MAX_PREVIOUS_TAKES = 4
MAX_HISTORY_S = 90.0  # total audio of previous takes per job
DEFAULT_MAX_TOKENS = 1024  # the model's per-chunk frame limit (the UI's "max seconds" default)
CHUNK_BYTES = 300  # the model's chunking (mirrored for the fake model and segment counts)
CHUNK_SPEAKERS = 3
STREAM_INTERVAL = 0.32  # seconds per streamed piece
FIRST_CHUNK_FRAMES = 4  # smaller first piece -> first audio after ~0.16 s
NORMALIZE_BELOW = 0.5  # saved takes with a peak below this are scaled to NORMALIZE_TO (gain in the sidecar)
NORMALIZE_TO = 0.9

MODEL_INFO = {
    MODEL_KEY: dict(
        label="Fish Audio Drama 3", short="Expressive TTS, cloning, up to 16 speakers, live streaming",
        sample_rate=SAMPLE_RATE,
        peak_gb=29.0, peak_stream_gb=25.5, resident_gb=24.4,  # measured 28.6-28.8 / 25.3 / 24.4 GB
        rtf=0.4,  # running estimate seed for ETAs (wall seconds per audio second); the server refines it
        streaming=True, cloning=True, needs_voice=False, inline_tags=True, multi_speaker=True,
        max_speakers=MAX_SPEAKERS, workflows=["tts", "dialogue"],
    ),
}
# Which models each workflow may use (first is the default). Kept as a table so the API stays model-agnostic.
WORKFLOW_MODELS = {"tts": [MODEL_KEY], "dialogue": [MODEL_KEY]}

# Sampling presets (D7: only "balanced" = the model's defaults until others are verified). top_k None = off.
SAMPLING_PRESETS = {"balanced": dict(temperature=1.0, top_p=0.9, top_k=None)}
DEFAULT_PRESET = "balanced"
LEGACY_PRESETS = ("stable", "expressive")  # S2 Pro presets: replayed recipes fall back to balanced + warning

# Language hint (D1): never sent to the model (it infers language from the text). It drives Whisper's language,
# the UI's sample text and is recorded in the sidecar.
LANGUAGES = ["zh", "en", "ja", "ko", "es", "pt", "ar", "ru", "fr", "de", "sv", "it", "tr", "no", "nl", "cy",
             "eu", "ca"]
# Inline delivery cues offered in the UI (D3; "[cue]" in the text). Others may work too.
CUES = ["sighs", "laughs", "whispers"]
LEGACY_PARAMS = ("instruct", "speed", "chunk_length")  # S2 Pro params: ignored with a job warning

WARMUP_TEXT = "Hello there."
WARMUP_MAX_TOKENS = 16

SPEAKER_TAG = re.compile(r"<\|speaker:([0-9]+)\|>")
LEGACY_SPEAKER = re.compile(r"\[S(\d+)\]")
STRUCTURAL_TOKENS = ("<|im_start|>", "<|im_end|>", "<|voice|>", "<|text|>", "<|interleave|>", "<|semantic:")


class RequestError(ValueError):
    """A request that can't be turned into generate() calls (shown to the user as-is)."""


class Cancelled(Exception):
    pass


@dataclass
class Voice:
    """A resolved library voice: reference audio path + transcript."""
    id: str
    audio: str
    transcript: str | None = None


@dataclass
class PrevTake:
    """A previous take to continue from (history turn): its text as generate() got it, and its WAV."""
    id: str  # "<workflow>/<stem>"
    text: str
    wav: str
    duration_s: float
    voices: list = field(default_factory=list)  # voice ids it used (for the "voices differ" warning)
    gain: float | None = None  # the sidecar's normalization gain: the WAV is divided by it before encoding


@dataclass
class Item:
    """One generate() call (repeated once per variation)."""
    label: str  # file name slug and history label
    kwargs: dict  # generate() kwargs (ref_audio = reference path(s); dropped from the sidecar)
    refs: list = field(default_factory=list)  # [(speaker, Voice)] in reference order
    history: list = field(default_factory=list)  # [PrevTake] oldest first
    segments: int = 1  # text chunks generate() will make (count_chunks), for per-segment progress
    seconds_hint: float | None = None  # expected audio length, for ETA
    warnings: list = field(default_factory=list)  # non-fatal request issues, shown to the user
    language: str = "auto"  # the effective hint ("auto" or a LANGUAGES code); never sent to the model
    language_mode: str = "auto"  # the picker's raw value ("auto" or a code)
    speakers: dict = field(default_factory=dict)  # {speaker: voice id | None} for every speaker in the text

    @property
    def voices(self) -> list:
        return [v for _, v in self.refs]


@dataclass
class Take:
    audio: "object"  # numpy float32, mono
    sample_rate: int
    wall_s: float
    first_audio_s: float | None
    peak_gb: float
    segments: int = 1  # text chunks that produced audio
    chunks: int = 1  # generate() results (streamed pieces when streaming, else one per text chunk)
    continuation: dict | None = None  # {takes, frames, calls} when previous takes were used

    @property
    def duration_s(self) -> float:
        return len(self.audio) / self.sample_rate if self.sample_rate else 0.0


# ---------- naming ----------

def strip_tags(text: str) -> str:
    return re.sub(r"<\|[^|]*\|>|\[[^\]]*\]", " ", text or "")


def slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^\w]+", "_", strip_tags(text), flags=re.UNICODE).strip("_").lower()
    return s[:limit].rstrip("_") or "audio"


def output_base(label: str, index: int, total: int, when: datetime | None = None) -> str:
    """HHMMSS_slug[_vN]."""
    base = f"{(when or datetime.now()):%H%M%S}_{slug(label)}"
    if total > 1:
        base += f"_v{index + 1}"
    return base


def estimate_seconds(text: str | None) -> float:
    """Rough spoken length: ~15 characters per second, tags removed, plus PAUSE_SECONDS per [pause]."""
    if not text:
        return 0.0
    pauses = len(re.findall(r"\[pause\]", text, re.I))
    return round(len(strip_tags(text).strip()) / 15.0 + pauses * PAUSE_SECONDS, 1)


# ---------- text rules (mirror of fish_drama3/chunking.py, mlx-audio 0.6.7) ----------

# Unicode White_Space (str.strip() would also strip U+001C..U+001F).
WHITE_SPACE = "".join(map(chr, [*range(0x9, 0xE), 0x20, 0x85, 0xA0, 0x1680, *range(0x2000, 0x200B),
                                0x2028, 0x2029, 0x202F, 0x205F, 0x3000]))
_WHITE_SPACE_RUN = re.compile(f"[{re.escape(WHITE_SPACE)}]+")
_LINE_BREAKS = frozenset("\n\r\v\f\x85  ")
_UNCUTTABLE = re.compile(r"<\|[^|>]*\|>|<([A-Za-z][\w-]*)\b[^>]*>.*?</\1>|\[[^\[\]]*\]", re.S)
_SENTENCE_ENDS = frozenset(".!?;。！？；…\n")
_CLAUSE_MARKS = frozenset(",:，：、")
_SPACES = frozenset(" ")


def check_text(text: str, location: str = "text") -> None:
    """chunking.validate_text as a RequestError: empty text, structural tokens, speaker tags outside 0..15
    (or with leading zeros)."""
    if not (text or "").strip(WHITE_SPACE):
        raise RequestError(f"{location}: text is empty")
    for token in STRUCTURAL_TOKENS:
        if token in text:
            raise RequestError(f"{location}: text must not contain {token}")
    for start in (m.start() for m in re.finditer(r"<\|speaker", text)):
        tag = SPEAKER_TAG.match(text, start)
        number = tag.group(1) if tag else ""
        if not number or number != str(int(number)) or int(number) > MAX_SPEAKERS - 1:
            raise RequestError(f"{location}: speaker tags run from <|speaker:0|> to <|speaker:{MAX_SPEAKERS - 1}|>")


validate = check_text


def flatten_lines(text: str) -> str:
    """Each whitespace run containing a line break -> one space (edges removed), as the model does per chunk."""
    def join(m: re.Match) -> str:
        if not any(c in _LINE_BREAKS for c in m.group()):
            return m.group()
        if m.start() == 0 or m.end() == len(text):
            return ""
        return " "
    return _WHITE_SPACE_RUN.sub(join, text)


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _cut_after(text: str, marks: frozenset) -> list[str]:
    parts, start = [], 0
    for i, ch in enumerate(text):
        decimal = ch == "." and 0 < i < len(text) - 1 and text[i - 1].isdigit() and text[i + 1].isdigit()
        if ch in marks and not decimal:
            parts.append(text[start:i + 1])
            start = i + 1
    if start < len(text):
        parts.append(text[start:])
    return parts


def _fit(text: str, limit: int, marks: tuple) -> list[str]:
    if _bytes(text) <= limit:
        return [text]
    if marks:
        return [p for part in _cut_after(text, marks[0]) for p in _fit(part, limit, marks[1:])]
    pieces, cur = [], ""
    for ch in text:
        if cur and _bytes(cur + ch) > limit:
            pieces.append(cur)
            cur = ""
        cur += ch
    return pieces + [cur]


def _free_pieces(text: str, limit: int) -> list[str]:
    return [p for s in _cut_after(text, _SENTENCE_ENDS) for p in _fit(s, limit, (_CLAUSE_MARKS, _SPACES))]


def _pieces(text: str, limit: int) -> list[str]:
    pieces, last = [], 0
    for m in _UNCUTTABLE.finditer(text):
        pieces += _free_pieces(text[last:m.start()], limit)
        pieces.append(m.group(0))
        last = m.end()
    return pieces + _free_pieces(text[last:], limit)


def _split_by_length(text: str, limit: int) -> list[str]:
    if not text.strip(WHITE_SPACE):
        return []
    if _bytes(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for p in _pieces(text, limit):
        if cur and _bytes(cur + p) > limit:
            chunks.append(cur)
            cur = ""
        cur += p
    chunks.append(cur)
    return [c.strip(WHITE_SPACE) for c in chunks if c.strip(WHITE_SPACE)]


def mirror_split_text(text: str, limit: int = CHUNK_BYTES, max_speakers: int = CHUNK_SPEAKERS) -> list[str]:
    """Pure-Python copy of chunking.split_text (used by FakeModel, and by count_chunks with the fake model)."""
    text = flatten_lines(text)
    if not SPEAKER_TAG.search(text):
        return _split_by_length(text, limit)
    parts = SPEAKER_TAG.split(text)
    turns = [("0", parts[0])] + list(zip(parts[1::2], parts[2::2]))
    tagged = [(sp, f"<|speaker:{sp}|> {p}") for sp, body in turns for p in _split_by_length(body, limit)]
    chunks: list[list[str]] = []
    speakers: set = set()
    size = 0
    for sp, piece in tagged:
        if chunks and ((sp not in speakers and len(speakers) >= max_speakers) or size + _bytes(piece) > limit):
            chunks.append([])
            speakers, size = set(), 0
        if not chunks:
            chunks.append([])
        chunks[-1].append(piece)
        speakers.add(sp)
        size += _bytes(piece)
    return ["\n".join(c) for c in chunks]


def split_text(text: str) -> list[str]:
    """The chunks generate() will make. Always the pure-Python mirror (cheap, safe on the HTTP thread);
    scripts/smoke.py checks it against the model's chunking.split_text."""
    return mirror_split_text(text)


def count_chunks(text: str) -> int:
    return max(1, len(split_text(text)))


def normalize_script(script: str) -> str:
    """Dialogue scripts: accept native <|speaker:N|> (N from 0) or legacy [S1]… (from 1), one turn per line."""
    lines = [l.strip() for l in (script or "").splitlines() if l.strip() and not l.lstrip().startswith("#")]
    s = "\n".join(lines)
    return LEGACY_SPEAKER.sub(lambda m: f"<|speaker:{int(m.group(1)) - 1}|>", s)


def parse_speakers(script: str) -> list[int]:
    return sorted({int(n) for n in SPEAKER_TAG.findall(script)})


# ---------- request -> generate() kwargs ----------

def _float(params: dict, key: str, default=None):
    v = params.get(key)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        raise RequestError(f"{key} must be a number")


def _str(params: dict, key: str) -> str | None:
    v = params.get(key)
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def text_lines(text: str) -> list[str]:
    """One take per line: skip blanks and # comments."""
    return [l.strip() for l in (text or "").splitlines() if l.strip() and not l.lstrip().startswith("#")]


def sampling(params: dict, warnings: list[str] | None = None) -> dict:
    """Preset (params.preset, default balanced) overridden by temperature / top_p / top_k (empty or "off" = off)."""
    name = params.get("preset") or DEFAULT_PRESET
    if name in LEGACY_PRESETS:
        if warnings is not None:
            warnings.append(f'preset "{name}" was an S2 Pro preset; used "{DEFAULT_PRESET}"')
        name = DEFAULT_PRESET
    if name not in SAMPLING_PRESETS:
        raise RequestError(f"preset must be one of: {', '.join(SAMPLING_PRESETS)}")
    kw = dict(SAMPLING_PRESETS[name])
    for k in ("temperature", "top_p"):
        v = _float(params, k)
        if v is not None:
            kw[k] = v
    if str(params.get("top_k") or "").strip().lower() != "off" and (v := _float(params, "top_k")) is not None:
        if v < 1 or v != int(v):
            raise RequestError("top_k must be empty (off) or an integer ≥ 1")
        kw["top_k"] = int(v)
    if not 0 < kw["temperature"] <= 2:
        raise RequestError("temperature must be in (0, 2]")
    if not 0 < kw["top_p"] <= 1:
        raise RequestError("top_p must be in (0, 1]")
    return kw


def language(params: dict, key: str = "language") -> str:
    lang = (_str(params, key) or "auto").lower()
    if lang != "auto" and lang not in LANGUAGES:
        raise RequestError(f"{key} must be auto or one of: {', '.join(LANGUAGES)}")
    return lang


def languages(params: dict) -> tuple[str, str]:
    """(language, language_mode): params.language is the effective hint (the picker's detected or chosen code, or
    "auto"); params.language_mode is the picker's raw value ("auto" or a code; defaults to language)."""
    lang = language(params)
    mode = language(params, "language_mode") if _str(params, "language_mode") else lang
    return lang, mode


def legacy_warnings(params: dict) -> list[str]:
    """S2 Pro params Drama 3 doesn't take: never sent to generate(), one warning each."""
    out = []
    if _str(params, "instruct"):
        out.append("instruct was ignored: Drama 3 takes delivery cues inline in the text, e.g. [whispers] or [sighs].")
    speed = params.get("speed")
    if speed not in (None, ""):
        try:
            unit = float(speed) == 1.0
        except (TypeError, ValueError):
            unit = False
        if not unit:
            out.append("speed was ignored: Drama 3 has no speed control.")
    if params.get("chunk_length") not in (None, ""):
        out.append("chunk_length was ignored: Drama 3 chunks text itself (~300 bytes).")
    return out


def common_kwargs(params: dict, has_voice: bool, warnings: list[str]) -> dict:
    """Sampling plus an explicit per-chunk max_tokens cap if given (max_tokens, or max_seconds)."""
    warnings += legacy_warnings(params)
    kw = sampling(params, warnings)
    if kw["top_k"] is not None and not has_voice:
        warnings.append("top_k caps the main token: without a reference voice takes can fall silent. "
                        "Leave it off unless you're testing.")
    max_tokens = _float(params, "max_tokens")
    if max_tokens is None and (ms := _float(params, "max_seconds")) is not None:
        max_tokens = ms * FRAMES_PER_SECOND
    if max_tokens is not None:
        if max_tokens < 1:
            raise RequestError("max_tokens must be ≥ 1")
        kw["max_tokens"] = int(max_tokens)
    return kw


def _cap_warning(kw: dict, text: str, warnings: list[str]):
    """Warn when an explicit per-chunk cap is below the expected length of the longest chunk."""
    cap = kw.get("max_tokens")
    if cap is None:
        return
    need = max((_bytes(strip_tags(c).strip()) * FRAMES_PER_BYTE for c in split_text(text)), default=0)
    if cap < need:
        warnings.append(f"max length per chunk ({cap / FRAMES_PER_SECOND:.1f} s) is below the estimated "
                        f"{need / FRAMES_PER_SECOND:.1f} s; long chunks may be cut off. Clear it to use the model's limit.")


def check_length(n_chars: int):
    if n_chars > MAX_JOB_CHARS:
        raise RequestError(f"too much text for one job ({n_chars} characters, max {MAX_JOB_CHARS}); split it up")


def _transcript(v: Voice) -> str:
    t = (v.transcript or "").strip()
    if not t:
        raise RequestError(f"voice '{v.id}' has no transcript; transcribe it in Library first")
    t = SPEAKER_TAG.sub(" ", t).strip()
    check_text(t, f"transcript of voice '{v.id}'")
    return t


def resolve_history(params: dict, resolve_take: Callable[[str], PrevTake] | None) -> list[PrevTake]:
    """params.previous_takes ["<workflow>/<stem>", …] oldest first -> [PrevTake]; at most MAX_PREVIOUS_TAKES and
    MAX_HISTORY_S seconds in total. resolve_take raises RequestError for missing / non-Drama 3 takes."""
    ids = params.get("previous_takes") or []
    if not isinstance(ids, list) or not all(isinstance(t, str) and t for t in ids):
        raise RequestError("previous_takes must be a list of take ids")
    if not ids:
        return []
    if len(ids) > MAX_PREVIOUS_TAKES:
        raise RequestError(f"at most {MAX_PREVIOUS_TAKES} previous takes")
    if resolve_take is None:
        raise RequestError("previous takes aren't available here")
    takes = [resolve_take(t) for t in ids]
    total = sum(t.duration_s or 0 for t in takes)
    if total > MAX_HISTORY_S:
        raise RequestError(f"previous takes are {total:.0f} s long (max {MAX_HISTORY_S:.0f} s); drop some")
    for t in takes:
        check_text(t.text, f"previous take {t.id}")
    return takes


def _history_warnings(history: list[PrevTake], voice_ids: list, warnings: list[str]):
    for t in history:
        if sorted(map(str, t.voices or [])) != sorted(map(str, voice_ids)):
            warnings.append(f"previous take {t.id} used different voices ({', '.join(t.voices or []) or 'none'}); "
                            "the continuation may drift between them.")


def tts_items(params: dict, voice: Voice | None, history: list[PrevTake] | None = None) -> list[Item]:
    text = params.get("text") or ""
    if SPEAKER_TAG.search(text) or LEGACY_SPEAKER.search(text):
        raise RequestError("speaker tags belong in Dialogue; Speech uses one voice")
    texts = text_lines(text) if params.get("lines") else [text.strip(WHITE_SPACE)]
    texts = [t for t in texts if t]
    if not texts:
        raise RequestError("text is empty")
    check_length(sum(len(t) for t in texts))
    for t in texts:
        check_text(t)
    lang, mode = languages(params)
    warnings: list[str] = []
    base = common_kwargs(params, voice is not None, warnings)
    refs = []
    if voice:
        base["ref_audio"] = voice.audio
        base["ref_text"] = _transcript(voice)
        refs = [(0, voice)]
    history = history or []
    _history_warnings(history, [voice.id] if voice else [], warnings)
    items = []
    for t in texts:
        item_warnings = list(warnings)
        kw = {**base, "text": t}
        _cap_warning(kw, t, item_warnings)
        item = Item(label=t, kwargs=kw, refs=list(refs), history=list(history), segments=count_chunks(t),
                    seconds_hint=estimate_seconds(t), warnings=item_warnings, language=lang, language_mode=mode,
                    speakers={0: voice.id if voice else None})
        items.append(item)
    return items


def dialogue_items(params: dict, voices: list[Voice | None], history: list[PrevTake] | None = None) -> list[Item]:
    script = normalize_script(params.get("script") or params.get("text") or "")
    if script.strip(WHITE_SPACE):
        check_text(script, "script")
    speakers = parse_speakers(script)
    if not speakers:
        raise RequestError('tag each turn with a speaker, e.g. "<|speaker:0|>Hi! <|speaker:1|>Hello." (or [S1] / [S2])')
    if speakers != list(range(len(speakers))):
        raise RequestError(f"speakers must be numbered 0…{len(speakers) - 1} without gaps")
    if len(speakers) > MAX_SPEAKERS:
        raise RequestError(f"at most {MAX_SPEAKERS} speakers")
    if SPEAKER_TAG.split(script, maxsplit=1)[0].strip(WHITE_SPACE):
        raise RequestError("text before the first speaker tag would be read as speaker 0; tag it with a speaker")
    check_length(len(strip_tags(script)))
    lang, mode = languages(params)
    warnings: list[str] = []
    extra = [i for i, v in enumerate(voices) if v and i >= len(speakers)]
    if extra:
        warnings.append(f"voices for speakers {', '.join(str(i) for i in extra)} were ignored: the script has "
                        f"{len(speakers)} speaker(s)")
    refs = [(k, voices[k]) for k in speakers if k < len(voices) and voices[k]]
    base = common_kwargs(params, bool(refs), warnings)
    if refs:  # D2: only voiced speakers get a reference; the others get a model-chosen voice
        base["ref_audio"] = [v.audio for _, v in refs]
        base["ref_text"] = [f"<|speaker:{k}|> {_transcript(v)}" for k, v in refs]
    history = history or []
    _history_warnings(history, [v.id for _, v in refs], warnings)
    kw = {**base, "text": script}
    _cap_warning(kw, script, warnings)
    label = _str(params, "name") or script
    item = Item(label=label, kwargs=kw, refs=refs, history=list(history), segments=count_chunks(script),
                seconds_hint=estimate_seconds(script), warnings=warnings, language=lang, language_mode=mode,
                speakers={k: (voices[k].id if k < len(voices) and voices[k] else None) for k in speakers})
    return [item]


def build_items(workflow: str, params: dict, resolve_voice: Callable[[str], Voice],
                resolve_take: Callable[[str], PrevTake] | None = None) -> list[Item]:
    """Validate a job request and turn it into generate() calls.

    tts:      params {text, lines?, voice?, preset?, temperature?, top_p?, top_k? (empty/"off" = off),
                      max_tokens? | max_seconds? (per-chunk cap), language?, previous_takes?}
    dialogue: params {script (or text), voices?: [voice id | null per speaker, ≤ 16], name?, + the above}
    Legacy S2 Pro params (instruct, speed, chunk_length) are ignored with a warning.
    """
    if workflow not in WORKFLOW_MODELS:
        raise RequestError(f"unknown workflow '{workflow}'")
    history = resolve_history(params, resolve_take)
    if workflow == "dialogue":
        ids = params.get("voices") or []
        if not isinstance(ids, list):
            raise RequestError("voices must be a list")
        if len(ids) > MAX_SPEAKERS:
            raise RequestError(f"at most {MAX_SPEAKERS} voices")
        return dialogue_items(params, [resolve_voice(v) if v else None for v in ids], history)
    voice = resolve_voice(params["voice"]) if params.get("voice") else None
    return tts_items(params, voice, history)


def warmup_kwargs(voice: Voice | None = None) -> dict:
    """A couple of words with a tiny max_tokens; output is thrown away. With a voice it primes the model's voice
    cache (same ref_audio/ref_text as a Speech job with that voice)."""
    kw = dict(text=WARMUP_TEXT, max_tokens=WARMUP_MAX_TOKENS, **SAMPLING_PRESETS[DEFAULT_PRESET])
    if voice and (voice.transcript or "").strip():
        kw.update(ref_audio=voice.audio, ref_text=_transcript(voice))
    return kw


# ---------- model ----------

def load_model(override: str | None = None):
    if FAKE:
        return FakeModel()
    from mlx_audio.tts import load

    return load(override or MODELS[MODEL_KEY])


def encode_take(model, wav: str, gain: float | None = None):
    """A take's WAV -> codec codes int32 [10, T] (private API, mlx-audio 0.6.7). The saved WAV is divided by its
    normalization gain first, so the history is what the model produced. Callers cache per take."""
    _check_private_api(model)
    samples = model._samples(str(wav))
    if gain:
        samples = samples / gain
    return model._encode_audio(samples)


def _check_private_api(model):
    if not all(hasattr(model, a) for a in ("_encode_audio", "_samples", "build_frames")):
        raise RequestError("continuing from previous takes needs mlx-audio 0.6.7 (Fish Drama 3 internals changed)")


@contextmanager
def continuation(model, history: list[PrevTake], codes_for: Callable[[PrevTake], object] | None = None):
    """Make generate() see the previous takes as earlier conversation turns: wraps model.build_frames to prepend
    [(flattened take text, codes), …] (oldest first) to the per-chunk history. Restores it in finally.

    Yields a stats dict {takes, frames, calls} (calls = build_frames calls that got the extra turns)."""
    stats = {"takes": len(history or []), "frames": 0, "calls": 0}
    if not history:
        yield stats
        return
    _check_private_api(model)
    turns = []
    for t in history:
        codes = codes_for(t) if codes_for else encode_take(model, t.wav, t.gain)
        turns.append((flatten_lines(t.text), codes))
        stats["frames"] += int(codes.shape[1])
    had_attr = "build_frames" in vars(model)  # normally a class method; restore exactly that
    orig = model.build_frames

    def with_takes(references, hist, target):
        stats["calls"] += 1
        return orig(references, [*turns, *hist], target)

    model.build_frames = with_takes
    try:
        yield stats
    finally:
        if had_attr:
            model.build_frames = orig
        else:
            del model.build_frames


# ---------- generation ----------

def to_mono(a):
    if a.ndim == 2:
        if a.shape[0] <= 8 and a.shape[0] < a.shape[1]:  # (channels, samples)
            a = a.T
        a = a.mean(axis=1)
    return a.reshape(-1)


def render(model, kwargs: dict, *, seed: int | None = None, stream: bool = False,
           on_chunk: Callable[[object, int, float], None] | None = None,
           on_progress: Callable[[int, int, float], None] | None = None,
           should_cancel: Callable[[], bool] | None = None, history: list[PrevTake] | None = None,
           codes_for: Callable[[PrevTake], object] | None = None, segments: int | None = None) -> Take | None:
    """One variation: seed, run generate() (streaming or not), concatenate the audio. None when no audio came out.

    on_chunk(audio_f32, sample_rate, seconds_so_far) fires for every result with audio (streamed pieces);
    on_progress(segment_done, segments_total, seconds_so_far) fires when a text chunk is finished;
    should_cancel() is polled between results. history: previous takes (see continuation()).
    The generator is always closed (finally), so a cancel or error frees the model's state.
    """
    import numpy as np

    text = kwargs.get("text") or ""
    total_segments = segments or count_chunks(text)
    mx = None
    if not isinstance(model, FakeModel):
        import mlx.core as mx

        mx.reset_peak_memory()
    if seed is not None:
        if mx is not None:
            mx.random.seed(seed)
        else:
            model.seed(seed)
    kw = dict(kwargs)
    kw["stream"] = bool(stream)
    if stream:
        kw.update(streaming_interval=STREAM_INTERVAL, first_chunk_frames=FIRST_CHUNK_FRAMES)
    with continuation(model, history or [], codes_for) as cont:
        t0 = time.perf_counter()
        first = None
        pieces, sr, total, results = [], SAMPLE_RATE, 0, 0
        done_seg = -1

        def advance(seg: int):
            nonlocal done_seg
            if seg > done_seg:
                done_seg = seg
                if on_progress:
                    on_progress(seg, max(total_segments, seg + 1), total / sr)

        gen = model.generate(**kw)
        try:
            for r in gen:
                results += 1
                seg = int(getattr(r, "segment_idx", 0) or 0)
                if stream:
                    advance(seg - 1)  # a new segment index means the previous chunk is finished
                a = to_mono(np.asarray(r.audio, dtype=np.float32))
                sr = r.sample_rate
                if len(a):
                    if first is None:
                        first = time.perf_counter() - t0
                    pieces.append(a)
                    total += len(a)
                    if on_chunk:
                        on_chunk(a, sr, total / sr)
                if not stream or getattr(r, "is_final_chunk", False):
                    advance(seg)
                if should_cancel and should_cancel():
                    raise Cancelled()
        finally:
            gen.close()
        if pieces and done_seg < 0:
            advance(0)
    if not pieces:
        return None
    peak = mx.get_peak_memory() / 1e9 if mx is not None else 0.0
    return Take(audio=np.concatenate(pieces), sample_rate=sr, wall_s=time.perf_counter() - t0,
                first_audio_s=first, peak_gb=peak, segments=done_seg + 1, chunks=results,
                continuation=dict(cont) if history else None)


def normalize_take(audio, language: str | None = None):
    """Saved takes only (D1): a peak below NORMALIZE_BELOW is scaled to NORMALIZE_TO (quiet languages, e.g.
    Japanese). Returns (audio, gain) with gain None when untouched. Applies to every language for now."""
    import numpy as np

    a = np.asarray(audio, dtype=np.float32)
    peak = float(np.abs(a).max()) if a.size else 0.0
    if peak <= 1e-4 or peak >= NORMALIZE_BELOW:
        return a, None
    gain = NORMALIZE_TO / peak
    return (a * gain).astype(np.float32), round(gain, 3)


# ---------- audio files ----------

def write_audio(path: Path, audio, sample_rate: int, fmt: str = "wav"):
    """WAV via stdlib (16-bit PCM); other formats via mlx_audio.audio_io (mp3 needs ffmpeg)."""
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "wav":
        import wave

        pcm = (np.clip(np.asarray(audio, dtype=np.float32).reshape(-1), -1, 1) * 32767).astype("<i2")
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(int(sample_rate))
            w.writeframes(pcm.tobytes())
        return
    from mlx_audio.audio_io import write as audio_write

    audio_write(str(path), audio, sample_rate, format=fmt)


def read_wav(path: str):
    """(float32 mono array, sample_rate) for a PCM WAV, stdlib only."""
    import wave

    import numpy as np

    with wave.open(str(path)) as w:
        sr, ch, width, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    dtype = {1: np.uint8, 2: "<i2", 4: "<i4"}[width]
    a = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    a = (a - 128) / 128 if width == 1 else a / float(2 ** (8 * width - 1))
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a, sr


# ---------- fake model (FISH_READER_FAKE_MODEL=1) ----------

@dataclass
class _FakeResult:
    audio: "object"
    samples: int
    sample_rate: int
    segment_idx: int
    token_count: int
    audio_duration: str
    real_time_factor: float
    processing_time_seconds: float
    peak_memory_usage: float
    is_streaming_chunk: bool
    is_final_chunk: bool


class FakeModel:
    """Stand-in with Drama 3's generate() interface and errors: one tone per text chunk (mirror_split_text), or
    ~0.32 s pieces of it when streaming. No MLX, no weights. build_frames/_samples/_encode_audio exist so
    continuation() runs the same way; last_history records the history turns build_frames got last."""

    sample_rate = SAMPLE_RATE

    def __init__(self):
        import numpy as np

        self._rng = np.random.default_rng(0)
        self.delay = float(os.environ.get("FISH_READER_FAKE_DELAY") or os.environ.get("FISH_UI_FAKE_DELAY") or "0.05")
        self.last_history: list = []

    def seed(self, seed: int):
        import numpy as np

        self._rng = np.random.default_rng(seed)

    def _samples(self, audio):
        import numpy as np

        if isinstance(audio, (str, Path)):
            audio = read_wav(str(audio))[0]
        return np.asarray(audio, dtype=np.float32).reshape(-1)

    def _encode_audio(self, samples):
        import numpy as np

        self.last_encoded_peak = float(np.abs(samples).max()) if len(samples) else 0.0
        frames = max(1, int(len(samples) / self.sample_rate * FRAMES_PER_SECOND))
        return np.zeros((10, frames), dtype=np.int32)

    def build_frames(self, references, history, target):
        try:
            check_text(target, "text")
            for i, (t, _c) in enumerate(history, 1):
                check_text(t, f"history {i}")
        except RequestError as e:
            raise ValueError(str(e)) from None
        self.last_history = [t for t, _c in history]
        return len(history), len(references)

    def generate(self, text: str, ref_audio=None, ref_text=None, max_tokens=None, temperature=None, top_k=None,
                 top_p=None, stream: bool = False, streaming_interval: float = 0.32, first_chunk_frames=None,
                 speed=1.0, instruct=None, **kwargs):
        import numpy as np

        if speed not in (None, 1.0):
            raise ValueError("Drama 3 does not support speed control")
        if instruct:
            raise ValueError("Drama 3 takes delivery cues inline, e.g. [whispers], not instruct")
        try:
            check_text(text, "text")
        except RequestError as e:
            raise ValueError(str(e)) from None
        audios = [] if ref_audio is None else (list(ref_audio) if isinstance(ref_audio, (list, tuple)) else [ref_audio])
        texts = [] if ref_text is None else ([ref_text] if isinstance(ref_text, str) else list(ref_text))
        if len(audios) != len(texts):
            raise ValueError("every reference audio needs its transcript (ref_text)")
        refs = [(t, None) for t in texts]
        temperature = 1.0 if temperature is None else temperature
        max_frames = max_tokens or DEFAULT_MAX_TOKENS
        interval = max(1, round(streaming_interval * FRAMES_PER_SECOND))
        first = first_chunk_frames or interval
        spf = self.sample_rate / FRAMES_PER_SECOND  # samples per frame
        chunks = mirror_split_text(text)
        pinned = latest = None
        for i, chunk in enumerate(chunks):
            is_last = i == len(chunks) - 1
            self.build_frames(refs, [t for t in (pinned, latest) if t is not None], chunk)
            secs = min(max(estimate_seconds(chunk), 0.4), max_frames / FRAMES_PER_SECOND, 8.0)
            frames = max(1, int(secs * FRAMES_PER_SECOND))
            n = int(frames * spf)
            t = np.arange(n) / self.sample_rate
            f = 180 + 40 * (i % 5) + 60 * float(self._rng.random())
            env = np.minimum(1, np.minimum(t, t[::-1]) * 20) if n else t
            tone = 0.2 * np.sin(2 * np.pi * f * t) * (1 + 0.3 * np.sin(2 * np.pi * 3 * t))
            noise = 0.02 * temperature * self._rng.standard_normal(n)
            a = ((tone + noise) * env).astype(np.float32)
            turn = (chunk, None)
            if pinned is None:
                pinned = turn
            else:
                latest = turn
            if not stream:
                time.sleep(self.delay)
                yield self._result(a, i, frames, False, is_last)
                continue
            start, emitted = 0, 0
            while emitted < frames:
                due = first if i == 0 and emitted == 0 else interval
                take = min(due, frames - emitted)
                end = n if emitted + take >= frames else int((emitted + take) * spf)
                time.sleep(self.delay)
                emitted += take
                yield self._result(a[start:end], i, take, True, is_last and emitted >= frames)
                start = end

    def _result(self, a, idx, frames, streaming, final):
        secs = len(a) / self.sample_rate
        return _FakeResult(audio=a, samples=len(a), sample_rate=self.sample_rate, segment_idx=idx,
                           token_count=frames, audio_duration=f"{secs:.2f}s", real_time_factor=1.0,
                           processing_time_seconds=self.delay, peak_memory_usage=0.0,
                           is_streaming_chunk=streaming, is_final_chunk=final)


def items_summary(items: Iterable[Item]) -> str:
    items = list(items)
    return f"{len(items)} item(s): " + "; ".join(i.label[:40] for i in items[:3])
