"""Shared helpers for the setup scripts (stdlib only: setup.py runs before the venv exists, on macOS's Python 3.9)."""
from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.json"
EXAMPLE = ROOT / "config.example.json"
VENV_PY = ROOT / ".venv" / "bin" / "python"
TAILSCALE_APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"

BOLD, DIM, RED, GREEN, YELLOW, RESET = ("\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m") \
    if sys.stdout.isatty() else ("",) * 6


def say(msg: str = ""):
    print(msg, flush=True)


def ok(msg: str):
    say(f"  {GREEN}✓{RESET} {msg}")


def warn(msg: str):
    say(f"  {YELLOW}!{RESET} {msg}")


def bad(msg: str):
    say(f"  {RED}✗{RESET} {msg}")


def step(title: str):
    say(f"\n{BOLD}{title}{RESET}")


def ask(prompt: str, default: str = "") -> str:
    try:
        got = input(f"  {prompt}{f' [{default}]' if default else ''}: ").strip()
    except EOFError:
        got = ""
    return got or default


def yes(prompt: str, default: bool = True) -> bool:
    got = ask(f"{prompt} ({'Y/n' if default else 'y/N'})").lower()
    return default if not got else got.startswith("y")


def load_config() -> dict:
    """config.json, created from the example (with a fresh token) when missing."""
    if not CONFIG.exists():
        cfg = json.loads(EXAMPLE.read_text())
        cfg["token"] = secrets.token_urlsafe(24)
        save_config(cfg)
    cfg = json.loads(CONFIG.read_text())
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(24)
        save_config(cfg)
    return cfg


def save_config(cfg: dict):
    CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")
    CONFIG.chmod(0o600)  # holds the token


def voices_dir(cfg: dict) -> Path:
    if cfg.get("voices_dir"):
        p = Path(cfg["voices_dir"]).expanduser()
    else:
        p = Path(cfg.get("data_dir") or "./data").expanduser() / "voices"
    return p if p.is_absolute() else (ROOT / p).resolve()


def voices(cfg: dict) -> list[dict]:
    """Usable voices (reference + transcript)."""
    out = []
    d = voices_dir(cfg)
    for v in sorted(d.glob("*/voice.json")) if d.is_dir() else []:
        t = v.parent / "transcript.txt"
        if (v.parent / "reference.wav").is_file() and t.is_file() and t.read_text().strip():
            try:
                name = json.loads(v.read_text()).get("name") or v.parent.name
            except json.JSONDecodeError:
                name = v.parent.name
            out.append({"id": v.parent.name, "name": name})
    return out


def tailscale() -> str | None:
    return shutil.which("tailscale") or (TAILSCALE_APP if Path(TAILSCALE_APP).exists() else None)


def tailscale_name() -> str | None:
    """This Mac's MagicDNS name (e.g. my-mac.tailnet.ts.net), or None when Tailscale isn't running."""
    ts = tailscale()
    if not ts:
        return None
    try:
        st = json.loads(subprocess.run([ts, "status", "--json"], capture_output=True, text=True, timeout=10).stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None
    if st.get("BackendState") != "Running":
        return None
    return (st.get("Self") or {}).get("DNSName", "").rstrip(".") or None
