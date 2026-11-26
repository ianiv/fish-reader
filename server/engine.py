"""Drama 3 inside fish-reader: one thread owns MLX (load, warm-up, renders, idle unload), as fish-ui's Worker does.

The pipeline calls render() from its own thread; the call is queued to the engine thread and blocks until the
take is done. While it renders, the audio is also written to a Live buffer (16-bit PCM) that the phone streams.
Voices are fish-ui-style folders: <voices_dir>/<id>/{voice.json, reference.wav, transcript.txt}.
"""
from __future__ import annotations

import gc
import json
import queue
import re
import sys
import threading
import time
import traceback
from concurrent.futures import Future
from pathlib import Path

import fish_core as core

VOICE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def log(*a):
    print("[engine]", *a, file=sys.stderr, flush=True)


class EngineError(RuntimeError):
    pass


class Live:
    """A take's audio as it's generated: little-endian int16 mono, readable from the start until it ends."""

    def __init__(self, sample_rate: int = core.SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.data = bytearray()
        self.done = False
        self.cond = threading.Condition()

    def push(self, audio):
        import numpy as np

        pcm = (np.clip(np.asarray(audio, dtype=np.float32), -1, 1) * 32767).astype("<i2").tobytes()
        with self.cond:
            self.data += pcm
            self.cond.notify_all()

    def close(self):
        with self.cond:
            self.done = True
            self.cond.notify_all()

    def read(self, pos: int, timeout: float = 120) -> bytes | None:
        """Bytes after `pos`, waiting for more; b"" once it has ended, None on timeout."""
        with self.cond:
            end = time.time() + timeout
            while len(self.data) <= pos and not self.done:
                left = end - time.time()
                if left <= 0:
                    return None
                self.cond.wait(left)
            return bytes(self.data[pos:])


class Engine:
    def __init__(self, voices_dir: Path, idle_unload_minutes: float = 15):
        self.voices_dir = Path(voices_dir)
        self.idle_unload_s = float(idle_unload_minutes or 0) * 60
        self.tasks: queue.Queue = queue.Queue()
        self.model = None
        self.state = "empty"  # empty | loading | warming | ready | idle-unloaded | error
        self.error = None
        self.load_s = self.warm_s = None
        self.busy = False
        self.rtf = core.MODEL_INFO[core.MODEL_KEY]["rtf"]
        self.last_activity = time.time()
        self.codes: dict = {}  # (wav path, mtime) -> codec codes of a previous take
        threading.Thread(target=self._loop, name="mlx-engine", daemon=True).start()
        if self.idle_unload_s > 0:
            threading.Thread(target=self._idle, name="mlx-idle", daemon=True).start()

    # --- voices ---

    def voices(self) -> list[dict]:
        out = []
        if self.voices_dir.is_dir():
            for d in sorted(self.voices_dir.iterdir()):
                meta = d / "voice.json"
                if meta.is_file() and (d / "reference.wav").is_file():
                    try:
                        m = json.loads(meta.read_text())
                    except (OSError, json.JSONDecodeError):
                        continue
                    out.append({"id": d.name, "name": m.get("name") or d.name, "description": m.get("description") or "",
                                "has_transcript": bool(self._transcript(d))})
        return sorted(out, key=lambda v: v["name"].lower())

    @staticmethod
    def _transcript(d: Path) -> str:
        t = d / "transcript.txt"
        return t.read_text().strip() if t.is_file() else ""

    def voice(self, vid: str) -> core.Voice:
        d = self.voices_dir / vid
        if not VOICE_ID.match(vid or "") or not (d / "reference.wav").is_file():
            raise core.RequestError(f"no voice '{vid}' in {self.voices_dir}")
        return core.Voice(id=vid, audio=str(d / "reference.wav"), transcript=self._transcript(d) or None)

    # --- state ---

    def status(self) -> dict:
        return {"key": core.MODEL_KEY, "state": self.state, "error": self.error, "load_s": self.load_s,
                "warm_s": self.warm_s, "busy": self.busy, "pending": self.tasks.qsize(), "rtf": self.rtf,
                "fake": core.FAKE}

    def _set(self, state: str, error: str | None = None):
        self.state, self.error = state, error
        log(f"model {state}" + (f": {error}" if error else ""))

    # --- public calls (any thread) ---

    def warm(self):
        """Load the model now (without waiting) so loading overlaps fetching and tagging."""
        if self.model is None and self.state != "loading":
            self._submit(lambda: self._ensure())

    def render(self, text: str, voice: str | core.Voice | None = None, history: list[core.PrevTake] | None = None,
               live: Live | None = None, should_cancel=lambda: False) -> tuple[core.Take, list[str]]:
        """One streamed take of `text` (≤ 3000 chars) continuing `history`. Returns (take, warnings).
        Raises core.RequestError for bad input, core.Cancelled when should_cancel() turned true."""
        v = self.voice(voice) if isinstance(voice, str) else voice  # a library voice id, or a ready Voice
        items = core.tts_items({"text": text}, v, history or [])
        item = items[0]
        try:
            take = self._submit(lambda: self._render(item, live, should_cancel)).result()
        finally:
            if live:
                live.close()
        if take is None:
            raise EngineError("the model made no audio")
        return take, item.warnings

    def unload(self):
        """Free the model's memory now (after anything already queued); it loads again on the next render."""
        if self.model is not None:
            self._submit(self._unload)

    # --- engine thread ---

    def _submit(self, fn) -> Future:
        f: Future = Future()
        self.tasks.put((fn, f))
        return f

    def _loop(self):
        while True:
            fn, f = self.tasks.get()
            self.busy = True
            try:
                f.set_result(fn())
            except BaseException as e:  # noqa: BLE001
                if not isinstance(e, (core.Cancelled, core.RequestError)):
                    traceback.print_exc()
                f.set_exception(e)
            finally:
                self.busy = False
                self.last_activity = time.time()

    def _ensure(self):
        if self.model is not None:
            return self.model
        try:
            self._set("loading")
            t0 = time.perf_counter()
            log(f"loading {core.MODELS[core.MODEL_KEY]}{' (FAKE)' if core.FAKE else ''}…")
            model = core.load_model()
            self.load_s = round(time.perf_counter() - t0, 2)
            self._set("warming")
            t0 = time.perf_counter()
            core.render(model, core.warmup_kwargs(), seed=0)
            self.warm_s = round(time.perf_counter() - t0, 2)
            self.model = model
            _clear_cache()
            log(f"ready: load {self.load_s}s, warm-up {self.warm_s}s")
            self._set("ready")
        except Exception as e:
            self.model = None
            gc.collect()
            _clear_cache()
            self._set("error", str(e))
            raise EngineError(f"couldn't load Drama 3: {e}") from None
        return self.model

    def _codes(self, take: core.PrevTake):
        k = (take.wav, Path(take.wav).stat().st_mtime_ns)
        if k not in self.codes:
            self.codes[k] = core.encode_take(self.model, take.wav, take.gain)
            while len(self.codes) > 8:
                self.codes.pop(next(iter(self.codes)))
        return self.codes[k]

    def _render(self, item: core.Item, live: Live | None, should_cancel):
        model = self._ensure()
        on_chunk = (lambda audio, sr, so_far: live.push(audio)) if live else None
        try:
            take = core.render(model, item.kwargs, stream=True, on_chunk=on_chunk, should_cancel=should_cancel,
                               history=item.history, codes_for=self._codes, segments=item.segments)
        finally:
            _clear_cache()
        if take and take.duration_s:
            rtf = take.wall_s / take.duration_s
            self.rtf = round(0.7 * self.rtf + 0.3 * min(max(rtf, self.rtf / 3), self.rtf * 3), 3)
            log(f"rendered {take.duration_s:.1f}s in {take.wall_s:.1f}s (first audio {take.first_audio_s or 0:.2f}s, "
                f"peak {take.peak_gb:.1f} GB)")
        return take

    def _unload(self):
        if self.model is None:
            return
        self.model = None
        self.codes.clear()
        gc.collect()
        _clear_cache()
        self._set("idle-unloaded")

    def _idle(self):
        while True:
            time.sleep(5)
            if self.model is not None and not self.busy and self.tasks.empty() \
                    and time.time() - self.last_activity >= self.idle_unload_s:
                self._submit(self._unload)
                self.last_activity = time.time()


def _clear_cache():
    if core.FAKE:
        return
    import mlx.core as mx

    mx.clear_cache()


def write_wav(path: Path, take: core.Take) -> float | None:
    """The take as a 16-bit WAV, normalized like fish-ui's saved takes. Returns the gain (None = untouched)."""
    audio, gain = core.normalize_take(take.audio)
    core.write_audio(path, audio, take.sample_rate, "wav")
    return gain
