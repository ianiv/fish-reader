# fish-reader

Listen to web articles on your iPhone, read aloud with expression by
[Fish Audio Drama 3](https://huggingface.co/fishaudio/drama-3) running on your own Mac.

```
iPhone app ──HTTPS over your tailnet──▶ fish-reader server on your Mac (runs Drama 3)
                                         fetch → find the article → mark up delivery → read aloud → m4a
```

1. Share a page from Safari (or paste a link). Shared pages are sent as you see them, so members-only posts work.
2. The server finds the article text, leaving out menus, ads, comments and reference lists.
3. Claude (optional, through the Claude Code CLI and its own login) marks up the delivery without changing a
   word: `<whisper>…</whisper>`, `<sad>…</sad>`, `[sighs]`, `[pause]` and so on.
4. Drama 3 reads it in parts of about 70 s, each continuing from the last so the voice carries on. The phone
   starts playing about a second after the first part starts rendering.

Everything stays on your devices: the server only listens on the Mac itself, and Tailscale makes it reachable
from your phone and nobody else.

## What you need

- **A Mac with Apple silicon (M1 or later) and at least 32 GB of memory.** Drama 3 uses about 27 GB while it
  reads, and the model is a ~22 GB download.
- **An iPhone on iOS 26**, and **Xcode** on the Mac to build the app (you build it yourself; it isn't in the App
  Store).
- **[Tailscale](https://tailscale.com/download)** on the Mac and the iPhone, signed in to the same account (free
  for personal use).
- [Homebrew](https://brew.sh) for the tools: `brew install uv ffmpeg xcodegen`
- Optional: **[Claude Code](https://claude.com/claude-code)**, logged in (run `claude` once). Without it articles
  are read without the expressive markup.

## Set up the Mac

```sh
git clone <this repo> fish-reader && cd fish-reader
./fish-reader setup
```

The setup script walks through everything and can be run again at any time:

1. checks the Mac and the tools,
2. creates the Python environment (`.venv`),
3. checks your Claude login,
4. downloads the Drama 3 model,
5. **adds your first voice**: record yourself reading a short passage, or use a 10–30 s clip of someone
   speaking (it can transcribe the clip for you),
6. turns on `tailscale serve`, which gives the server an HTTPS address only your devices can reach,
7. writes your iOS build settings (`ios/Local.xcconfig`),
8. shows a **QR code** for connecting the phone.

Then start the server and leave it running:

```sh
./fish-reader
```

The model loads with the first article (about 15 s) and unloads after a few minutes without work, so it only
uses memory while it reads.

## Build the iPhone app

```sh
cd ios && xcodegen generate && open ArticleReader.xcodeproj
```

Connect your iPhone, pick it at the top of the Xcode window, and press Run (⌘R). The first time, enable
Developer Mode on the phone (Settings → Privacy & Security) and trust your developer certificate (Settings →
General → VPN & Device Management) when asked.

Signing uses `ios/Local.xcconfig` (written by setup; see `ios/Local.example.xcconfig`): your team ID and a bundle
ID prefix you own, such as `com.yourname`. With a free Apple ID the app expires after 7 days: run it from Xcode
again. If Xcode says your team can't use App Groups (the app shares its settings with the share extension
through one), you need a paid Apple Developer account.

## Connect the phone

On the Mac run `./fish-reader pair`, then scan the QR code with the iPhone's **Camera** app and tap the link:
Reader opens and asks to connect. Or type the server address and token it prints into Reader → Settings.

## Using it

- **Add an article**: `+` in the app (it offers a copied link), or Safari → Share → **Reader**. Both let you pick
  the voice. Use Share for paid posts you're signed in to.
- **Listen**: it starts while the article is still being made. ±15 s, 0.8–2× speed (pitch-preserving, tuned for
  speech), lock screen and AirPods controls, background audio, and it remembers your place. Finished articles
  play offline.
- **Pause an article** that's being made (swipe right in the library, or the ••• menu) to free the Mac's GPU;
  resume later. If you quit the server, unfinished articles carry on when it starts again.
- **Voices**: `./fish-reader add-voice` adds another; choose the default in Settings or per article.

**Only use voices you have the right to use**: your own, or someone who agreed to have their voice cloned. Don't
use it to impersonate anyone.

## Configuration

`config.json` (created by setup; see `config.example.json`):

| key | default | |
|---|---|---|
| `port` | 8767 | the server listens on 127.0.0.1 only |
| `data_dir` | `./data` | articles and voices |
| `voices_dir` | `<data_dir>/voices` | one folder per voice: `voice.json`, `reference.wav`, `transcript.txt` |
| `voice` | `null` | default narrator (a voice folder name); `null` lets the model pick |
| `idle_unload_minutes` | 15 | free the model's memory after this long without work (0 = never) |
| `warm_on_start` | `false` | load the model when the server starts |
| `tagger` | `claude` | `claude` (falls back to `none` when Claude Code isn't installed or logged in) or `none` |
| `claude_model` | `sonnet` | model for the markup |
| `token` | generated | the app's password; keep it private |

Articles live in `data/articles/<id>/` (`parts/NN.m4a` per part, `audio.m4a` once finished).

## Troubleshooting

- **The app can't connect**: is `./fish-reader` running? Is Tailscale on, on both devices? `./fish-reader pair`
  warns when `tailscale serve` isn't set up. In the Tailscale admin console, MagicDNS and HTTPS certificates must
  be on.
- **"Couldn't load Drama 3" / the Mac runs out of memory**: close other big apps; don't run another copy of
  Drama 3 (such as fish-ui) with its model loaded at the same time.
- **No expressive markup**: the server log says why at startup (usually: Claude Code isn't logged in).
- **The iOS Simulator says "audio route descriptor"** (developers): coreaudiod is stuck; `sudo killall coreaudiod`.

## For developers

```sh
./scripts/check.sh                 # py_compile, smoke test, iOS compile (app + extension + tests)
IOS_DESTINATION="platform=iOS Simulator,name=iPhone 17 Pro" ./scripts/check.sh   # also runs the iOS unit tests
```

The smoke test runs the server with a fake model (test tones) and a fake tagger against a cluttered local page;
it never loads the real model or calls Claude. `FISH_READER_FAKE_MODEL=1` and `FISH_READER_TAGGER=fake|none`
work for manual testing too. The API is in [docs/API.md](docs/API.md).

`server/fish_core.py` is Drama 3's generation code from [fish-ui](https://github.com/ianiv/fish-ui), and
`server/engine.py` runs it in-process on one thread. `ios/project.yml` is the source of the Xcode project
(generated, gitignored): change settings there, not in Xcode.

### Cues

`server/text_rules.py` lists what the tagger may use: `SPAN_TAGS` (`<whisper>`, `<excited>`, `<sad>`, `<angry>`,
`<serious>`, `<sarcastic>`, `<shouting>`, `<emphasis>`, `<laughing>`, `<slow>`, `<fast>`) and `POINT_CUES`
(`[sighs]`, `[laughs]`, `[chuckles]`, `[pause]`). Spans are preferred because the voice returns to normal at the
closing tag. A test on Drama 3 (2 seeds, measured loudness) found `<whisper>…</whisper>` 12–16 dB quieter with an
immediate return to normal, while a `[whispers]` point cue was only about 5 dB quieter and left the next
sentence 2–3 dB quieter too. No tag name was ever spoken, and `<slow>`/`<fast>` changed the pace (9.8 s vs 7.2 s).
Spans can't nest or cross paragraphs and are at most 400 characters.

## License

This code is under the [MIT License](LICENSE).

Drama 3's weights are not part of this repo (each Mac downloads them) and are under the
[Fish Audio Community License](https://huggingface.co/fishaudio/drama-3/blob/main/LICENSE), which also covers
the audio you make with them: read it before use, especially for anything commercial.
