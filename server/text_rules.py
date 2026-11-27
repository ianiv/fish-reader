"""Reader text rules on top of fish_core: splitting article text into parts, and the word guard that checks a
tagger only inserted cues. strip_tags / check_text extend fish_core's for span tags and single-speaker text.
"""
from __future__ import annotations

import re

from fish_core import MAX_HISTORY_S, MAX_JOB_CHARS, STRUCTURAL_TOKENS  # noqa: F401  (characters per take; seconds of previous takes)

PART_CHARS = 850          # reader part size before cues: measured ~12 chars/s untagged (1084 chars = 89.5 s), so
                          # ~70 s, leaving room for pauses and slow spans under MAX_HISTORY_S (90 s) for continuation
FIRST_PART_CHARS = 400    # the first body part is short so its tagging (and so the first words) come quickly

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


# --- podcast scripts ---

_TURN = re.compile(r"^\s*(?:\*\*)?(?:host\s+|speaker\s+)?([AB])(?:\*\*)?\s*[:：]\s*(?:\*\*)?\s*", re.I)


def parse_script(text: str) -> list[tuple[int, str]]:
    """A two-host script ("A: …" / "B: …" lines) -> [(speaker 0|1, turn text)]. Lines before the first turn
    (a preamble) are dropped; unprefixed lines after it belong to the turn above."""
    turns: list[list] = []
    for line in (text or "").splitlines():
        m = _TURN.match(line)
        if m:
            turns.append([0 if m.group(1).upper() == "A" else 1, line[m.end():].strip()])
        elif turns and line.strip():
            turns[-1][1] += " " + line.strip()
    out = []
    for spk, t in turns:
        t = clean_turn(t)
        if words(t):
            if out and out[-1][0] == spk:  # two turns in a row by one host: one turn
                out[-1] = (spk, out[-1][1] + " " + t)
            else:
                out.append((spk, t))
    return out


def clean_turn(text: str) -> str:
    """Keep only markup Drama 3 can use: known point cues and well-formed known span tags (else the tags go and
    the words stay); no speaker tags, structural tokens, parentheticals, markdown emphasis."""
    t = re.sub(r"<\|[^|]*\|>", " ", text)
    for token in STRUCTURAL_TOKENS:
        t = t.replace(token, " ")
    t = re.sub(r"\[S\d+\]", " ", t)
    t = re.sub(r"\[([^\]]*)\]", lambda m: m.group() if m.group(1).strip().lower() in POINT_CUES else " ", t)
    t = re.sub(r"\((?:[^()]*)\)", lambda m: m.group() if words(m.group()) and not re.match(
        r"\((?:laugh|sigh|pause|chuckl|music|beat|sound)", m.group(), re.I) else " ", t)
    t = re.sub(r"\*\*?|__", "", t)
    if check_spans(t):
        t = _SPAN.sub("", t)
    return re.sub(r"\s+", " ", t).strip()


def split_turns(turns: list[tuple[int, str]], limit: int = PART_CHARS, first_limit: int | None = None) \
        -> list[list[tuple[int, str]]]:
    """Turns -> parts of at most `limit` spoken characters, cut between turns (a longer turn is cut at sentence
    ends, its pieces staying with the same host). The first part can be shorter (`first_limit`), so audio starts
    sooner."""
    pieces = []
    for spk, t in turns:
        if len(strip_tags(t)) <= limit:
            pieces.append((spk, t))
        else:
            for piece in _cut(t, limit):
                pieces.append((spk, piece if not check_spans(piece) else _SPAN.sub("", piece)))
    parts: list[list[tuple[int, str]]] = []
    cur: list[tuple[int, str]] = []
    size = 0
    for spk, t in pieces:
        n = len(strip_tags(t))
        cap = first_limit if (first_limit and not parts) else limit
        if cur and size + n > cap:
            parts.append(cur)
            cur, size = [], 0
        cur.append((spk, t))
        size += n
    if cur:
        parts.append(cur)
    return parts
