import Foundation

/// Mirrors the reader server's article JSON (docs/API.md).
struct Article: Codable, Identifiable, Hashable {
    let id: String
    let url: String
    var title: String
    var site: String?
    var byline: String?
    var status: Status
    var error: String?
    var created: Double
    var updated: Double?
    var progress: Progress?
    var durationS: Double?
    var audioUrl: String?
    var voice: String?
    var warnings: [String]?
    var source: String?
    var parts: [Part]?

    enum Status: String, Codable {
        case queued, fetching, extracting, tagging, rendering, done, error, cancelled

        var isActive: Bool { [.queued, .fetching, .extracting, .tagging, .rendering].contains(self) }

        var label: String {
            switch self {
            case .queued: "Queued"
            case .fetching: "Fetching"
            case .extracting: "Finding the article"
            case .tagging: "Adding expression"
            case .rendering: "Reading aloud"
            case .done: "Ready"
            case .error: "Failed"
            case .cancelled: "Cancelled"
            }
        }
    }

    struct Progress: Codable, Hashable {
        var partsDone: Int
        var partsTotal: Int
    }

    struct Part: Codable, Hashable, Identifiable {
        let index: Int
        var text: String
        var tagged: String?
        var status: String
        var durationS: Double?
        var startS: Double?
        var audioUrl: String?
        var warnings: [String]?
        var id: Int { index }
        var isReady: Bool { status == "done" && audioUrl != nil && durationS != nil }
    }

    var displayTitle: String { title.isEmpty ? url : title }
    var readyParts: [Part] {
        var out: [Part] = []
        for p in parts ?? [] {  // only the leading run of finished parts can be played in order
            guard p.isReady else { break }
            out.append(p)
        }
        return out
    }
}

struct Voice: Codable, Identifiable, Hashable {
    let id: String
    var name: String
    var description: String?
}

struct Health: Codable {
    var ok: Bool
    var fishUi: String
    var tagger: String
}

/// Marked-up text split into runs: plain text, [point] cues and <tag>…</tag> spans (no nesting).
enum CueText {
    enum Run: Hashable {
        case text(String)
        case cue(String)
        case span(String, String)  // tag, the words it wraps
    }

    private static let markup = /<([A-Za-z][\w-]*)>(.*?)<\/\1>|\[([^\[\]]*)\]/.dotMatchesNewlines()

    static func runs(_ s: String) -> [Run] {
        var out: [Run] = []
        var rest = s[...]
        while let m = rest.firstMatch(of: markup) {
            if m.range.lowerBound > rest.startIndex { out.append(.text(String(rest[..<m.range.lowerBound]))) }
            if let tag = m.output.1, let inner = m.output.2 {
                out.append(.span(String(tag), String(inner)))
            } else if let cue = m.output.3?.trimmingCharacters(in: .whitespaces), !cue.isEmpty {
                out.append(.cue(cue))
            }
            rest = rest[m.range.upperBound...]
        }
        if !rest.isEmpty { out.append(.text(String(rest))) }
        return out
    }
}

extension JSONDecoder {
    static let api: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()
}
