"""fish-reader server: turns article URLs into Drama 3 audio (the model runs in this process). API: docs/API.md.

Binds 127.0.0.1 by default; reach it from the phone with `tailscale serve`. Every /api request needs
`Authorization: Bearer <token>` (config.json `token`, generated on first run).
"""
from __future__ import annotations

import argparse
import warnings
warnings.filterwarnings("ignore", message="SOCKS support")
import hmac
import json
import os
import re
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fish_core as core  # noqa: E402
from engine import Engine  # noqa: E402
from pipeline import Reader, StepError  # noqa: E402
from taggers import make_tagger  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = {"host": "127.0.0.1", "port": 8767, "data_dir": "./data", "voices_dir": None, "voice": None,
            "idle_unload_minutes": 15, "warm_on_start": False, "continuation": False, "podcast_hosts": [], "tagger": "claude", "claude_model": "sonnet",
            "token": None}
MAX_BODY = 6 * 1024 * 1024  # a shared page (html) can be large


def load_config(path: Path) -> dict:
    cfg = dict(DEFAULTS)
    if path.exists():
        cfg.update(json.loads(path.read_text()))
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(24)
        path.write_text(json.dumps({k: v for k, v in cfg.items()}, indent=2) + "\n")
        os.chmod(path, 0o600)
    return cfg


class Handler(BaseHTTPRequestHandler):
    server_version = "fish-reader/0.1"
    reader: Reader
    engine: Engine
    token: str
    tagger_name: str
    quiet = False

    def log_message(self, fmt, *args):
        if not self.quiet:
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # --- helpers ---

    def send_json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def error(self, code: int, msg: str):
        self.send_json({"error": msg}, code)

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise StepError("request body too large")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise StepError("body must be JSON") from None
        if not isinstance(data, dict):
            raise StepError("body must be a JSON object")
        return data

    def authed(self) -> bool:
        got = self.headers.get("Authorization", "")
        if got.startswith("Bearer ") and hmac.compare_digest(got[7:].strip().encode(), self.token.encode()):
            return True
        self.error(401, "missing or wrong token")
        return False

    def send_file(self, path: Path, ctype: str):
        if not path.is_file():
            return self.error(404, "not ready")
        size = path.stat().st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng or "")
        if rng and m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 16, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def send_live(self, aid: str, index: int):
        """Part `index` as it renders: 16-bit mono PCM from the start of the part, ending when the part does.
        Waits (up to 2 min) for the part to start; 409 when it's already finished (play its m4a instead)."""
        live = self.reader.live(aid, index)
        if live is None:
            a = self.reader.get(aid)
            done = bool(a and index < len(a["parts"]) and a["parts"][index]["status"] == "done")
            return self.error(409 if done else 404, "part is ready: play its audio file" if done else "no live audio for this part")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("X-Sample-Rate", str(live.sample_rate))
        self.send_header("X-Channels", "1")
        self.send_header("X-Sample-Format", "s16le")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        pos = 0
        try:
            while (chunk := live.read(pos)) is not None and chunk:
                self.wfile.write(chunk)
                self.wfile.flush()
                pos += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the phone stopped listening

    # --- routes ---

    def route(self):
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/"):
            return self.error(404, "not found")
        if not self.authed():
            return
        m = self.command, path
        try:
            if m == ("GET", "/api/health"):
                return self.send_json({"ok": True, "model": self.engine.status(), "tagger": self.reader.tagger.name})
            if m == ("GET", "/api/voices"):
                return self.send_json({"voices": [{k: v[k] for k in ("id", "name", "description")}
                                                  for v in self.engine.voices() if v["has_transcript"]]})
            if m == ("GET", "/api/articles"):
                return self.send_json({"articles": [summary(a) for a in self.reader.list()]})
            if m == ("POST", "/api/articles"):
                b = self.body()
                url = b.get("url")
                if not isinstance(url, str) or not url.strip():
                    return self.error(400, "url is required")
                voice, page = b.get("voice"), b.get("html")
                if page is not None and (not isinstance(page, str) or not page.strip()):
                    return self.error(400, "html must be the page source")
                hosts = b.get("hosts")
                return self.send_json(self.reader.add(url, voice if isinstance(voice, str) and voice else None, page,
                                                      kind=b.get("kind") or "article",
                                                      hosts=hosts if isinstance(hosts, list) else None,
                                                      length=b.get("length") or "medium"), 201)
            am = re.fullmatch(r"/api/articles/([\w-]+)(/.*)?", path)
            if am:
                aid, rest = am.group(1), am.group(2) or ""
                a = self.reader.get(aid)
                if not a:
                    return self.error(404, "no such article")
                if rest == "" and self.command == "GET":
                    return self.send_json(a)
                if rest == "" and self.command == "DELETE":
                    self.reader.delete(aid)
                    return self.send_json({"removed": aid})
                if rest == "/retry" and self.command == "POST":
                    return self.send_json(self.reader.retry(aid))
                if rest == "/pause" and self.command == "POST":
                    return self.send_json(self.reader.pause(aid))
                if rest == "/resume" and self.command == "POST":
                    return self.send_json(self.reader.resume(aid))
                lm = re.fullmatch(r"/parts/(\d+)/live", rest)
                if lm and self.command == "GET":
                    return self.send_live(aid, int(lm.group(1)))
                if self.command in ("GET", "HEAD"):
                    d = self.reader.dir(aid)
                    if rest == "/audio.m4a":
                        return self.send_file(d / "audio.m4a", "audio/mp4")
                    pm = re.fullmatch(r"/parts/(\d+)\.m4a", rest)
                    if pm:
                        return self.send_file(d / "parts" / f"{int(pm.group(1)):02d}.m4a", "audio/mp4")
            return self.error(404, "not found")
        except (StepError, core.RequestError) as e:
            return self.error(400, str(e))

    do_GET = do_POST = do_DELETE = do_HEAD = route


def summary(a: dict) -> dict:
    return {k: a.get(k) for k in ("id", "url", "title", "site", "byline", "status", "error", "created", "updated",
                                  "progress", "duration_s", "audio_url", "voice", "kind", "hosts", "host_names",
                                  "length")}


def main():
    p = argparse.ArgumentParser(description="fish-reader server")
    p.add_argument("--config", default=os.environ.get("FISH_READER_CONFIG") or str(ROOT / "config.json"))
    p.add_argument("--port", type=int)
    p.add_argument("--host", help="bind address (default 127.0.0.1; use tailscale serve instead of 0.0.0.0)")
    p.add_argument("--data-dir")
    p.add_argument("--voices-dir")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()
    cfg = load_config(Path(args.config))
    for k in ("port", "host", "data_dir", "voices_dir"):
        if getattr(args, k) is not None:
            cfg[k] = getattr(args, k)
    data_dir = Path(cfg["data_dir"])
    if not data_dir.is_absolute():
        data_dir = (ROOT / data_dir).resolve()
    voices_dir = Path(cfg["voices_dir"]).expanduser() if cfg.get("voices_dir") else data_dir / "voices"
    if not voices_dir.is_absolute():
        voices_dir = (ROOT / voices_dir).resolve()
    engine = Engine(voices_dir, cfg.get("idle_unload_minutes", 15))
    if cfg.get("warm_on_start"):
        engine.warm()
    tagger = make_tagger(cfg)
    Handler.reader = Reader(data_dir, engine, tagger, cfg.get("voice"), bool(cfg.get("continuation", False)),
                            cfg.get("podcast_hosts") or [])
    Handler.engine, Handler.token, Handler.tagger_name, Handler.quiet = engine, cfg["token"], tagger.name, args.quiet
    httpd = ThreadingHTTPServer((cfg["host"], int(cfg["port"])), Handler)
    httpd.daemon_threads = True
    print(f"fish-reader on http://{cfg['host']}:{cfg['port']}/  (Drama 3{' FAKE' if core.FAKE else ''}, tagger "
          f"{tagger.name}, data {data_dir}, voices {voices_dir}; token in {args.config})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
