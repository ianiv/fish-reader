"""Show how to connect the iPhone app to this Mac: the server URL, the token and a QR code.

Scan the QR code with the iPhone's Camera app: it opens Reader and fills in Settings
(fishreader://pair?server=…&token=…). Run with the repo's venv: ./fish-reader pair
"""
from __future__ import annotations

import subprocess
import sys
import urllib.parse

from common import BOLD, DIM, RESET, bad, load_config, ok, say, tailscale, tailscale_name, warn


def served(ts: str, port: int) -> bool:
    out = subprocess.run([ts, "serve", "status"], capture_output=True, text=True, timeout=10).stdout
    return f"127.0.0.1:{port}" in out or f"localhost:{port}" in out


def main():
    cfg = load_config()
    port = int(cfg.get("port", 8767))
    name = tailscale_name()
    if not name:
        bad("Tailscale isn't running on this Mac, so the phone can't reach the server.")
        say("    Install Tailscale (https://tailscale.com/download) on the Mac and the iPhone, sign in to the same "
            "account on both, then run this again.")
        sys.exit(1)
    if not served(tailscale(), port):
        warn(f"`tailscale serve` isn't forwarding to the server yet. Run:\n"
             f"    {tailscale()} serve --bg --https=443 http://127.0.0.1:{port}")
    server = f"https://{name}"
    link = "fishreader://pair?" + urllib.parse.urlencode({"server": server, "token": cfg["token"]})
    say(f"\n{BOLD}Connect the iPhone app{RESET}")
    say("  Scan this with the iPhone's Camera app and tap the link (Reader must be installed):\n")
    try:
        import segno
    except ImportError:
        warn("QR code needs the venv (./fish-reader pair); showing the link instead")
        say(f"  {link}")
    else:
        segno.make(link, error="m").terminal(compact=True, border=2)
    say(f"\n  Or type these in Reader → Settings:")
    say(f"    Server  {server}")
    say(f"    Token   {cfg['token']}")
    say(f"  {DIM}(the token is in config.json; anyone with it and access to your tailnet can use the server){RESET}")
    ok("Start the server with ./fish-reader if it isn't running")


if __name__ == "__main__":
    main()
