# fish-reader

Listen to web articles on your iPhone, read by Fish Audio Drama 3 with expression.

```
iPhone app ──HTTPS (Tailscale)──▶ fish-reader server (Mac) ──▶ fish-ui (Mac, Drama 3)
                                   fetch → extract → mark up delivery → render → m4a
```

1. The phone sends a URL (or, from Safari's share sheet, the page itself, so members-only posts work).
2. The server finds the article text (trafilatura), leaving out menus, ads, comments and reference lists.
3. Claude (`claude -p`, the Claude Code CLI with its own login, no API key) marks up the delivery without
   changing a word: span tags such as `<whisper>…</whisper>`, `<sad>…</sad>` or `<sarcastic>…</sarcastic>` for
   a stretch of words, after which the voice returns to normal narration, and point cues such as `[sighs]` or
   `[pause]`. A guard checks the words and the markup.
4. fish-ui renders it in parts of about 75 s, each continuing from the previous one so the voice and delivery
   carry on. The phone starts playing as soon as the first part is ready.

## Mac setup

Needs [fish-ui](../fish-ui) (running, with its own venv), `ffmpeg`, the `claude` CLI (logged in) and Tailscale.

```sh
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python trafilatura
(cd ../fish-ui && ./fish-ui)      # Drama 3 on 127.0.0.1:8766
./fish-reader                     # 127.0.0.1:8767; writes config.json with a token on first run
tailscale serve --bg --https=443 http://127.0.0.1:8767
```

The phone then uses `https://<mac-name>.<tailnet>.ts.net` and the `token` from `config.json`. The server only
binds to 127.0.0.1; `tailscale serve` gives tailnet-only HTTPS with a real certificate.

`config.json` (see `config.example.json`): `fish_ui_url`, `port`, `data_dir`, `voice` (default narrator: a
fish-ui library voice id; `null` lets the model pick one, and continuation keeps it), `tagger`
(`claude` | `none`), `claude_model` (default `sonnet`), `token`. `FISH_READER_TAGGER=fake|none` overrides the
tagger.

A library voice gives the steadiest narrator across a long article: record or upload one in fish-ui's Library,
then pick it in the app's Settings.

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

The smoke test runs fish-ui's fake model (from `$FISH_UI_DIR`, default `~/Code/fish-ui`) and a fake tagger
against a cluttered local article page. It never loads the real model or calls Claude.

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
[Fish Audio Community License](https://huggingface.co/fishaudio/drama-3/blob/main/LICENSE), see fish-ui's README.
