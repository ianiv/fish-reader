"""Small client for the fish-ui HTTP API (see fish-ui/docs/CONTRACTS.md)."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

TERMINAL = ("done", "error", "cancelled")


class FishError(RuntimeError):
    pass


class FishClient:
    def __init__(self, base: str = "http://127.0.0.1:8766", timeout: float = 30):
        self.base, self.timeout = base.rstrip("/"), timeout

    def call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error") or e.reason
            except Exception:  # noqa: BLE001
                msg = e.reason
            raise FishError(f"fish-ui {method} {path}: {e.code} {msg}") from None
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            raise FishError(f"fish-ui is not reachable at {self.base} ({getattr(e, 'reason', e)}); is ./fish-ui running?") from None

    def models(self) -> dict:
        return self.call("GET", "/api/models")

    def voices(self) -> list:
        return self.call("GET", "/api/voices")["voices"]

    def warm(self) -> dict:
        return self.call("POST", "/api/models/drama3/warm", {})

    def pcm(self, job_id: str):
        """The job's live PCM stream (an open response: float32 mono, X-Sample-Rate header)."""
        try:
            return urllib.request.urlopen(f"{self.base}/api/jobs/{job_id}/pcm", timeout=60)
        except urllib.error.HTTPError as e:
            raise FishError(f"fish-ui pcm {job_id}: {e.code}") from None
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            raise FishError(f"fish-ui is not reachable ({getattr(e, 'reason', e)})") from None

    def submit_tts(self, text: str, voice: str | None = None, previous_takes: list[str] | None = None,
                   stream: bool = False) -> dict:
        params = {"text": text, "language": "auto", "language_mode": "auto"}
        if voice:
            params["voice"] = voice
        if previous_takes:
            params["previous_takes"] = previous_takes
        return self.call("POST", "/api/jobs", {"workflow": "tts", "params": params, "variations": 1,
                                               "formats": ["wav"], "stream": stream})

    def job(self, job_id: str) -> dict:
        return self.call("GET", f"/api/jobs/{job_id}")

    def cancel(self, job_id: str):
        try:
            self.call("DELETE", f"/api/jobs/{job_id}")
        except FishError:
            pass

    def wait(self, job_id: str, should_stop=lambda: False, interval: float = 0.5, on_progress=None) -> dict:
        """Poll the job until it ends; cancels it when should_stop() turns true."""
        while True:
            job = self.job(job_id)
            if job["status"] in TERMINAL:
                return job
            if on_progress:
                on_progress(job)
            if should_stop():
                self.cancel(job_id)
            time.sleep(interval)

    def download(self, url: str, dest) -> None:
        with urllib.request.urlopen(self.base + url, timeout=120) as r, open(dest, "wb") as f:
            while chunk := r.read(1 << 16):
                f.write(chunk)
