"""Set up fish-reader on this Mac, step by step: checks, Python environment, model download, your first voice,
Tailscale, the iOS build settings, and the QR code that connects the iPhone app.

Run it again any time: finished steps are skipped or just re-checked.  Usage: ./fish-reader setup
(Runs on macOS's own Python 3; everything else goes in the repo's .venv.)
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (BOLD, DIM, RESET, ROOT, VENV_PY, ask, bad, load_config, ok, save_config, say, step,  # noqa: E402
                    tailscale, tailscale_name, voices, warn, yes)

MODEL = "mlx-community/fish-audio-drama-3-4bit"
MIN_RAM_GB = 32
LOCAL_XCCONFIG = ROOT / "ios" / "Local.xcconfig"


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kw)


def check_machine():
    step("1. This Mac")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        bad("Drama 3 runs on Apple silicon Macs (M1 or later) only.")
        sys.exit(1)
    ram = int(run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout) / 2 ** 30
    if ram < MIN_RAM_GB:
        bad(f"{ram:.0f} GB of memory: Drama 3 needs about 27 GB while it reads, so {MIN_RAM_GB} GB is the minimum.")
        if not yes("Continue anyway?", default=False):
            sys.exit(1)
    else:
        ok(f"Apple silicon, {ram:.0f} GB of memory")
    free = shutil.disk_usage(Path.home()).free / 2 ** 30
    (ok if free > 30 else warn)(f"{free:.0f} GB free disk (the model is a ~22 GB download)")


def check_tools() -> dict:
    step("2. Tools")
    found = {}
    need = {"uv": "Python environments: brew install uv (or see https://docs.astral.sh/uv/)",
            "ffmpeg": "audio conversion: brew install ffmpeg"}
    missing = False
    for tool, how in need.items():
        if shutil.which(tool):
            ok(tool)
        else:
            bad(f"{tool} is missing ({how})")
            missing = True
    if missing:
        say(f"  {DIM}No Homebrew? https://brew.sh{RESET}")
        sys.exit(1)
    found["claude"] = bool(shutil.which("claude"))
    if found["claude"]:
        ok("claude (Claude Code): adds expressive markup to articles")
    else:
        warn("claude (Claude Code) not found: articles will be read without expressive markup. Optional: "
             "https://claude.com/claude-code, then run `claude` once to log in")
    found["tailscale"] = bool(tailscale())
    (ok if found["tailscale"] else warn)("Tailscale" if found["tailscale"] else
                                         "Tailscale not found: the phone needs it to reach this Mac "
                                         "(https://tailscale.com/download, on the Mac and the iPhone)")
    found["xcodegen"] = bool(shutil.which("xcodegen"))
    (ok if found["xcodegen"] else warn)("xcodegen" if found["xcodegen"] else
                                        "xcodegen not found (needed to build the iOS app): brew install xcodegen")
    found["xcode"] = run(["xcode-select", "-p"], capture_output=True).returncode == 0 and \
        Path("/Applications/Xcode.app").exists()
    (ok if found["xcode"] else warn)("Xcode" if found["xcode"] else
                                     "Xcode not found (needed to build the iOS app): install it from the App Store")
    return found


def python_env():
    step("3. Python environment (.venv)")
    if not VENV_PY.exists():
        run(["uv", "venv", "--python", "3.13", str(ROOT / ".venv")], check=True)
    r = run(["uv", "pip", "install", "--python", str(VENV_PY), "-q", "-r", str(ROOT / "requirements.txt")])
    if r.returncode:
        bad("installing the requirements failed (see above)")
        sys.exit(1)
    ok("installed")


def claude_login(found: dict, cfg: dict):
    if not found["claude"] or cfg.get("tagger") != "claude":
        return
    step("4. Claude login")
    say(f"  {DIM}checking with one tiny request…{RESET}")
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        r = run(["claude", "-p", "--output-format", "json", "--model", cfg.get("claude_model", "sonnet"),
                 "--tools", "", "--no-session-persistence"], input="Reply with OK.", capture_output=True, text=True,
                timeout=90, env=env, cwd=str(Path.home()))
        good = r.returncode == 0 and not json.loads(r.stdout or "{}").get("is_error")
    except (subprocess.SubprocessError, json.JSONDecodeError):
        good = False
    if good:
        ok("logged in: articles get expressive markup")
    else:
        warn("claude isn't logged in (run `claude` once and log in). Until then articles are read without "
             "expressive markup; the server checks again each time it starts.")


def model():
    step("5. Drama 3 model")
    code = (f"from huggingface_hub import snapshot_download, try_to_load_from_cache as c\n"
            f"import sys\nsys.exit(0 if c('{MODEL}', 'config.json') and c('{MODEL}', 'model-00002.safetensors') "
            f"else 1)")
    if run([str(VENV_PY), "-c", code]).returncode == 0:
        ok("downloaded")
        return
    say(f"  {MODEL} is about 22 GB. Weights license: Fish Audio Community License (non-commercial use; read it at "
        f"https://huggingface.co/{MODEL}).")
    if not yes("Download it now?"):
        warn("skipped: the server downloads it the first time it reads something")
        return
    r = run([str(VENV_PY), "-c", f"from huggingface_hub import snapshot_download; snapshot_download('{MODEL}')"])
    (ok if r.returncode == 0 else bad)("downloaded" if r.returncode == 0 else "download failed: run setup again "
                                                                             "to resume")


def first_voice(cfg: dict):
    step("6. Narrator voice")
    have = voices(cfg)
    if have:
        ok(f"{len(have)} voice{'s' if len(have) != 1 else ''}: {', '.join(v['name'] for v in have)}"
           + (f" (default: {cfg['voice']})" if cfg.get("voice") else ""))
        if not yes("Add another one?", default=False):
            return
    else:
        say("  Drama 3 reads in a voice cloned from a short clip. Without one, each article gets a voice the model "
            "picks.")
        if not yes("Add your first voice now?"):
            warn("skipped: add one later with ./fish-reader add-voice")
            return
    run([str(VENV_PY), str(ROOT / "scripts" / "add_voice.py")])


def tailscale_serve(found: dict, cfg: dict):
    step("7. Reaching the server from the phone (Tailscale)")
    if not found["tailscale"]:
        warn("install Tailscale on this Mac and the iPhone (same account), then run setup again")
        return
    name = tailscale_name()
    if not name:
        warn("Tailscale isn't running or signed in: open it, sign in, then run setup again")
        return
    ts, port = tailscale(), int(cfg.get("port", 8767))
    status = run([ts, "serve", "status"], capture_output=True, text=True).stdout
    if f"127.0.0.1:{port}" in status or f"localhost:{port}" in status:
        ok(f"https://{name} → the server (tailnet only)")
        return
    say(f"  `tailscale serve` gives the server an HTTPS address only your own devices can reach: https://{name}")
    if yes("Set it up now?"):
        r = run([ts, "serve", "--bg", "--https=443", f"http://127.0.0.1:{port}"])
        if r.returncode == 0:
            ok(f"https://{name} → the server")
        else:
            bad("didn't work. In the Tailscale admin console (DNS page) turn on MagicDNS and HTTPS certificates, "
                "then run setup again")


def team_ids() -> list[tuple[str, str]]:
    """(team id, name) from the Apple Development certificates in the keychain."""
    pems = run(["security", "find-certificate", "-a", "-c", "Apple Development", "-p"], capture_output=True,
               text=True).stdout
    out = []
    for pem in re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", pems, re.S):
        subj = run(["openssl", "x509", "-noout", "-subject"], input=pem, capture_output=True, text=True).stdout
        team = re.search(r"OU\s*=\s*([A-Z0-9]{10})", subj)
        org = re.search(r"\bO\s*=\s*([^,/\n]+)", subj)
        if team and team.group(1) not in [t for t, _ in out]:
            out.append((team.group(1), org.group(1).strip() if org else ""))
    return out


def ios_settings():
    step("8. iOS app build settings (ios/Local.xcconfig)")
    if LOCAL_XCCONFIG.exists():
        ok("already set: " + " ".join(l.split("=")[1].strip() for l in LOCAL_XCCONFIG.read_text().splitlines()
                                      if "=" in l and not l.startswith("//")))
        return
    say("  You build the app yourself in Xcode, signed with your Apple ID (a free one works: the app then needs "
        "reinstalling from Xcode every 7 days).")
    teams = team_ids()
    team = ""
    if teams:
        for i, (t, n) in enumerate(teams, 1):
            say(f"  {i}) {t}  {n}")
        pick = ask("Your team (number, or a team ID)", "1")
        team = teams[int(pick) - 1][0] if pick.isdigit() and 0 < int(pick) <= len(teams) else pick
    else:
        say(f"  {DIM}No signing certificate yet: in Xcode, Settings → Accounts, add your Apple ID; its Team ID is "
            f"shown there (or leave it empty and pick the team in Xcode).{RESET}")
        team = ask("Team ID (10 characters, or Enter to skip)")
    user = re.sub(r"[^a-z0-9]", "", os.environ.get("USER", "me").lower()) or "me"
    prefix = ask("Bundle ID prefix, reverse DNS that's yours", f"com.{user}")
    LOCAL_XCCONFIG.write_text(f"DEVELOPMENT_TEAM = {team}\nBUNDLE_PREFIX = {prefix}\n")
    ok(f"wrote {LOCAL_XCCONFIG.relative_to(ROOT)}")


def main():
    say(f"{BOLD}fish-reader setup{RESET}  {DIM}(Ctrl-C to stop; run again to continue){RESET}")
    check_machine()
    found = check_tools()
    python_env()
    cfg = load_config()
    ok(f"config.json ready (port {cfg.get('port', 8767)})")
    claude_login(found, cfg)
    model()
    first_voice(cfg)
    cfg = load_config()
    tailscale_serve(found, cfg)
    ios_settings()
    step("9. Connect the phone")
    run([str(VENV_PY), str(ROOT / "scripts" / "pair.py")])
    say(f"\n{BOLD}Next{RESET}")
    say("  • Start the server:  ./fish-reader")
    say("  • Build the app:     cd ios && xcodegen generate && open ArticleReader.xcodeproj")
    say("                       then pick your iPhone at the top and press Run (⌘R)")
    say("  • Pair:              ./fish-reader pair  (shows the QR code again)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\nstopped")
