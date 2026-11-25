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
        cmd = [self.binary, "-p", "--output-format", "json", "--model", self.model,
               "--system-prompt", SYSTEM_PROMPT.format(spans=", ".join(f"<{t}>…</{t}>" for t in tr.SPAN_TAGS),
                                                       points=", ".join(f"[{c}]" for c in tr.POINT_CUES)),
               "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence"]
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # use the CLI login
        p = subprocess.run(cmd, input=_prompt(text, context), capture_output=True, text=True,
                           timeout=self.timeout, env=env, cwd=os.path.expanduser("~"))
        if p.returncode != 0:
            raise RuntimeError(f"claude -p failed ({p.returncode}): {(p.stderr or p.stdout).strip()[:300]}")
        try:
            out = json.loads(p.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"claude -p: unexpected output {p.stdout[:200]!r}")
        if out.get("is_error"):
            raise RuntimeError(f"claude -p: {str(out.get('result'))[:300]}")
        return _clean_answer(str(out.get("result", "")))


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
