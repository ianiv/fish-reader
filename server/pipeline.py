"""Article pipeline: fetch -> extract -> split -> tag (ahead) -> render on fish-ui -> package (m4a).

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

import text_rules as tr
from fish_client import FishClient, FishError
from taggers import Tagger, tag_checked

ACTIVE = ("queued", "fetching", "extracting", "tagging", "rendering")
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
    """Article store plus one worker thread that runs articles in order (fish-ui renders one job at a time)."""

    def __init__(self, data_dir: Path, fish: FishClient, tagger: Tagger, voice: str | None = None):
        self.root = Path(data_dir) / "articles"
        self.root.mkdir(parents=True, exist_ok=True)
        self.fish, self.tagger, self.default_voice = fish, tagger, voice
        self.lock = threading.RLock()
        self.wake = threading.Condition(self.lock)
        self.articles: dict[str, dict] = {}
        self.cancelled: set[str] = set()
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

    def add(self, url: str, voice: str | None = None, html: str | None = None) -> dict:
        """html: the page as the phone saw it (Safari share sheet, signed in), used instead of fetching."""
        if html is None:
            check_url(url)
        elif not urllib.parse.urlsplit(url.strip()).scheme in ("http", "https"):
            raise StepError("only http(s) URLs can be read")
        a = {"id": new_id(), "url": url.strip(), "title": "", "byline": "", "site": "", "date": "",
             "status": "queued", "step": None, "error": None, "warnings": [], "created": time.time(),
             "voice": voice if voice is not None else self.default_voice, "tagger": self.tagger.name,
             "source": "fetched", "parts": [], "duration_s": None, "audio_url": None,
             "progress": {"parts_done": 0, "parts_total": 0}}
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
        """Load Drama 3 now (if fish-ui unloaded it) so loading overlaps fetching and tagging."""
        try:
            self.fish.warm()
        except FishError:
            pass

    def live_job(self, aid: str, index: int, timeout: float = 120) -> str | None:
        """The fish-ui job rendering part `index`, waiting up to `timeout` s for it to start. None when the part
        is already done (play its m4a), the article is gone, or it failed / timed out."""
        deadline = time.time() + timeout
        with self.lock:
            while True:
                a = self.articles.get(aid)
                if not a or a["status"] in ("error", "cancelled") or (a["parts"] and index >= len(a["parts"])):
                    return None
                if a["parts"]:  # (no parts yet while fetching / extracting: wait)
                    p = a["parts"][index]
                    if p["status"] == "done":
                        return None
                    if p.get("fish_job") and p["status"] == "rendering":
                        return p["fish_job"]
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
                self.update(a, status="queued", error=None)
                self.wake.notify_all()
        return self.get(aid)

    def delete(self, aid: str) -> bool:
        with self.lock:
            a = self.articles.pop(aid, None)
            if not a:
                return False
            self.cancelled.add(aid)
            job = a.get("fish_job")
        if job:
            self.fish.cancel(job)
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
                pass
            except (StepError, FishError) as e:
                self._fail(a, str(e))
            except Exception as e:  # noqa: BLE001
                self._fail(a, f"{type(e).__name__}: {e}")

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
            # The title and byline are a tiny untagged first part, so rendering (and the phone's live stream)
            # starts right after extraction while Claude tags the short first body part.
            intro = ([art["title"] + ("" if re.search(r"[.!?…]$", art["title"]) else ".")] if art["title"] else [])
            if art["byline"]:
                intro.append(f"By {art['byline']}.")
            intro = " ".join(intro)
            texts = ([intro] if intro else []) + tr.split_parts(art["paragraphs"], first_limit=tr.FIRST_PART_CHARS)
            self.update(a, title=art["title"] or a["url"], byline=art["byline"], site=art["site"], date=art["date"],
                        parts=[{"index": i, "text": t, "tagged": t if (intro and i == 0) else None,
                                "status": "tagged" if (intro and i == 0) else "pending", "take": None,
                                "fish_job": None, "duration_s": None, "start_s": None, "audio_url": None,
                                "warnings": []}
                               for i, t in enumerate(texts)],
                        progress={"parts_done": 0, "parts_total": len(texts)})
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

    def _render_all(self, a: dict):
        d = self.dir(a["id"])
        (d / "parts").mkdir(exist_ok=True)
        parts = a["parts"]
        futures = {i: self.tag_pool.submit(self._tag, a, i) for i, p in enumerate(parts) if p["tagged"] is None}
        if futures:
            self.update(a, status="tagging", step="tag")
        for i, p in enumerate(parts):
            self._check(a)
            if i in futures:
                futures[i].result()
            if p["status"] == "done" and (d / "parts" / f"{i:02d}.m4a").exists():
                continue
            self.update(a, status="rendering", step="render")
            prev = parts[i - 1] if i else None
            history = [prev["take"]] if prev and prev.get("take") and (prev["duration_s"] or 99) <= tr.MAX_HISTORY_S else []
            with self.lock:
                p["status"] = "rendering"
            try:
                job = self.fish.submit_tts(p["tagged"] or p["text"], a.get("voice"), history, stream=True)
            except FishError as e:
                if history and "previous" in str(e):  # e.g. the take was deleted in fish-ui: go on without it
                    job = self.fish.submit_tts(p["tagged"] or p["text"], a.get("voice"), stream=True)
                    p["warnings"].append("couldn't continue from the previous part")
                else:
                    raise
            with self.lock:
                p["fish_job"] = job["id"]
                self.update(a, fish_job=job["id"])
                self.wake.notify_all()  # wakes live_job() waiters
            job = self.fish.wait(job["id"], should_stop=lambda: a["id"] in self.cancelled)
            self._check(a)
            if job["status"] != "done" or not job.get("outputs"):
                raise StepError(f"fish-ui job {job['status']}: {job.get('error') or 'no audio'} (part {i + 1})")
            take = job["outputs"][0]
            wav = d / "parts" / f"{i:02d}.wav"
            self.fish.download(take["url"], wav)
            to_m4a(wav, d / "parts" / f"{i:02d}.m4a", f"{a['title']} ({i + 1}/{len(parts)})")
            with self.lock:
                p.update(status="done", take=take["id"], duration_s=round(wav_duration(wav), 3),
                         audio_url=f"/api/articles/{a['id']}/parts/{i}.m4a",
                         warnings=p["warnings"] + list(job.get("warnings") or []))
                self._times(a)
                a["progress"] = {"parts_done": sum(q["status"] == "done" for q in parts), "parts_total": len(parts)}
                a["fish_job"] = None
                self._save(a)
                self.wake.notify_all()
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
