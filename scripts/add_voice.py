"""Add a narrator voice: record yourself reading a short passage, or use an audio file of someone speaking.

Drama 3 clones the voice from a 10-30 s reference clip plus its exact transcript. A voice is a folder:
<voices_dir>/<id>/{voice.json, reference.wav, transcript.txt} (the same layout as fish-ui).
Run with the repo's venv: ./fish-reader add-voice [--file clip.m4a] [--name "My voice"] [--transcript "..."]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from common import BOLD, DIM, RESET, ask, bad, load_config, ok, save_config, say, voices, voices_dir, warn, yes

PASSAGE = ("The harbour was quiet that morning. A few boats rocked against the pier, and somewhere a radio played "
           "an old song. I poured a coffee, opened the paper, and smiled: for once, the news could wait. "
           "Then the phone rang. Was it good news, or bad? I took a breath and answered.")
MIN_S, MAX_S = 6.0, 30.0
WHISPER = "mlx-community/whisper-large-v3-turbo"
WHISPER_PROCESSOR = "openai/whisper-large-v3-turbo"


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return float(out or 0)


def to_reference(src: Path, dest: Path, max_s: float | None = None) -> float:
    """Mono 44.1 kHz 16-bit WAV, leading/trailing silence trimmed, optionally cut to max_s."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", "44100",
           "-af", "silenceremove=start_periods=1:start_threshold=-45dB,areverse,"
                  "silenceremove=start_periods=1:start_threshold=-45dB,areverse"]
    if max_s:
        cmd += ["-t", str(max_s)]
    subprocess.run(cmd + ["-c:a", "pcm_s16le", str(dest)], check=True)
    return duration(dest)


def record(dest: Path) -> bool:
    say(f"\n  Read this aloud in your normal narrating voice, at an easy pace, in a quiet room:\n")
    say(f"  {BOLD}{PASSAGE}{RESET}\n")
    ask("Press Enter to start recording")
    for device in (":default", ":0"):  # the system's input device; else the first one
        p = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "avfoundation", "-i", device,
                              "-ac", "1", "-ar", "44100", str(dest)], stdin=subprocess.PIPE)
        t0 = time.time()
        time.sleep(0.5)
        if p.poll() is None:
            break
    if p.poll() is not None:
        bad("Couldn't open the microphone. Allow your terminal app in System Settings → Privacy & Security → "
            "Microphone, then try again (or use an audio file).")
        return False
    ask("● Recording… press Enter when you've finished reading")
    p.communicate(b"q")
    say(f"  {DIM}recorded {time.time() - t0:.0f} s{RESET}")
    return dest.exists()


def transcribe(path: Path) -> str:
    say(f"  {DIM}transcribing with Whisper (downloads ~1.6 GB the first time)…{RESET}")
    from mlx_audio.stt import load

    model = load(WHISPER)
    if getattr(model, "_processor", "n/a") is None:  # the mlx-community repo ships weights only
        from transformers import WhisperProcessor

        model._processor = WhisperProcessor.from_pretrained(WHISPER_PROCESSOR)
    return re.sub(r"\s+", " ", (model.generate(str(path)).text or "")).strip()


def slug(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "voice"
    if not re.match(r"^[a-z0-9]", base):
        base = "v-" + base
    vid, n = base, 2
    while vid in taken:
        vid, n = f"{base}-{n}", n + 1
    return vid


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", help="audio file of the voice (any format ffmpeg reads)")
    ap.add_argument("--name", help="display name")
    ap.add_argument("--transcript", help="exactly what is said in the clip")
    ap.add_argument("--default", action="store_true", help="make it the server's default narrator")
    args = ap.parse_args()
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"needs {tool}: brew install ffmpeg")
    cfg = load_config()
    vdir = voices_dir(cfg)

    say(f"\n{BOLD}Add a voice{RESET}")
    say(f"  {DIM}Only use a voice you have the right to use: your own, or someone who agreed to it.{RESET}")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src, transcript, source = None, args.transcript, "upload"
        if args.file:
            src = Path(args.file).expanduser()
        else:
            say("  1) Record yourself now (needs a microphone)\n  2) Use an audio file")
            if ask("Choose", "1") == "1":
                src, transcript, source = tmp / "recording.wav", PASSAGE, "recording"
                if not record(src):
                    sys.exit(1)
            else:
                src = Path(ask("Path to the audio file (you can drag it into this window)").strip("'\" ")).expanduser()
        if not src.is_file():
            sys.exit(f"no such file: {src}")

        ref = tmp / "reference.wav"
        long = duration(src) > MAX_S + 1
        if long and not transcript:
            warn(f"the clip is {duration(src):.0f} s: using the first {MAX_S:.0f} s")
        dur = to_reference(src, ref, MAX_S if (long and not transcript) else None)
        if dur < MIN_S:
            sys.exit(f"only {dur:.1f} s of speech: use at least {MIN_S:.0f} s (10-30 s works best)")
        if dur > MAX_S + 1:
            warn(f"{dur:.0f} s is long for a reference; 10-30 s works best (longer is slower and no better)")

        if not transcript:
            say("  Drama 3 needs the exact words spoken in the clip.")
            typed = ask("Type them, or press Enter to transcribe automatically")
            transcript = typed or transcribe(ref)
            if not typed:
                say(f"  Heard: {BOLD}{transcript}{RESET}")
                fix = ask("Press Enter if that's right, or type the corrected text")
                transcript = fix or transcript
        if not transcript.strip():
            sys.exit("no transcript")

        name = args.name or ask("Name for this voice", "My voice")
        taken = {d.name for d in vdir.iterdir()} if vdir.is_dir() else set()
        vid = slug(name, taken)
        d = vdir / vid
        d.mkdir(parents=True)
        shutil.copy(ref, d / "reference.wav")
        (d / "transcript.txt").write_text(transcript.strip() + "\n")
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        (d / "voice.json").write_text(json.dumps({"name": name, "description": "", "source": source,
                                                  "created": now, "updated": now, "duration_s": round(dur, 2),
                                                  "sample_rate": 44100}, indent=2) + "\n")
    ok(f"added voice '{name}' ({dur:.1f} s) in {d}")
    if args.default or not cfg.get("voice") or yes("Make it the default narrator?", default=len(voices(cfg)) <= 1):
        cfg["voice"] = vid
        save_config(cfg)
        ok("it's the default narrator (restart the server if it's running)")


if __name__ == "__main__":
    main()
