"""Taggers: add Drama 3 markup (<whisper>…</whisper> spans, [sighs] point cues) to article text without changing its words.

Tagger protocol: `name`, `tag(text, context) -> str`. `tag_checked()` wraps any tagger with the word guard
(text_rules.guard): one retry on a bad answer, then the untagged text plus a warning.
Later: AppleTagger (a Swift CLI using Foundation Models on macOS 26) behind the same protocol.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from typing import Protocol

import text_rules as tr


class Tagger(Protocol):
    name: str

    def tag(self, text: str, context: dict) -> str: ...


SYSTEM_PROMPT = """You prepare article text for an expressive text-to-speech model (Fish Audio Drama 3). It reads \
two kinds of markup:
- Span tags wrap the words that need a particular delivery, and the voice returns to a normal narrating tone \
after the closing tag: "He leaned in. <whisper>The boss is leaving.</whisper> Nobody moved."
- Point cues mark a sound or beat at one spot: "[sighs] Fine." or "It was gone. [pause] All of it."

Mark the text up so a skilled narrator would read it with fitting emotion and intonation.
Rules:
- Copy the text EXACTLY. Never change, add, remove, reorder or respell a word. Keep punctuation and the blank \
lines between paragraphs.
- Only insert markup. Span tags: {spans}. Point cues: {points}. Use no others.
- The default is a calm, natural narrating voice with no markup. Use a span only where the words clearly call \
for that delivery (a whispered aside, an angry quote, a sad moment, sarcasm, a list read quickly), and close it \
as soon as that moment ends, usually within the same sentence or the next one. Never wrap a whole paragraph.
- Spans never nest, never overlap and never cross a blank line. Do not put two cues or tags back to back.
- <emphasis> wraps the one word or short phrase to stress. [pause] adds a short beat before a twist or a shift.
- Be sparing. Typical news or explanatory prose needs very little; quotes, anecdotes, jokes and dramatic \
moments need more.
- Reply with the marked-up text only: no preface, no notes, no code fences."""


def _spans() -> str:
    return ", ".join(f"<{t}>…</{t}>" for t in tr.SPAN_TAGS)


def _points() -> str:
    return ", ".join(f"[{c}]" for c in tr.POINT_CUES)


def _prompt(text: str, context: dict) -> str:
    head = [f"Article: {context['title']}" if context.get("title") else "",
            f"Previous passage (context only, do not tag or repeat it):\n{context['before']}" if context.get("before") else ""]
    head = "\n\n".join(h for h in head if h)
    return (head + "\n\n" if head else "") + "Passage to tag:\n<passage>\n" + text + "\n</passage>"


def _clean_answer(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^```\w*\n|\n```$", "", s).strip()
    m = re.search(r"<passage>\n?(.*?)\n?</passage>", s, re.S)
    return (m.group(1) if m else s).strip()


class ClaudeCliTagger:
    """`claude -p` (the Claude Code CLI, using its own login; no API key), no tools, no MCP, no session files."""
    name = "claude"

    def __init__(self, model: str = "sonnet", binary: str = "claude", timeout: float = 180):
        self.model, self.binary, self.timeout = model, binary, timeout

    def tag(self, text: str, context: dict) -> str:
        return _clean_answer(self.ask(SYSTEM_PROMPT.format(spans=_spans(), points=_points()), _prompt(text, context)))

    def ask(self, system: str, prompt: str, timeout: float | None = None) -> str:
        """One `claude -p` call; the answer's text."""
        cmd = [self.binary, "-p", "--output-format", "json", "--model", self.model, "--system-prompt", system,
               "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence"]
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # use the CLI login
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           timeout=timeout or self.timeout, env=env, cwd=os.path.expanduser("~"))
        if p.returncode != 0:
            raise RuntimeError(f"claude -p failed ({p.returncode}): {(p.stderr or p.stdout).strip()[:300]}")
        try:
            out = json.loads(p.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"claude -p: unexpected output {p.stdout[:200]!r}")
        if out.get("is_error"):
            raise RuntimeError(f"claude -p: {str(out.get('result'))[:300]}")
        return str(out.get("result", ""))


class FakeTagger:
    """Deterministic stand-in for tests: [sighs] first, the first question starting "Why"/"Did" in <emphasis>
    (one span), [pause] after each "?"."""
    name = "fake"

    def tag(self, text: str, context: dict) -> str:
        if "BREAK-THE-GUARD" in text:
            return text.replace("BREAK-THE-GUARD", "changed")
        text = re.sub(r"(?<![\w<])(Why|Did)\b(.*?\?)", r"<emphasis>\1\2</emphasis>", text, count=1)
        return "[sighs] " + re.sub(r"\?(</emphasis>)?(\s+)(?=\S)", r"?\1 [pause]\2", text)


class NoTagger:
    name = "none"

    def tag(self, text: str, context: dict) -> str:
        return text


AUTH_PROBLEM = re.compile(r"log ?in|logged|auth|credential|api key|subscription", re.I)


class ClaudeOrNone:
    """The Claude tagger when `claude` is installed and logged in, else no tagging (plain text). Checked once at
    start in the background (one tiny `claude -p` call), and again whenever a call fails with a login problem."""

    def __init__(self, claude: ClaudeCliTagger, probe: bool = True):
        self.claude = claude
        self.problem: str | None = None
        if not shutil.which(claude.binary):
            self._off(f"`{claude.binary}` not found")
        elif probe:
            threading.Thread(target=self._probe, name="claude-probe", daemon=True).start()

    @property
    def name(self) -> str:
        return f"none (claude unavailable: {self.problem})" if self.problem else "claude"

    def _off(self, why: str):
        if not self.problem:
            print(f"[tagger] no tagging: {why}. Install Claude Code and log in (run `claude` once), then restart for expressive "
                  f"markup.", file=sys.stderr, flush=True)
        self.problem = why

    def _probe(self):
        try:
            self.claude.tag("OK.", {})
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            self._off("not logged in" if AUTH_PROBLEM.search(msg) else msg[:120])

    def tag(self, text: str, context: dict) -> str:
        if self.problem:
            return text
        try:
            return self.claude.tag(text, context)
        except Exception as e:
            if AUTH_PROBLEM.search(str(e)):
                self._off("not logged in")
                return text
            raise


def make_tagger(cfg: dict) -> Tagger:
    kind = os.environ.get("FISH_READER_TAGGER") or cfg.get("tagger", "claude")
    if kind == "fake":
        return FakeTagger()
    if kind == "none":
        return NoTagger()
    if kind == "claude":
        return ClaudeOrNone(ClaudeCliTagger(model=cfg.get("claude_model", "sonnet"),
                                            binary=cfg.get("claude_binary", "claude")))
    raise ValueError(f"unknown tagger {kind!r} (claude | fake | none)")


def tag_checked(tagger: Tagger, text: str, context: dict) -> tuple[str, list[str]]:
    """(tagged text, warnings). Falls back to `text` when the tagger fails or changes words twice."""
    problem = ""
    for attempt in range(2):
        try:
            out = tagger.tag(text, context)
        except Exception as e:  # noqa: BLE001 - any tagger failure falls back to plain text
            problem = str(e)
            continue
        problem = tr.guard(text, out) or ""
        if not problem:
            return re.sub(r"[ \t]+", " ", out).strip(), []
    return text, [f"tagging skipped for this part ({tagger.name}: {problem})"]


# --- podcast scripts ---

PODCAST_WORDS = {"short": 700, "medium": 1400, "long": 2800}  # ~5, 10, 20 minutes

PODCAST_PROMPT = """You write the script of a two-host audio conversation about one article, for an expressive \
text-to-speech model (Fish Audio Drama 3) that performs it with two voices. It must sound like two friends who \
are genuinely into the topic talking it through, unscripted: NOT like two people reading prepared lines.
Hosts: A has read the article closely; B has skimmed it and is curious, quick and a bit skeptical.

How real conversation sounds (do all of this):
- Mostly short turns. Explanations come out in pieces, drawn out by B's questions and reactions, never as a \
paragraph-long monologue. Now and then a longer turn when someone gets going on a point.
- Backchannels and reactions as their own short turns: "Mm-hm.", "Yeah.", "Right, right.", "Oh, wow.", "Huh.", \
"Wait, what?", "No way."
- Natural disfluency, used lightly: "um", "uh", "I mean", "you know", "like", "so…", restarts ("It's, it's \
not that simple"), trailing off ("And that's kind of…"), a word repeated for emphasis.
- Interruptions and finishing each other's thoughts: end a turn with an em dash where the other cuts in \
("So the clerks just—" / "Lost their jobs? No, actually…").
- Real feeling: surprise, amusement, mild disagreement, an "aha" moment, a joke or a bit of teasing between the \
hosts, a callback to something said earlier. Let the energy rise and fall.
- Plain spoken words: contractions, no written-style phrases ("Furthermore", "In conclusion"), numbers said \
the way people say them. The hosts never call each other by name and never mention a script, show or episode.

Delivery markup (this is how the emotion reaches the voices, so use it: roughly every second or third turn):
- Span tags wrap the words said a certain way, then the voice returns to normal: {spans}. They can't nest and \
close within the turn. E.g. "<laughing>That's ridiculous.</laughing>", "<excited>Okay, this is the good \
part.</excited>", "<sarcastic>Great, more meetings.</sarcastic>".
- Point cues mark a sound or a beat at one spot: {points}. E.g. "[laughs] Fair.", "Hm. [pause] I'm not sure \
that follows."

Content:
- Stay faithful to the article. No facts, numbers, names, quotes or claims that aren't in it; say when \
something is the author's view. Opinions and reactions are the hosts' own and sound like it ("I find that kind \
of wild"). If the article is thin on something, the hosts can say so.
- Open straight into it with a hook (no "welcome to the show"), naming the subject and the source if given. Cover \
the main points in a sensible order, with tangents kept short. End with a quick, natural wrap-up.
- No stage directions, sound effects or anything in parentheses: only what is said, plus the markup above.

Format: one turn per line, starting with "A: " or "B: ". Nothing else: no title, no notes, no markdown.
Length: about {words} words in total."""


class ClaudeScriptWriter:
    """Writes a podcast script with `claude -p`."""

    def __init__(self, claude: ClaudeCliTagger):
        self.claude = claude

    def write(self, article: dict, length: str) -> str:
        head = [f"Title: {article['title']}" if article.get("title") else "",
                f"Source: {article['site']}" if article.get("site") else "",
                f"Author: {article['byline']}" if article.get("byline") else ""]
        body = "\n\n".join(article["paragraphs"])[:80_000]
        prompt = "\n".join(h for h in head if h) + "\n\n<article>\n" + body + "\n</article>"
        system = PODCAST_PROMPT.format(spans=_spans(), points=_points(), words=PODCAST_WORDS.get(length, 1400))
        return self.claude.ask(system, prompt, timeout=600)


class FakeScriptWriter:
    """For tests: the article's sentences, alternating between A and B, with a cue or two."""

    def write(self, article: dict, length: str) -> str:
        sentences = [s for p in article["paragraphs"] for s in re.split(r"(?<=[.!?])\s+", p) if s.strip()]
        lines = [f"{'AB'[i % 2]}: {s}" for i, s in enumerate(sentences)]
        if lines:
            lines[0] += " [laughs]"
        if len(lines) > 1:
            lines[1] = "B: <excited>" + lines[1][3:] + "</excited>"
        return "Here's the script:\n" + "\n".join(lines)


def make_writer(tagger: Tagger):
    """The podcast script writer that goes with the tagger: Claude when it's available, None when not."""
    if isinstance(tagger, FakeTagger):
        return FakeScriptWriter()
    if isinstance(tagger, ClaudeOrNone):
        return ClaudeScriptWriter(tagger.claude)
    return None
