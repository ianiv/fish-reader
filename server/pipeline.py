"""Article pipeline: fetch -> extract -> split -> tag (ahead) -> render with Drama 3 (engine.py) -> package (m4a).

An article lives in <data>/articles/<id>/: article.json (state, below), source.html, parts/NN.wav (until the
article is joined), parts/NN.m4a, audio.m4a. Each step is skipped when its result is already there, so a retry
or a restart picks up where it stopped.
"""
from __future__ import annotations

import html as htmllib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fish_core as core
import text_rules as tr
from engine import Engine, EngineError, Live, write_wav
from taggers import PODCAST_WORDS, ClaudeOrNone, Tagger, make_writer, tag_checked

ACTIVE = ("queued", "fetching", "extracting", "scripting", "tagging", "rendering")
KINDS = ("article", "podcast")
MAX_BYTES = 5 * 1024 * 1024
MIN_CHARS = 300
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
      "Version/26.0 Safari/605.1.15")


class StepError(RuntimeError):
    pass


class Cancelled(Exception):
    pass


# ---------- fetch ----------

def _allowed(u: urllib.parse.SplitResult) -> bool:
    """Tests only: FISH_READER_ALLOW_HOSTS="127.0.0.1:8123,…" lets those exact host:port pairs through."""
    allow = [h.strip() for h in os.environ.get("FISH_READER_ALLOW_HOSTS", "").split(",") if h.strip()]
    return f"{u.hostname}:{u.port}" in allow


def check_url(url: str) -> str:
    """http(s) URLs whose host resolves only to public addresses (the server fetches what the phone sends)."""
    u = urllib.parse.urlsplit(url.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise StepError("only http(s) URLs can be read")
    if _allowed(u):
        return url.strip()
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise StepError(f"can't resolve {u.hostname}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise StepError(f"{u.hostname} is a private or local address")
    return url.strip()


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str) -> tuple[str, str]:
    """(final url, html text)."""
    check_url(url)
    opener = urllib.request.build_opener(_Redirects)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml",
                                               "Accept-Language": "en;q=0.9,*;q=0.5"})
    try:
        with opener.open(req, timeout=15) as r:
            ctype = r.headers.get("Content-Type", "")
            if ctype and "html" not in ctype and "xml" not in ctype:
                raise StepError(f"not a web page ({ctype.split(';')[0]})")
            body = r.read(MAX_BYTES + 1)
            final = r.geturl()
            charset = r.headers.get_content_charset() or "utf-8"
    except StepError:
        raise
    except urllib.error.HTTPError as e:
        raise StepError(f"the site answered {e.code} {e.reason}") from None
    except Exception as e:  # noqa: BLE001
        raise StepError(f"couldn't fetch the page ({getattr(e, 'reason', e)})") from None
    if len(body) > MAX_BYTES:
        raise StepError("the page is larger than 5 MB")
    return final, body.decode(charset, errors="replace")


# ---------- extract ----------

def clean_paragraph(p: str) -> str:
    p = htmllib.unescape(p)
    p = re.sub(r"https?://\S+|www\.\S+", "", p)             # bare links
    p = re.sub(r"\[[^\]]{0,40}\]", "", p)                  # footnote markers / [edit]; brackets would be cues
    p = re.sub(r"</?[A-Za-z][\w-]*[^>]*>", "", p)          # stray markup would read as a span tag
    p = p.replace("<|", " ").replace("|>", " ")            # never a structural token
    p = re.sub(r"(\*\*|__|(?<!\w)[*_]|[*_](?!\w))", "", p)  # markdown emphasis
    p = re.sub(r"\s+", " ", p).strip()
    return p


_CITATION = re.compile(r"\b(Retrieved|Archived from|ISBN|doi:|pp?\.\s*\d|Accessed|et al\.)", re.I)
_NAME_WORDS = {"and", "de", "van", "von", "der", "la", "le", "du", "da", "di", "bin", "al"}


def drop_lists(lines: list[str]) -> list[str]:
    """Keep short bulleted lists (they're usually part of the story), drop long runs of bullets (filmographies,
    link lists, bibliographies) and anything that reads as a citation."""
    out, run = [], []

    def flush():
        if run and len(run) <= 6:
            out.extend(r.lstrip("-•* ").strip() for r in run)
        run.clear()

    for line in lines:
        s = line.strip()
        if _CITATION.search(s):
            continue
        if re.match(r"^[-•*]\s", s):
            run.append(s)
        else:
            flush()
            out.append(s)
    flush()
    return out


def plausible_byline(author: str) -> str:
    """trafilatura's author, when it looks like person names (<= 8 words, capitalised): else ""."""
    words = author.replace(";", ",").replace(",", " , ").split()
    names = [w for w in words if w != ","]
    if not names or len(names) > 8 or any(w[0].islower() and w.lower() not in _NAME_WORDS for w in names):
        return ""
    return re.sub(r"\s+,", ",", " ".join(words)).replace(" ,", ",")


def extract(html: str, url: str) -> dict:
    """{title, byline, site, date, paragraphs: [str]}; the title is not in paragraphs."""
    import trafilatura  # slow import; only the pipeline needs it

    text = trafilatura.extract(html, url=url, output_format="txt", include_comments=False, include_tables=False,
                               include_images=False, include_links=False, favor_precision=True, deduplicate=True)
    if not text or len(text) < MIN_CHARS:
        text = trafilatura.extract(html, url=url, output_format="txt", include_comments=False,
                                   include_tables=False, favor_recall=True) or text
    meta = trafilatura.extract_metadata(html, default_url=url)
    paragraphs = [c for c in (clean_paragraph(p) for p in drop_lists((text or "").split("\n"))) if c]
    # drop short trailing junk such as "Share this article" / "Advertisement"
    paragraphs = [p for p in paragraphs if len(p) > 25 or re.search(r"[.!?…\"”]$", p)]
    total = sum(len(p) for p in paragraphs)
    if total < MIN_CHARS:
        raise StepError("couldn't find article text on this page (it may need a login or JavaScript)")
    title = clean_paragraph(meta.title) if meta and meta.title else ""
    if paragraphs and title and paragraphs[0].rstrip(".") == title.rstrip("."):
        paragraphs = paragraphs[1:]
    return {"title": title, "byline": plausible_byline(clean_paragraph(meta.author)) if meta and meta.author else "",
            "site": (meta.sitename if meta and meta.sitename else urllib.parse.urlsplit(url).hostname) or "",
            "date": (meta.date if meta else "") or "", "paragraphs": paragraphs}


# ---------- audio ----------

def wav_duration(path: Path) -> float:
    import wave
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def to_m4a(src: Path, dest: Path, title: str = ""):
    tmp = dest.with_suffix(".tmp.m4a")
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(src), "-c:a", "aac", "-b:a", "96k",
           "-movflags", "+faststart"]
    if title:
        cmd += ["-metadata", f"title={title}"]
    subprocess.run(cmd + [str(tmp)], check=True)
    tmp.replace(dest)


def join_m4a(wavs: list[Path], dest: Path, title: str = "", artist: str = ""):
    listing = dest.parent / "concat.txt"
    listing.write_text("".join(f"file '{w.resolve()}'\n" for w in wavs))
    tmp = dest.with_suffix(".tmp.m4a")
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
           "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart"]
    if title:
        cmd += ["-metadata", f"title={title}"]
    if artist:
        cmd += ["-metadata", f"artist={artist}"]
    try:
        subprocess.run(cmd + [str(tmp)], check=True)
        tmp.replace(dest)
    finally:
        listing.unlink(missing_ok=True)


# ---------- store + worker ----------

def new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)


class Reader:
    """Article store plus one worker thread that runs articles in order (the engine renders one take at a time)."""

    def __init__(self, data_dir: Path, engine: Engine, tagger: Tagger, voice: str | None = None,
                 continuation: bool = False, hosts: list[str] | None = None):
        self.root = Path(data_dir) / "articles"
        self.root.mkdir(parents=True, exist_ok=True)
        self.engine, self.tagger, self.default_voice = engine, tagger, voice
        # continue each part from the previous one (carries voice and delivery over; can also carry emotion
        # along and build it up). Without a voice it's always on: it's what keeps the narrator the same.
        self.continuation = continuation
        self.writer = make_writer(tagger)  # podcast scripts
        self.default_hosts = hosts or []
        self.lives: dict[tuple[str, int], Live] = {}  # parts rendering now
        self.lock = threading.RLock()
        self.wake = threading.Condition(self.lock)
        self.articles: dict[str, dict] = {}
        self.cancelled: set[str] = set()
        self.pausing: set[str] = set()  # cancelled to pause (keep the article), not to delete
        self.tag_pool = ThreadPoolExecutor(1, thread_name_prefix="tagger")
        for f in sorted(self.root.glob("*/article.json")):
            try:
                a = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if a.get("status") in ACTIVE:
                a["status"] = "queued"  # resume after a restart
            self.articles[a["id"]] = a
        threading.Thread(target=self._loop, name="reader-worker", daemon=True).start()

    # --- state ---

    def dir(self, aid: str) -> Path:
        return self.root / aid

    def _save(self, a: dict):
        d = self.dir(a["id"])
        d.mkdir(parents=True, exist_ok=True)
        a["updated"] = time.time()
        tmp = d / "article.json.tmp"
        tmp.write_text(json.dumps(a, ensure_ascii=False, indent=1))
        tmp.replace(d / "article.json")

    def update(self, a: dict, **kw):
        with self.lock:
            a.update(kw)
            self._save(a)

    def get(self, aid: str) -> dict | None:
        with self.lock:
            a = self.articles.get(aid)
            return json.loads(json.dumps(a)) if a else None

    def list(self) -> list[dict]:
        with self.lock:
            return sorted((json.loads(json.dumps(a)) for a in self.articles.values()),
                          key=lambda a: a["created"], reverse=True)

    def add(self, url: str, voice: str | None = None, html: str | None = None, kind: str = "article",
            hosts: list[str] | None = None, length: str = "medium") -> dict:
        """html: the page as the phone saw it (Safari share sheet, signed in), used instead of fetching.
        kind "podcast": Claude writes a two-host conversation about the article, read by `hosts` (two voice ids;
        default config podcast_hosts), about `length` long (short | medium | long)."""
        if kind not in KINDS:
            raise StepError(f"kind must be one of {', '.join(KINDS)}")
        if kind == "podcast":
            hosts = hosts or self.default_hosts
            if not isinstance(hosts, list) or len(hosts) != 2 or not all(isinstance(h, str) and h for h in hosts):
                raise StepError("a podcast needs two host voices")
            for h in hosts:
                v = self.engine.voice(h)  # raises for a missing voice
                if not v.transcript:
                    raise StepError(f"voice '{h}' has no transcript")
            if length not in PODCAST_WORDS:
                raise StepError(f"length must be one of {', '.join(PODCAST_WORDS)}")
            if self.writer is None:
                raise StepError("podcasts need the Claude tagger (tagger: claude in config.json)")
        if html is None:
            check_url(url)
        elif not urllib.parse.urlsplit(url.strip()).scheme in ("http", "https"):
            raise StepError("only http(s) URLs can be read")
        a = {"id": new_id(), "url": url.strip(), "title": "", "byline": "", "site": "", "date": "",
             "status": "queued", "step": None, "error": None, "warnings": [], "created": time.time(),
             "voice": voice if voice is not None else self.default_voice, "tagger": self.tagger.name,
             "source": "fetched", "parts": [], "duration_s": None, "audio_url": None,
             "progress": {"parts_done": 0, "parts_total": 0}, "kind": kind}
        if kind == "podcast":
            names = {v["id"]: v["name"] for v in self.engine.voices()}
            a.update(voice=None, hosts=hosts, host_names=[names.get(h, h) for h in hosts], length=length)
        threading.Thread(target=self._warm, daemon=True).start()
        with self.lock:
            self._save(a)
            if html is not None:
                (self.dir(a["id"]) / "source.html").write_text(html)
                a["source"] = "shared"
            self.articles[a["id"]] = a
            self._save(a)
            self.wake.notify_all()
        return self.get(a["id"])

    def _warm(self):
        """Load Drama 3 now (if it isn't loaded) so loading overlaps fetching and tagging."""
        self.engine.warm()

    def live(self, aid: str, index: int, timeout: float = 120) -> Live | None:
        """Part `index`'s live audio, waiting up to `timeout` s for it to start rendering. None when the part
        is already done (play its m4a), the article is gone, or it failed / timed out."""
        deadline = time.time() + timeout
        with self.lock:
            while True:
                a = self.articles.get(aid)
                if not a or a["status"] in ("error", "cancelled", "paused") or (a["parts"] and index >= len(a["parts"])):
                    return None
                if a["parts"]:  # (no parts yet while fetching / extracting: wait)
                    p = a["parts"][index]
                    if p["status"] == "done":
                        return None
                    live = self.lives.get((aid, index))
                    if live and p["status"] == "rendering":
                        return live
                left = deadline - time.time()
                if left <= 0:
                    return None
                self.wake.wait(min(left, 1.0))

    def retry(self, aid: str) -> dict | None:
        with self.lock:
            a = self.articles.get(aid)
            if not a:
                return None
            if a["status"] not in ACTIVE:
                self.cancelled.discard(aid)
                self.pausing.discard(aid)
                self.update(a, status="queued", error=None)
                self.wake.notify_all()
        return self.get(aid)

    def pause(self, aid: str) -> dict | None:
        """Stop working on an article for now (frees the GPU): a running render stops within a moment and that
        part starts over on resume; finished parts are kept."""
        with self.lock:
            a = self.articles.get(aid)
            if not a:
                return None
            if a["status"] == "queued":
                self.update(a, status="paused")
            elif a["status"] in ACTIVE:
                self.pausing.add(aid)
                self.cancelled.add(aid)
        return self.get(aid)

    def resume(self, aid: str) -> dict | None:
        with self.lock:
            a = self.articles.get(aid)
            if a and a["status"] == "paused":
                self.pausing.discard(aid)
                self.cancelled.discard(aid)
                self.update(a, status="queued", error=None)
                self.wake.notify_all()
        return self.get(aid)

    def delete(self, aid: str) -> bool:
        with self.lock:
            a = self.articles.pop(aid, None)
            if not a:
                return False
            self.cancelled.add(aid)  # a running render stops after its current piece
        shutil.rmtree(self.dir(aid), ignore_errors=True)
        return True

    # --- worker ---

    def _next(self) -> dict | None:
        queued = [a for a in self.articles.values() if a["status"] == "queued"]
        return min(queued, key=lambda a: a["created"]) if queued else None

    def _loop(self):
        while True:
            with self.lock:
                a = self._next()
                while a is None:
                    self.wake.wait()
                    a = self._next()
            try:
                self.run(a)
            except Cancelled:
                self._paused(a)
            except (StepError, EngineError, core.RequestError) as e:
                self._fail(a, str(e))
            except Exception as e:  # noqa: BLE001
                self._fail(a, f"{type(e).__name__}: {e}")

    def _paused(self, a: dict):
        """After a Cancelled: if it was a pause, keep the article as paused (its unfinished part re-renders on
        resume) and unload the model when nothing else is waiting."""
        with self.lock:
            if a["id"] not in self.pausing or a["id"] not in self.articles:
                return
            self.pausing.discard(a["id"])
            self.cancelled.discard(a["id"])
            for p in a.get("parts") or []:
                if p["status"] == "rendering":
                    p["status"] = "tagged" if p.get("tagged") is not None else "pending"
            self.update(a, status="paused")
            idle = self._next() is None
        if idle:
            self.engine.unload()

    def _fail(self, a: dict, msg: str):
        with self.lock:
            if a["id"] in self.articles:
                self.update(a, status="error", error=msg)

    def _check(self, a: dict):
        if a["id"] in self.cancelled:
            raise Cancelled()

    def run(self, a: dict):
        d = self.dir(a["id"])
        self._check(a)
        if not a["parts"]:
            src = d / "source.html"
            if not src.exists():
                self.update(a, status="fetching", step="fetch")
                final, page = fetch(a["url"])
                src.write_text(page)
                self.update(a, final_url=final)
            self._check(a)
            self.update(a, status="extracting", step="extract")
            art = extract(src.read_text(), a.get("final_url") or a["url"])
            if a.get("kind") == "podcast":
                return self._script(a, art)
            # The title and byline are a tiny untagged first part, so rendering (and the phone's live stream)
            # starts right after extraction while Claude tags the short first body part.
            intro = ([art["title"] + ("" if re.search(r"[.!?…]$", art["title"]) else ".")] if art["title"] else [])
            if art["byline"]:
                intro.append(f"By {art['byline']}.")
            intro = " ".join(intro)
            texts = ([intro] if intro else []) + tr.split_parts(art["paragraphs"], first_limit=tr.FIRST_PART_CHARS)
            self.update(a, title=art["title"] or a["url"], byline=art["byline"], site=art["site"], date=art["date"],
                        parts=[{"index": i, "text": t, "tagged": t if (intro and i == 0) else None,
                                "status": "tagged" if (intro and i == 0) else "pending",
                                "continued": False, "gain": None, "duration_s": None, "start_s": None, "audio_url": None,
                                "warnings": []}
                               for i, t in enumerate(texts)],
                        progress={"parts_done": 0, "parts_total": len(texts)})
        self._render_all(a)

    def _script(self, a: dict, art: dict):
        """Podcast: Claude writes the conversation, which is cut into parts between turns, then rendered."""
        d = self.dir(a["id"])
        self.update(a, title=art["title"] or a["url"], byline=art["byline"], site=art["site"], date=art["date"],
                    status="scripting", step="script")
        if isinstance(self.tagger, ClaudeOrNone) and self.tagger.problem:
            raise StepError(f"podcasts need Claude: {self.tagger.problem}")
        try:
            script = self.writer.write(art, a.get("length") or "medium")
        except Exception as e:  # noqa: BLE001
            raise StepError(f"writing the script failed: {e}") from None
        self._check(a)
        (d / "script.txt").write_text(script)
        turns = tr.parse_script(script)
        if len(turns) < 2 or {s for s, _ in turns} != {0, 1}:
            raise StepError("Claude didn't write a usable two-host script (see script.txt)")
        names = a.get("host_names") or ["A", "B"]

        def show(t: list, tagged: bool) -> str:
            return "\n\n".join(f"{names[s]}: {x if tagged else re.sub(r'\s+', ' ', tr.strip_tags(x)).strip()}"
                                for s, x in t)

        groups = tr.split_turns(turns, limit=tr.PODCAST_PART_CHARS)
        self.update(a, parts=[{"index": i, "turns": [{"speaker": s, "text": x} for s, x in g],
                               "text": show(g, False), "tagged": show(g, True), "status": "tagged",
                               "continued": False, "gain": None, "duration_s": None, "start_s": None,
                               "audio_url": None, "warnings": []} for i, g in enumerate(groups)],
                    progress={"parts_done": 0, "parts_total": len(groups)})
        self._render_all(a)

    def _tag(self, a: dict, i: int) -> None:
        p = a["parts"][i]
        if p["tagged"] is not None:
            return
        ctx = {"title": a["title"], "before": a["parts"][i - 1]["text"] if i else ""}
        tagged, warnings = tag_checked(self.tagger, p["text"], ctx)
        with self.lock:
            p.update(tagged=tagged, warnings=p["warnings"] + warnings)
            if p["status"] == "pending":
                p["status"] = "tagged"
            self._save(a)

    SAMPLE_MIN_S = 8.0  # a voice sample shorter than this gives the model too little to go on

    def _voice_sample(self, a: dict, i: int) -> core.Voice | None:
        """Part i as the article's voice sample, when it's needed (no library voice, continuation off) and
        long enough."""
        p, wav = a["parts"][i], self.dir(a["id"]) / "parts" / f"{i:02d}.wav"
        if a.get("kind") == "podcast" or a.get("voice") or self.continuation \
                or (p.get("duration_s") or 0) < self.SAMPLE_MIN_S or not wav.exists():
            return None
        try:
            core.check_text(p["text"])
        except core.RequestError:
            return None
        return core.Voice(id=f"part {i}", audio=str(wav), transcript=p["text"])

    def _render_all(self, a: dict):
        d = self.dir(a["id"])
        (d / "parts").mkdir(exist_ok=True)
        parts = a["parts"]
        futures = {i: self.tag_pool.submit(self._tag, a, i) for i, p in enumerate(parts) if p["tagged"] is None}
        if futures:
            self.update(a, status="tagging", step="tag")
        # No library voice and continuation off: the first part long enough becomes the article's voice
        # sample (its audio + words), so the narrator stays the same without carrying emotion along. Parts up
        # to that one continue from the previous part instead.
        sample = None
        for i, p in enumerate(parts):
            self._check(a)
            if i in futures:
                futures[i].result()
            if p["status"] == "done" and (d / "parts" / f"{i:02d}.m4a").exists():
                sample = sample or self._voice_sample(a, i)
                continue
            self.update(a, status="rendering", step="render")
            prev = parts[i - 1] if i else None
            prev_wav = d / "parts" / f"{i - 1:02d}.wav"
            history = []
            voice = a.get("voice") or (None if self.continuation else sample)
            if a.get("kind") != "podcast" and (self.continuation or not voice) and prev and prev_wav.exists() \
                    and (prev["duration_s"] or 99) <= tr.MAX_HISTORY_S:
                history = [core.PrevTake(id=f"part {i - 1}", text=prev["tagged"] or prev["text"], wav=str(prev_wav),
                                         duration_s=prev["duration_s"], voices=[a["voice"]] if a.get("voice") else [],
                                         gain=prev.get("gain"))]
            live = Live()
            with self.lock:
                p["status"] = "rendering"
                self.lives[(a["id"], i)] = live
                self.wake.notify_all()  # wakes live() waiters
            try:
                if a.get("kind") == "podcast":
                    take, warnings = self.engine.render_dialogue([(t["speaker"], t["text"]) for t in p["turns"]],
                                                                 a["hosts"], live,
                                                                 should_cancel=lambda: a["id"] in self.cancelled)
                else:
                    take, warnings = self.engine.render(p["tagged"] or p["text"], voice, history, live,
                                                        should_cancel=lambda: a["id"] in self.cancelled)
            except core.Cancelled:
                raise Cancelled() from None
            except core.RequestError as e:
                raise StepError(f"part {i + 1}: {e}") from None
            finally:
                with self.lock:
                    self.lives.pop((a["id"], i), None)
            self._check(a)
            wav = d / "parts" / f"{i:02d}.wav"
            gain = write_wav(wav, take)
            to_m4a(wav, d / "parts" / f"{i:02d}.m4a", f"{a['title']} ({i + 1}/{len(parts)})")
            with self.lock:
                p.update(status="done", duration_s=round(wav_duration(wav), 3), gain=gain, continued=bool(history),
                         voice_sample=voice.id if isinstance(voice, core.Voice) else None,
                         audio_url=f"/api/articles/{a['id']}/parts/{i}.m4a", warnings=p["warnings"] + warnings)
                self._times(a)
                a["progress"] = {"parts_done": sum(q["status"] == "done" for q in parts), "parts_total": len(parts)}
                self._save(a)
                self.wake.notify_all()
            sample = sample or self._voice_sample(a, i)
        wavs = [d / "parts" / f"{i:02d}.wav" for i in range(len(parts))]
        if all(w.exists() for w in wavs):
            join_m4a(wavs, d / "audio.m4a", a["title"], a.get("site") or "")
            for w in wavs:
                w.unlink()
        warnings = sorted({w for p in parts for w in p["warnings"]})
        self.update(a, status="done", step=None, audio_url=f"/api/articles/{a['id']}/audio.m4a",
                    duration_s=round(sum(p["duration_s"] or 0 for p in parts), 3), warnings=warnings)

    @staticmethod
    def _times(a: dict):
        t = 0.0
        for p in a["parts"]:
            if p["duration_s"] is None:
                break
            p["start_s"] = round(t, 3)
            t += p["duration_s"]
