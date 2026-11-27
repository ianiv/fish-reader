import Foundation
import Observation

/// The article list plus polling: while any article is being made, refresh every 2 s.
@MainActor @Observable
final class ArticleStore {
    var articles: [Article] = []
    var details: [String: Article] = [:]
    var error: String?
    var loading = false
    private var pollTask: Task<Void, Never>?

    var client: APIClient? { APIClient() }

    func refresh() async {
        guard let client else { return }
        loading = true
        defer { loading = false }
        do {
            articles = try await client.articles()
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
        startPollingIfNeeded()
    }

    func load(_ id: String) async {
        guard let client else { return }
        do {
            let a = try await client.article(id)
            details[id] = a
            if let i = articles.firstIndex(where: { $0.id == id }) { articles[i] = a }
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    func add(url: String, voice: String? = ReaderSettings.voice, podcast: PodcastOptions? = nil) async -> Article? {
        guard let client else { error = "Set the server in Settings first."; return nil }
        do {
            let a = try await client.add(url: url, voice: voice, podcast: podcast)
            articles.insert(a, at: 0)
            details[a.id] = a
            error = nil
            startPollingIfNeeded()
            return a
        } catch {
            self.error = error.localizedDescription
            return nil
        }
    }

    func delete(_ id: String) async {
        guard let client else { return }
        do {
            try await client.delete(id)
            PlayerController.shared.close(id)
            articles.removeAll { $0.id == id }
            details[id] = nil
            AudioCache.remove(id)
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// Pause (stops the server's work on it and frees its GPU) or resume an article.
    func setPaused(_ id: String, _ paused: Bool) async {
        guard let client else { return }
        do {
            let a = try await (paused ? client.pause(id) : client.resume(id))
            details[id] = a
            if let i = articles.firstIndex(where: { $0.id == id }) { articles[i].status = a.status }
            startPollingIfNeeded()
        } catch {
            self.error = error.localizedDescription
        }
    }

    func retry(_ id: String) async {
        guard let client else { return }
        do {
            details[id] = try await client.retry(id)
            startPollingIfNeeded()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private var anyActive: Bool { articles.contains { $0.status.isActive } }

    func startPollingIfNeeded() {
        guard pollTask == nil, anyActive else { return }
        pollTask = Task { [weak self] in
            while let self, !Task.isCancelled, self.anyActive {
                try? await Task.sleep(for: .seconds(2))
                guard let client = self.client, let list = try? await client.articles() else { continue }
                self.articles = list
                for a in list where self.details[a.id] != nil {
                    await self.load(a.id)
                }
            }
            self?.pollTask = nil
        }
    }
}

/// Downloaded part audio: Caches/articles/<id>/<n>.m4a.
enum AudioCache {
    static var root: URL {
        FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appendingPathComponent("articles")
    }

    static func file(_ article: String, part: Int) -> URL {
        root.appendingPathComponent(article).appendingPathComponent("\(part).m4a")
    }

    static func remove(_ article: String) {
        try? FileManager.default.removeItem(at: root.appendingPathComponent(article))
    }
}
