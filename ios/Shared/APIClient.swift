import Foundation

struct APIError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

/// The reader server's API (docs/API.md). Every request carries the bearer token.
struct APIClient {
    var baseURL: URL
    var token: String
    var session: URLSession = .shared

    init?(server: String = ReaderSettings.serverURL, token: String = ReaderSettings.token) {
        var s = server.trimmingCharacters(in: .whitespacesAndNewlines)
        while s.hasSuffix("/") { s.removeLast() }
        guard !s.isEmpty, !token.isEmpty, let url = URL(string: s.contains("://") ? s : "https://" + s) else { return nil }
        baseURL = url
        self.token = token
    }

    func url(_ path: String) -> URL {
        URL(string: path, relativeTo: baseURL)?.absoluteURL ?? baseURL.appendingPathComponent(path)
    }

    func request(_ method: String, _ path: String, body: [String: Any]? = nil) -> URLRequest {
        var r = URLRequest(url: url(path))
        r.httpMethod = method
        r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        r.timeoutInterval = 30
        if let body {
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
            r.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        return r
    }

    private func send<T: Decodable>(_ r: URLRequest, as: T.Type) async throws -> T {
        let (data, resp) = try await session.data(for: r)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(code) else {
            let msg = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["error"] as? String
            throw APIError(message: msg ?? "server answered \(code)")
        }
        return try JSONDecoder.api.decode(T.self, from: data)
    }

    func health() async throws -> Health { try await send(request("GET", "/api/health"), as: Health.self) }

    func articles() async throws -> [Article] {
        struct List: Decodable { let articles: [Article] }
        return try await send(request("GET", "/api/articles"), as: List.self).articles
    }

    func article(_ id: String) async throws -> Article { try await send(request("GET", "/api/articles/\(id)"), as: Article.self) }

    func add(url: String, voice: String?, html: String? = nil) async throws -> Article {
        var body: [String: Any] = ["url": url]
        if let voice { body["voice"] = voice }
        if let html { body["html"] = html }
        var r = request("POST", "/api/articles", body: body)
        r.timeoutInterval = 60
        return try await send(r, as: Article.self)
    }

    func delete(_ id: String) async throws {
        struct Removed: Decodable { let removed: String }
        _ = try await send(request("DELETE", "/api/articles/\(id)"), as: Removed.self)
    }

    func retry(_ id: String) async throws -> Article { try await send(request("POST", "/api/articles/\(id)/retry"), as: Article.self) }

    func voices() async throws -> [Voice] {
        struct List: Decodable { let voices: [Voice] }
        return try await send(request("GET", "/api/voices"), as: List.self).voices
    }

    /// Downloads an audio path (e.g. a part's audio_url) to `dest`, replacing it.
    func download(_ path: String, to dest: URL) async throws {
        let (tmp, resp) = try await session.download(for: request("GET", path))
        guard (resp as? HTTPURLResponse)?.statusCode == 200 else { throw APIError(message: "audio not ready") }
        try? FileManager.default.removeItem(at: dest)
        try FileManager.default.createDirectory(at: dest.deletingLastPathComponent(), withIntermediateDirectories: true)
        try FileManager.default.moveItem(at: tmp, to: dest)
    }
}
