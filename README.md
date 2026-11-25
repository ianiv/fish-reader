# fish-reader

Listen to web articles on your iPhone, read by Fish Audio Drama 3 with expression.

```
iPhone app ──HTTPS (Tailscale)──▶ fish-reader server (Mac, runs Drama 3 itself)
                                   fetch → extract → mark up delivery → render → m4a
```

1. The phone sends a URL (or, from Safari's share sheet, the page itself, so members-only posts work).
2. The server finds the article text (trafilatura), leaving out menus, ads, comments and reference lists.
3. Claude (`claude -p`, the Claude Code CLI with its own login, no API key) marks up the delivery without
   changing a word: span tags such as `<whisper>…</whisper>`, `<sad>…</sad>` or `<sarcastic>…</sarcastic>` for
   a stretch of words, after which the voice returns to normal narration, and point cues such as `[sighs]` or
   `[pause]`. A guard checks the words and the markup.
4. Drama 3 renders it in parts of about 70 s, each continuing from the previous one so the voice and delivery
   carry on. The phone plays each part live as it's generated, starting with the spoken title.

## Mac setup

Needs an Apple silicon Mac with about 30 GB free for Drama 3 (4-bit, mlx-audio 0.6.7), `ffmpeg`, the `claude`
CLI (logged in) and Tailscale. fish-ui isn't needed: its generation code is copied into `server/fish_core.py`
(see its docstring) and `server/engine.py` runs it in-process. Don't run both with the model loaded at once.

```sh
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -r requirements.txt
./fish-reader                     # 127.0.0.1:8767; writes config.json with a token on first run
tailscale serve --bg --https=443 http://127.0.0.1:8767
```

The weights (`mlx-community/fish-audio-drama-3-4bit`) download to the Hugging Face cache on first use. The model
loads with the first article (about 10 s) and unloads after `idle_unload_minutes` without work.

The phone then uses `https://<mac-name>.<tailnet>.ts.net` and the `token` from `config.json`. The server only
binds to 127.0.0.1; `tailscale serve` gives tailnet-only HTTPS with a real certificate.

`config.json` (see `config.example.json`): `port`, `data_dir`, `voices_dir` (default `<data_dir>/voices`),
`voice` (default narrator: a voice id; `null` lets the model pick one, and continuation keeps it),
`idle_unload_minutes` (default 15, 0 = never), `warm_on_start`, `tagger` (`claude` | `none`), `claude_model`
(default `sonnet`), `token`. `FISH_READER_TAGGER=fake|none` overrides the tagger; `FISH_READER_FAKE_MODEL=1`
runs a stub model that makes test tones.

A voice gives the steadiest narrator across a long article. Voices use fish-ui's folder layout:
`<voices_dir>/<id>/voice.json` (`{name, description}`), `reference.wav` and `transcript.txt` (needed; voices
without one aren't offered). Copy them from fish-ui's `data/voices`, or point `voices_dir` there, then pick one
in the app's Settings.

## iOS app

```sh
cd ios && xcodegen generate && open ArticleReader.xcodeproj
```

`ArticleReader.xcodeproj` is generated (and gitignored): change settings in `ios/project.yml`, not in Xcode, or the next `xcodegen generate` drops them. Before running on your phone, check `DEVELOPMENT_TEAM` (currently `7EN8DG9MKN`) and, if you like,
`BUNDLE_PREFIX` / `APP_GROUP` in `ios/project.yml`, then regenerate. The app and the **Read Aloud** share
extension share the server URL and token through the app group.

- Library of articles with progress; `+` pastes a link.
- Safari → Share → **Read Aloud** sends the page as you see it (signed in), so paid posts work.
- Article view: text with cues shown dimmed and tagged spans in italics, the current part highlighted, tap a part to jump there.
- Player: play/pause, ±15 s, 0.8–2× speed, lock screen controls, background audio, remembers your place.
  Parts are cached on the phone, so finished articles play offline.

## Quality gate

```sh
./scripts/check.sh                 # py_compile, smoke test, iOS compile (app + extension + tests)
IOS_DESTINATION="platform=iOS Simulator,name=iPhone 17 Pro" ./scripts/check.sh   # also runs the iOS unit tests
```

The smoke test runs the reader with the fake model and a fake tagger against a cluttered local article page. It never loads the real model or calls Claude.

## Cues

`server/text_rules.py` lists what the tagger may use: `SPAN_TAGS` (`<whisper>`, `<excited>`, `<sad>`, `<angry>`,
`<serious>`, `<sarcastic>`, `<shouting>`, `<emphasis>`, `<laughing>`, `<slow>`, `<fast>`) and `POINT_CUES`
(`[sighs]`, `[laughs]`, `[chuckles]`, `[pause]`). Spans are preferred because the voice returns to normal at the
closing tag. A test on Drama 3 (2 seeds, measured loudness) found `<whisper>…</whisper>` 12–16 dB quieter with an
immediate return to normal, while a `[whispers]` point cue was only about 5 dB quieter and left the next
sentence 2–3 dB quieter too. No tag name was ever spoken, and `<slow>`/`<fast>` changed the pace (9.8 s vs 7.2 s).
The model card only documents point cues, so listen and prune the lists. Spans can't nest or cross paragraphs and
are at most 400 characters (Drama 3's chunker never cuts inside one). Later: an Apple Foundation Models tagger
behind the same `Tagger` interface.

## License

MIT (this code). Drama 3's weights are under the
[Fish Audio Community License](https://huggingface.co/fishaudio/drama-3/blob/main/LICENSE), which applies to any audio you make with them.
`server/fish_core.py` comes from fish-ui (MIT, same author).
