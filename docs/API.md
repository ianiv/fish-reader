# fish-reader API

Server: `server/server.py`, default `127.0.0.1:8767`. Every `/api` request needs `Authorization: Bearer <token>`
(`token` in `config.json`, made on first run); otherwise 401 `{error}`. JSON unless noted.

| Method + path | Body | Returns |
|---|---|---|
| `GET /api/health` | | `{ok, model: {key: "drama3", state: empty\|loading\|warming\|ready\|idle-unloaded\|error, error, load_s, warm_s, busy, pending, rtf, fake}, tagger}` |
| `GET /api/voices` | | `{voices: [{id, name, description}]}` (voices in `voices_dir` that have a transcript) |
| `POST /api/articles` | `{url, voice?, html?, kind?, hosts?, length?}` | 201 article. `html` = the page as the phone saw it (share sheet), used instead of fetching (members-only posts). `kind: "podcast"` makes a two-host conversation instead of a reading: `hosts` = two voice ids (default config `podcast_hosts`), `length` = `short` \| `medium` \| `long` (~5/10/20 min); needs the Claude tagger. 400 for non-http(s) URLs, or (without `html`) hosts that resolve to private/local addresses |
| `GET /api/articles` | | `{articles: [summary]}` newest first; summary = article without `parts`, `warnings` |
| `GET /api/articles/<id>` | | article |
| `DELETE /api/articles/<id>` | | `{removed}`; stops its render after the current piece |
| `POST /api/articles/<id>/retry` | | article; re-queues a failed/cancelled one, skipping finished steps |
| `POST /api/articles/<id>/pause` | | article; stops work on it (`paused`, kept across restarts). A running render stops and that part starts over on resume; the model unloads when nothing else is queued |
| `POST /api/articles/<id>/resume` | | article; re-queues a paused one |
| `GET /api/articles/<id>/parts/<n>.m4a` | | AAC audio of part n (Range supported); 404 until ready |
| `GET /api/articles/<id>/audio.mp3` | | the finished article (or podcast) as one MP3 with ID3 tags (title, artist, album), sent as an attachment named after the title; encoded on the first request and kept. 409 until it's done |
| `GET /api/articles/<id>/audio.m4a` | | the whole article, once `done` |
| `GET /api/articles/<id>/parts/<n>/live` | | part n as it renders: `application/octet-stream`, raw s16le mono PCM (`X-Sample-Rate: 44100`, `X-Sample-Format: s16le`) from the part's start, closing when the part ends. Waits up to 2 min for the part to start; 409 once it's done (play the m4a) |

Article:

```
{id, url, final_url?, source: fetched|shared, title, byline, site, date, voice, tagger,
 status: queued|fetching|extracting|scripting|tagging|rendering|paused|done|error|cancelled, kind: article|podcast, hosts, host_names, length (podcasts), step, error, warnings: [str],
 progress: {parts_done, parts_total}, duration_s, audio_url, created, updated, fish_job,
 parts: [{index, text, tagged (null until tagged), status: pending|tagged|rendering|done,
          turns (podcasts: [{speaker: 0|1, text}]; text/tagged then read "<host name>: …" per turn), continued (rendered as a continuation of part n-1), voice_sample ("part k" when part k of this article was the voice reference: no library voice and `continuation` off), gain (normalization factor | null), duration_s, start_s (offset in the whole article), audio_url, warnings}]}
```

Pipeline per article: fetch (unless `html` was sent) → extract with trafilatura (title, byline, paragraphs; links,
footnote markers, long bullet lists and citations dropped) → split into parts of ≤ 850 characters at paragraph
or sentence ends (spoken title and byline first) → tag each part (one part ahead of rendering) → one streamed
Drama 3 take per part (`engine.py`) with the article's voice (with `continuation` on, also continuing from the
previous part's WAV and text, ≤ 90 s; with no voice, the first part of 8 s or more becomes the voice sample) →
`parts/NN.m4a` → `audio.m4a`.

Podcasts: fetch → extract → `scripting`: Claude (`taggers.ClaudeScriptWriter`, one `claude -p` call) writes the
conversation as `A:`/`B:` lines, saved as `script.txt` → `text_rules.parse_script` keeps known markup only and
joins consecutive lines by one host → split into parts of ≤ 400 characters (`PODCAST_PART_CHARS`) between turns
(a longer turn is cut at sentence ends) → one multi-speaker take per part (`Engine.render_dialogue`), speakers
numbered in the order they first speak in that part, each with its host's voice reference. Files: `data/articles/<id>/{article.json, source.html, parts/NN.wav (until joined), parts/NN.m4a, audio.m4a}`. Unfinished
articles resume on restart.

Tagging: the tagger may only insert `text_rules.SPAN_TAGS` spans (`<tag>…</tag>`, balanced, not nested, within one
paragraph, ≤ 400 chars) and `POINT_CUES` (`[cue]`). The guard compares the words with markup removed and checks the spans; on a mismatch (or a tagger error) it retries once, then reads that part untagged and adds a warning.
