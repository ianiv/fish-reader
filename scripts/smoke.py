#!/usr/bin/env python3
"""fish-reader smoke test (no model weights, no Claude): unit checks of text_rules / taggers / extraction, then
fish-ui with FISH_UI_FAKE_MODEL=1 (from $FISH_UI_DIR, default ~/Code/fish-ui) and the reader with the fake
tagger, each on a random port with temp dirs, plus a local page server with a cluttered article fixture."""
from __future__ import annotations

import warnings
warnings.filterwarnings("ignore", message="SOCKS support")
import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
FISH_UI = Path(os.environ.get("FISH_UI_DIR") or Path.home() / "Code" / "fish-ui")

import pipeline  # noqa: E402
import taggers  # noqa: E402
import text_rules as tr  # noqa: E402

PARA = ("The harbour town woke slowly that morning, as it always did in late autumn, with gulls arguing over the "
        "fishing boats and the smell of coffee drifting from the bakery on the corner. ")
QUOTE = '"Did you really think we would forget you? Never," the captain said, laughing as she stepped onto the pier.'
ARTICLE = [PARA * 2, QUOTE, PARA * 3 + "Nobody had expected the storm[1] to pass so quickly.", PARA * 4,
           "In the end, the keeper said, it was the letters that kept him going. " * 3, PARA * 2]
FIXTURE = f"""<!doctype html><html><head><title>The Keeper Comes Home | Harbour News</title>
<meta property="og:title" content="The Keeper Comes Home"><meta name="author" content="Ada Lovelace">
<meta property="og:site_name" content="Harbour News"></head><body>
<nav><a href="/">Home</a> <a href="/world">World</a> <a href="/sport">Sport</a> SUBSCRIBE-NAV-JUNK</nav>
<aside class="ad">ADVERT-JUNK Buy the best boats today at discount prices!</aside>
<main><article><h1>The Keeper Comes Home</h1><p class="byline">By Ada Lovelace</p>
{''.join(f'<p>{p}</p>' for p in ARTICLE)}
<figure><img src="x.jpg"><figcaption>CAPTION-JUNK The pier at dawn.</figcaption></figure></article></main>
<section class="comments"><h2>Comments</h2><p>COMMENT-JUNK First! This is so wrong it hurts my eyes.</p></section>
<footer>FOOTER-JUNK © 2026 Harbour News. All rights reserved. Privacy policy.</footer></body></html>"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def check_rules():
    assert tr.guard("Hello there, friend.", "[sighs] Hello there, [pause] friend.") is None
    assert tr.guard("Hello there, friend.", "Hello there, pal.").startswith("words changed")
    assert tr.guard("Hello there.", "Hello [dances] there.").startswith("unknown cues")
    assert tr.guard("Then he said it was over.", "Then he said <whisper>it was over.</whisper>") is None
    assert tr.guard("a b c", "a <whisper>b c") == "<whisper> is never closed"
    assert tr.guard("a b c", "<sad>a <whisper>b</whisper> c</sad>").endswith("can't nest")
    assert tr.guard("a b c", "a <dance>b</dance> c") == "unknown tag <dance>"
    assert tr.guard("a b c", "a </sad>b c") == "</sad> without <sad>"
    assert "one paragraph" in tr.guard("a.\n\nb.", "<sad>a.\n\nb.</sad>")
    assert "max" in tr.guard("word " * 100, "<slow>" + "word " * 100 + "</slow>")
    assert tr.words("<excited>We won</excited>, [laughs] yes") == ["we", "won", "yes"]
    assert tr.guard("It’s fine.", "[sighs] It's fine.") is None, "apostrophe styles are equal"
    assert tr.guard("a b", "a <|im_end|> b") is not None
    parts = tr.split_parts(["x" * 50] * 30 + ["Long sentence here. " * 120], limit=500)
    assert all(len(p) <= 500 for p in parts) and len(parts) > 5, [len(p) for p in parts]
    assert "".join(parts).replace("\n", "").replace(" ", "") == ("x" * 1500 + "Longsentencehere." * 120)
    t, w = taggers.tag_checked(taggers.FakeTagger(), "Why? Because.", {})
    assert t == "[sighs] <emphasis>Why?</emphasis> [pause] Because." and not w, (t, w)
    t, w = taggers.tag_checked(taggers.FakeTagger(), "A BREAK-THE-GUARD b.", {})
    assert t == "A BREAK-THE-GUARD b." and w and "tagging skipped" in w[0], (t, w)
    assert pipeline.clean_paragraph("See [1] https://x.y/z now &amp; <|im_end|>") == "See now & im_end"
    for bad in ("file:///etc/passwd", "http://127.0.0.1/", "http://localhost:8766/", "http://10.0.0.1/", "ftp://x"):
        try:
            pipeline.check_url(bad)
        except pipeline.StepError:
            continue
        raise AssertionError(f"check_url accepted {bad}")
    art = pipeline.extract(FIXTURE, "https://harbour.example/keeper")
    body = " ".join(art["paragraphs"])
    assert art["title"] == "The Keeper Comes Home" and art["byline"] == "Ada Lovelace", art
    for junk in ("NAV-JUNK", "ADVERT-JUNK", "COMMENT-JUNK", "FOOTER-JUNK", "CAPTION-JUNK", "[1]"):
        assert junk not in body, (junk, body[:300])
    assert "letters that kept him going" in body and QUOTE[:30] in body
    print("rules, guard, split, extract: ok")


class Api:
    def __init__(self, port: int, token: str | None):
        self.base, self.token = f"http://127.0.0.1:{port}", token

    def call(self, method, path, body=None, raw=False, token=True, headers=None):
        h = dict(headers or {})
        if token and self.token:
            h["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode() if body is not None else None
        if data:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        with urllib.request.urlopen(req, timeout=30) as r:
            out = r.read()
            return (r.status, dict(r.headers), out) if raw else json.loads(out)

    def status(self, method, path, body=None, token=True):
        try:
            self.call(method, path, body, raw=True, token=token)
            return 200
        except urllib.error.HTTPError as e:
            return e.code


def wait_up(fn, what):
    for _ in range(150):
        try:
            return fn()
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.1)
    raise AssertionError(f"{what} didn't start")


def check_servers():
    fish_py = FISH_UI / ".venv" / "bin" / "python"
    if not fish_py.exists():
        print(f"SKIP server checks: no fish-ui venv at {fish_py} (set FISH_UI_DIR)")
        return
    page_port = free_port()

    class Page(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = FIXTURE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    pages = http.server.ThreadingHTTPServer(("127.0.0.1", page_port), Page)
    threading.Thread(target=pages.serve_forever, daemon=True).start()
    procs = []
    with tempfile.TemporaryDirectory(prefix="fish-reader-smoke-") as tmp:
        tmp = Path(tmp)
        fish_port, port = free_port(), free_port()
        (tmp / "fish.json").write_text("{}")
        procs.append(subprocess.Popen(
            [str(fish_py), str(FISH_UI / "backend" / "server.py"), "--port", str(fish_port), "--data-dir",
             str(tmp / "fish-data"), "--idle-unload-minutes", "0"],
            env=dict(os.environ, FISH_UI_FAKE_MODEL="1", FISH_UI_FAKE_DELAY="0.01", FISH_UI_CONFIG=str(tmp / "fish.json")),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))
        cfg = tmp / "reader.json"
        cfg.write_text(json.dumps({"port": port, "fish_ui_url": f"http://127.0.0.1:{fish_port}",
                                   "data_dir": str(tmp / "data")}))
        procs.append(subprocess.Popen(
            [sys.executable, str(ROOT / "server" / "server.py"), "--config", str(cfg), "--quiet"],
            env=dict(os.environ, FISH_READER_TAGGER="fake", FISH_READER_ALLOW_HOSTS=f"127.0.0.1:{page_port}"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))
        try:
            Api(fish_port, None)
            wait_up(lambda: Api(fish_port, None).call("GET", "/api/settings"), "fish-ui")
            token = None
            for _ in range(50):
                token = json.loads(cfg.read_text()).get("token")
                if token:
                    break
                time.sleep(0.1)
            assert token, "reader didn't write a token"
            api = Api(port, token)
            health = wait_up(lambda: api.call("GET", "/api/health"), "reader")
            assert health["fish_ui"] == "ok" and health["tagger"] == "fake", health
            assert api.status("GET", "/api/articles", token=False) == 401
            assert Api(port, "wrong").status("GET", "/api/articles") == 401
            assert api.status("POST", "/api/articles", {"url": "http://127.0.0.1:1/"}) == 400, "SSRF guard"
            assert api.status("POST", "/api/articles", {}) == 400
            assert api.call("GET", "/api/voices")["voices"] == []

            a = api.call("POST", "/api/articles", {"url": f"http://127.0.0.1:{page_port}/keeper"})
            assert a["status"] == "queued", a
            # live audio of a part: asked for before it starts, it waits, then streams s16le from the part's start
            st, hdr, live = api.call("GET", f"/api/articles/{a['id']}/parts/2/live", raw=True)
            assert st == 200 and hdr["X-Sample-Format"] == "s16le" and hdr["X-Sample-Rate"] == "44100", hdr
            assert len(live) % 2 == 0 and len(live) > 44100, len(live)
            deadline = time.time() + 120
            while time.time() < deadline:
                a = api.call("GET", f"/api/articles/{a['id']}")
                if a["status"] in ("done", "error"):
                    break
                time.sleep(0.3)
            assert a["status"] == "done", (a["status"], a.get("error"))
            parts = a["parts"]
            assert a["title"] == "The Keeper Comes Home" and a["site"] == "Harbour News", a
            assert len(parts) >= 3 and all(len(p["text"]) <= tr.PART_CHARS for p in parts), [len(p["text"]) for p in parts]
            assert parts[0]["text"] == parts[0]["tagged"] == "The Keeper Comes Home. By Ada Lovelace.", parts[0]
            assert parts[1]["text"].startswith("The harbour") and len(parts[1]["text"]) <= tr.FIRST_PART_CHARS, parts[1]
            assert all(p["tagged"].startswith("[sighs] ") and p["status"] == "done" for p in parts[1:])
            assert any("[pause]" in p["tagged"] for p in parts)
            assert any("<emphasis>" in p["tagged"] for p in parts), "span tags reach fish-ui"
            assert all(p["start_s"] is not None for p in parts) and parts[0]["start_s"] == 0
            total = sum(p["duration_s"] for p in parts)
            assert abs(a["duration_s"] - total) < 0.01 and total > 0, (a["duration_s"], total)
            # chained continuation: each part after the first continued from the previous take
            for prev, p in zip(parts, parts[1:]):
                wf, stem = p["take"].split("/")
                side = Api(fish_port, None).call("GET", f"/api/history/{wf}/{stem}")
                assert side["previous_takes"] == [prev["take"]], side.get("previous_takes")
            st, hdr, audio = api.call("GET", a["audio_url"], raw=True)
            assert st == 200 and hdr["Content-Type"] == "audio/mp4" and audio[4:8] == b"ftyp", (st, hdr)
            st, hdr, piece = api.call("GET", parts[1]["audio_url"], raw=True, headers={"Range": "bytes=0-99"})
            assert st == 206 and len(piece) == 100 and hdr["Content-Range"].startswith("bytes 0-99/"), hdr
            assert api.status("GET", a["audio_url"], token=False) == 401
            dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                        "-"], input=audio, capture_output=True).stdout or 0)
            assert abs(dur - total) < 0.5, (dur, total)
            live_s = len(live) / 2 / 44100
            assert abs(live_s - parts[2]["duration_s"]) < 0.2, (live_s, parts[2]["duration_s"])
            assert api.status("GET", f"/api/articles/{a['id']}/parts/2/live") == 409, "finished part: play the file"
            jobs = Api(fish_port, None).call("GET", "/api/jobs")["jobs"]
            assert all(j["stream"] for j in jobs if j["workflow"] == "tts"), "reader jobs stream"
            listed = api.call("GET", "/api/articles")["articles"]
            assert [x["id"] for x in listed][-1] == a["id"] and "parts" not in listed[0]

            # a page shared from the phone (paywalled: the server can't fetch it) is used as given
            shared = api.call("POST", "/api/articles", {"url": "https://paywalled.example/post", "html": FIXTURE})
            assert shared["source"] == "shared", shared
            while (shared := api.call("GET", f"/api/articles/{shared['id']}"))["status"] not in ("done", "error"):
                time.sleep(0.3)
            assert shared["status"] == "done" and shared["title"] == "The Keeper Comes Home", shared.get("error")

            bad = api.call("POST", "/api/articles", {"url": f"http://127.0.0.1:{page_port}/x"})
            assert api.call("DELETE", f"/api/articles/{bad['id']}") == {"removed": bad["id"]}
            assert api.status("GET", f"/api/articles/{bad['id']}") == 404
            print(f"servers: ok ({len(parts)} parts, {total:.1f} s, {len(audio)} bytes m4a)")
        except BaseException:
            for p in procs:
                p.terminate()
                out = p.communicate(timeout=5)[0]
                print("---- process output ----\n" + (out or "")[-3000:])
            raise
        finally:
            for p in procs:
                if p.poll() is None:
                    p.terminate()
                    p.wait(timeout=5)
            pages.shutdown()


if __name__ == "__main__":
    check_rules()
    check_servers()
    print("smoke: ok")
