"""Text rules the reader shares with fish-ui (copied from fish-ui/backend/fish_core.py, mlx-audio 0.6.7).

Keep in step with fish_core: MAX_JOB_CHARS, MAX_HISTORY_S, STRUCTURAL_TOKENS, strip_tags, check_text.
Also: splitting article text into parts, and the word guard that checks a tagger only inserted cues.
"""
from __future__ import annotations

import re

MAX_JOB_CHARS = 3000      # fish_core.MAX_JOB_CHARS: characters per job
MAX_HISTORY_S = 90.0      # fish_core.MAX_HISTORY_S: total audio of previous takes per job
PART_CHARS = 1100         # reader part size before cues: ~75 s of audio, so the previous part fits MAX_HISTORY_S
FIRST_PART_CHARS = 400    # the first body part is short so its tagging (and so the first words) come quickly
STRUCTURAL_TOKENS = ("<|im_start|>", "<|im_end|>", "<|voice|>", "<|text|>", "<|interleave|>", "<|semantic:")

# Point cues: an event at one spot ([sighs] Fine.). sighs/laughs are the model card's examples.
POINT_CUES = ["sighs", "laughs", "chuckles", "pause"]
# Span tags: a delivery for a stretch of words, <whisper>…</whisper>; the voice returns to normal after the
# closing tag. Drama 3's chunker never cuts inside one. Measured 2026-11-24 (README "Cues"): <whisper> is
# 12-16 dB quieter and recovers at once, where [whispers] is ~5 dB and lingers; <slow>/<fast> change the pace;
# no tag name is ever spoken.
SPAN_TAGS = ["whisper", "excited", "sad", "angry", "serious", "sarcastic", "shouting", "emphasis", "laughing",
             "slow", "fast"]
CUES = POINT_CUES  # kept for callers that only know point cues
MAX_SPAN_CHARS = 400  # a span is never cut, so keep it to a sentence or two

_TAG = re.compile(r"<\|[^|]*\|>|\[[^\]]*\]|</?[A-Za-z][\w-]*>")
_SPAN = re.compile(r"<(/?)([A-Za-z][\w-]*)>")
_WORD = re.compile(r"[\w']+", re.UNICODE)


class TextError(ValueError):
    pass


def strip_tags(text: str) -> str:
    """Point cues, span tags and speaker tags -> a space (fish_core.strip_tags plus <tag>/</tag>)."""
    return _TAG.sub(" ", text or "")


def check_text(text: str) -> None:
    """fish_core.check_text for single-speaker text: not empty, no structural tokens, no speaker tags."""
    if not (text or "").strip():
        raise TextError("text is empty")
    for token in STRUCTURAL_TOKENS:
        if token in text:
            raise TextError(f"text must not contain {token}")
    if "<|speaker" in text or re.search(r"\[S\d+\]", text):
        raise TextError("text must not contain speaker tags")


def words(text: str) -> list[str]:
    """Spoken words, case-folded, cues removed; punctuation and apostrophe styles ignored."""
    t = strip_tags(text).replace("\u2019", "'").replace("\u2018", "'")
    return [w.casefold() for w in _WORD.findall(t)]


def cues_in(text: str) -> list[str]:
    return [m.group()[1:-1].strip().lower() for m in re.finditer(r"\[[^\]]*\]", text or "")]


def check_spans(text: str) -> str | None:
    """None when every <tag> is a known span tag, closed by its </tag>, not nested, within one paragraph and
    at most MAX_SPAN_CHARS long; else the reason."""
    open_tag, start = None, 0
    for m in _SPAN.finditer(text):
        closing, name = m.group(1) == "/", m.group(2).lower()
        if name not in SPAN_TAGS:
            return f"unknown tag <{name}>"
        if not closing:
            if open_tag:
                return f"<{name}> inside <{open_tag}>: tags can't nest"
            open_tag, start = name, m.end()
        else:
            if open_tag != name:
                return f"</{name}> without <{name}>"
            inner = text[start:m.start()]
            if not inner.strip() or "\n\n" in inner:
                return f"<{name}> must wrap words within one paragraph"
            if len(inner) > MAX_SPAN_CHARS:
                return f"<{name}> span is {len(inner)} characters (max {MAX_SPAN_CHARS})"
            open_tag = None
    return f"<{open_tag}> is never closed" if open_tag else None


def guard(original: str, tagged: str) -> str | None:
    """None when `tagged` is `original` with only cues/tags (and whitespace/punctuation) changed, else the reason."""
    if words(original) != words(tagged):
        a, b = words(original), words(tagged)
        i = next((k for k, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
        return f"words changed near word {i}: {' '.join(a[i:i + 4])!r} -> {' '.join(b[i:i + 4])!r}"
    unknown = sorted({c for c in cues_in(tagged) if c not in POINT_CUES})
    if unknown:
        return f"unknown cues: {', '.join(unknown)}"
    if problem := check_spans(tagged):
        return problem
    try:
        check_text(tagged)
    except TextError as e:
        return str(e)
    return None


def _sentences(par: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?…])\s+", par) if s]


def _cut(text: str, limit: int) -> list[str]:
    """A paragraph over `limit` -> pieces at sentence ends (then spaces as a last resort)."""
    out, cur = [], ""
    for s in _sentences(text):
        while len(s) > limit:
            cut = s.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if cur:
                out.append(cur)
                cur = ""
            out.append(s[:cut].strip())
            s = s[cut:].strip()
        if cur and len(cur) + 1 + len(s) > limit:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}" if cur else s
    if cur:
        out.append(cur)
    return out


def split_parts(paragraphs: list[str], limit: int = PART_CHARS, first_limit: int | None = None) -> list[str]:
    """Paragraphs -> parts of at most `limit` characters, cut at paragraph boundaries (sentence ends for a
    paragraph longer than the limit). Paragraphs within a part are joined with a blank line. With
    `first_limit`, the first part is whole sentences up to that size and may end mid-paragraph."""
    pars = [p.strip() for p in paragraphs if p.strip()]
    if first_limit and pars:
        first, rest = [], []
        for i, par in enumerate(pars):
            sents = _sentences(par)
            taken = []
            for j, sent in enumerate(sents):
                size = sum(len(x) + 1 for x in taken) + sum(len(x) + 2 for x in first)
                if size + len(sent) > first_limit:
                    if not first and not taken:  # one sentence longer than first_limit: cut it
                        head, *tail = _cut(sent, first_limit)
                        taken, sents = [head], [*tail, *sents[j + 1:]]
                        j = -1
                    break
            else:
                j = len(sents)
            if j == -1:
                first.append(" ".join(taken))
                rest = [" ".join(sents)] + pars[i + 1:]
                break
            taken = sents[:j]
            if taken:
                first.append(" ".join(taken))
            if j < len(sents):
                rest = [" ".join(sents[j:])] + pars[i + 1:]
                break
        rest = [r for r in rest if r]
        return ["\n\n".join(first)] + (split_parts(rest, limit) if rest else [])
    parts, cur = [], ""
    for par in pars:
        pieces = _cut(par, limit) if len(par) > limit else [par]
        for piece in pieces:
            if cur and len(cur) + 2 + len(piece) > limit:
                parts.append(cur)
                cur = piece
            else:
                cur = f"{cur}\n\n{piece}" if cur else piece
    if cur:
        parts.append(cur)
    return parts
