import XCTest
@testable import ArticleReader

final class ModelTests: XCTestCase {
    let json = """
    {"id": "20261124-151810-abc123", "url": "https://harbour.example/keeper", "title": "The Keeper Comes Home",
     "byline": "Ada Lovelace", "site": "Harbour News", "date": "", "status": "rendering", "step": "render",
     "error": null, "warnings": [], "created": 1795000000.5, "updated": 1795000010.0, "voice": null,
     "tagger": "fake", "source": "shared", "duration_s": null, "audio_url": null, "fish_job": "j1",
     "progress": {"parts_done": 1, "parts_total": 3},
     "parts": [
      {"index": 0, "text": "The Keeper Comes Home.", "tagged": "[sighs] The Keeper Comes Home.", "status": "done",
       "take": "tts/151810_x", "duration_s": 16.0, "start_s": 0.0, "audio_url": "/api/articles/x/parts/0.m4a", "warnings": []},
      {"index": 1, "text": "Why? Because.", "tagged": "[sighs] Why? [pause] Because.", "status": "rendering",
       "take": null, "duration_s": null, "start_s": null, "audio_url": null, "warnings": []},
      {"index": 2, "text": "End.", "tagged": null, "status": "pending", "take": null, "duration_s": null,
       "start_s": null, "audio_url": null, "warnings": []}
     ]}
    """

    func testDecodesArticle() throws {
        let a = try JSONDecoder.api.decode(Article.self, from: Data(json.utf8))
        XCTAssertEqual(a.title, "The Keeper Comes Home")
        XCTAssertEqual(a.status, .rendering)
        XCTAssertTrue(a.status.isActive)
        XCTAssertEqual(a.progress, Article.Progress(partsDone: 1, partsTotal: 3))
        XCTAssertEqual(a.parts?.count, 3)
        XCTAssertEqual(a.readyParts.map(\.index), [0])
        XCTAssertEqual(a.parts?[0].audioUrl, "/api/articles/x/parts/0.m4a")
    }

    func testDecodesHealthAndList() throws {
        let h = try JSONDecoder.api.decode(Health.self, from: Data(#"{"ok": true, "fish_ui": "ok", "model": {}, "tagger": "claude"}"#.utf8))
        XCTAssertEqual(h.fishUi, "ok")
        XCTAssertEqual(h.tagger, "claude")
        struct List: Decodable { let articles: [Article] }
        let summary = #"{"articles": [{"id": "a", "url": "https://x", "title": "", "status": "queued", "error": null, "created": 1, "progress": {"parts_done": 0, "parts_total": 0}}]}"#
        let l = try JSONDecoder.api.decode(List.self, from: Data(summary.utf8))
        XCTAssertEqual(l.articles.first?.displayTitle, "https://x")
        XCTAssertNil(l.articles.first?.parts)
    }

    func testCueRuns() {
        XCTAssertEqual(CueText.runs("[sighs] Why? [pause] Because."),
                       [.cue("sighs"), .text(" Why? "), .cue("pause"), .text(" Because.")])
        XCTAssertEqual(CueText.runs("plain"), [.text("plain")])
        XCTAssertEqual(CueText.runs("open [ only"), [.text("open [ only")])
        XCTAssertEqual(CueText.runs("He leaned in. <whisper>She's leaving.</whisper> [pause] Done."),
                       [.text("He leaned in. "), .span("whisper", "She's leaving."), .text(" "), .cue("pause"), .text(" Done.")])
        XCTAssertEqual(CueText.runs("a <sad>b"), [.text("a <sad>b")])
    }

    func testClientURLs() throws {
        let c = try XCTUnwrap(APIClient(server: "my-mac.tail.ts.net/", token: "t"))
        XCTAssertEqual(c.url("/api/articles/x/parts/0.m4a").absoluteString, "https://my-mac.tail.ts.net/api/articles/x/parts/0.m4a")
        XCTAssertEqual(c.request("GET", "/api/health").value(forHTTPHeaderField: "Authorization"), "Bearer t")
        XCTAssertNil(APIClient(server: "", token: "t"))
    }

    func testFormatTime() {
        XCTAssertEqual(formatTime(0), "0:00")
        XCTAssertEqual(formatTime(95.4), "1:35")
        XCTAssertEqual(formatTime(3725), "1:02:05")
    }
}
