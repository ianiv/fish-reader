import SwiftUI
import UIKit

@main
struct ArticleReaderApp: App {
    @State private var store = ArticleStore()
    @Environment(\.scenePhase) private var phase

    var body: some Scene {
        WindowGroup {
            LibraryView()
                .environment(store)
                .environment(PlayerController.shared)
                .onChange(of: phase) { _, p in if p == .active { Task { await store.refresh() } } }
        }
    }
}

// MARK: library

struct LibraryView: View {
    @Environment(ArticleStore.self) private var store
    @Environment(PlayerController.self) private var player
    @State private var adding = false
    @State private var settings = !ReaderSettings.isConfigured
    @State private var path: [String] = []
    @State private var pairing: Pairing?

    var body: some View {
        NavigationStack(path: $path) {
            List {
                if let e = store.error {
                    Label(e, systemImage: "exclamationmark.triangle").foregroundStyle(.red).font(.footnote)
                }
                ForEach(store.articles) { a in
                    NavigationLink(value: a.id) { ArticleRow(article: a) }
                        .swipeActions {
                            Button("Delete", role: .destructive) { Task { await store.delete(a.id) } }
                        }
                        .swipeActions(edge: .leading) {
                            if a.status.isActive {
                                Button("Pause", systemImage: "pause") { Task { await store.setPaused(a.id, true) } }.tint(.orange)
                            } else if a.status == .paused {
                                Button("Resume", systemImage: "play") { Task { await store.setPaused(a.id, false) } }.tint(.green)
                            }
                        }
                }
            }
            .overlay {
                if store.articles.isEmpty && !store.loading {
                    ContentUnavailableView("No articles", systemImage: "text.page",
                                           description: Text("Tap + to paste a link, or share a page from Safari with “Read Aloud”."))
                }
            }
            .navigationTitle("Reader")
            .navigationDestination(for: String.self) { ArticleView(id: $0) }
            .refreshable { await store.refresh() }
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button { settings = true } label: { Image(systemName: "gearshape") }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { adding = true } label: { Image(systemName: "plus") }
                }
            }
            .safeAreaInset(edge: .bottom) {
                if let a = player.article {
                    MiniPlayer(article: a) { if path.last != a.id { path.append(a.id) } }
                }
            }
            .sheet(isPresented: $adding) {
                AddArticleView { id in path.append(id) }
            }
            .sheet(isPresented: $settings, onDismiss: { Task { await store.refresh() } }) { SettingsView() }
            .task { await store.refresh() }
        }
        .onOpenURL { url in
            guard let p = Pairing(url) else { return }
            settings = false  // (an open Settings sheet would hide the alert, and save its old values over these)
            Task {
                try? await Task.sleep(for: .milliseconds(600))
                pairing = p
            }
        }
        .alert("Connect to this server?", isPresented: Binding(get: { pairing != nil }, set: { if !$0 { pairing = nil } }),
               presenting: pairing) { p in
            Button("Connect") {
                ReaderSettings.serverURL = p.server
                ReaderSettings.token = p.token
                settings = false
                Task { await store.refresh() }
            }
            Button("Cancel", role: .cancel) {}
        } message: { p in
            Text(p.server)
        }
    }
}

/// A fishreader://pair?server=…&token=… link (the QR code printed by scripts/pair.py on the Mac).
struct Pairing {
    let server: String
    let token: String

    init?(_ url: URL) {
        guard url.scheme == "fishreader", url.host == "pair",
              let q = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems,
              let server = q.first(where: { $0.name == "server" })?.value, server.hasPrefix("http"),
              let token = q.first(where: { $0.name == "token" })?.value, !token.isEmpty else { return nil }
        self.server = server
        self.token = token
    }
}

struct ArticleRow: View {
    let article: Article

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(article.displayTitle).font(.headline).lineLimit(2)
            HStack(spacing: 6) {
                if let site = article.site, !site.isEmpty { Text(site) }
                StatusBadge(article: article)
                if let d = article.durationS { Text(formatTime(d)) }
            }
            .font(.caption).foregroundStyle(.secondary)
            if article.status.isActive, let p = article.progress, p.partsTotal > 0 {
                ProgressView(value: Double(p.partsDone), total: Double(p.partsTotal))
            }
        }
        .padding(.vertical, 2)
    }
}

struct StatusBadge: View {
    let article: Article

    var body: some View {
        switch article.status {
        case .done: EmptyView()
        case .error: Text("Failed").foregroundStyle(.red)
        default:
            if let p = article.progress, p.partsTotal > 0, article.status == .rendering {
                Text("\(article.status.label) \(p.partsDone)/\(p.partsTotal)")
            } else {
                Text(article.status.label)
            }
        }
    }
}

struct AddArticleView: View {
    @Environment(ArticleStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var url = ""
    @State private var busy = false
    @State private var voice = ReaderSettings.voice ?? ""  // "" = the server's default
    @State private var voices: [Voice] = []
    var opened: (String) -> Void

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("https://…", text: $url)
                        .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                } footer: {
                    Text("Members-only posts: open them in Safari while signed in and use Share → Read Aloud instead.")
                }
                Section {
                    Picker("Voice", selection: $voice) {
                        Text("Server default").tag("")
                        ForEach(voices) { Text($0.name).tag($0.id) }
                        if !voice.isEmpty, !voices.contains(where: { $0.id == voice }) { Text(voice).tag(voice) }
                    }
                } footer: {
                    Text("Starts on the narrator chosen in Settings.")
                }
                if let e = store.error { Text(e).foregroundStyle(.red).font(.footnote) }
            }
            .navigationTitle("Add article")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Read") {
                        busy = true
                        Task {
                            if let a = await store.add(url: url, voice: voice.isEmpty ? nil : voice) {
                                PlayerController.shared.open(a)
                                PlayerController.shared.play()  // starts live as soon as the first part renders
                                dismiss()
                                opened(a.id)
                            }
                            busy = false
                        }
                    }
                    .disabled(busy || URL(string: url)?.scheme?.hasPrefix("http") != true)
                }
            }
            .task { voices = (try? await APIClient()?.voices()) ?? [] }
            .onAppear {
                if url.isEmpty, UIPasteboard.general.hasURLs, let u = UIPasteboard.general.url { url = u.absoluteString }
            }
        }
        .presentationDetents([.medium])
    }
}

// MARK: article

struct ArticleView: View {
    let id: String
    @Environment(ArticleStore.self) private var store
    @Environment(PlayerController.self) private var player

    @State private var scrolled = false

    private var article: Article? { store.details[id] ?? store.articles.first { $0.id == id } }

    /// The part holding the playhead: the player's, if it has this article, else from the saved position.
    private func playheadPart(_ a: Article) -> Int {
        if player.article?.id == a.id { return player.currentPart }
        var t = UserDefaults.standard.double(forKey: "pos.\(a.id)")
        for p in a.parts ?? [] {
            guard let d = p.durationS, t >= d else { return p.index }
            t -= d
        }
        return max(0, (a.parts?.count ?? 1) - 1)
    }

    var body: some View {
        Group {
            if let a = article { content(a) } else { ProgressView() }
        }
        .navigationBarTitleDisplayMode(.inline)
        .task {
            await store.load(id)
            // take over the player only when it isn't busy with another article
            if let a = store.details[id], player.article?.id != id, !player.isPlaying,
               !a.readyParts.isEmpty || a.status.isActive { player.open(a) }
            store.startPollingIfNeeded()
        }
        .onChange(of: store.details[id]) { _, a in
            if let a, player.article?.id == a.id { player.open(a) }
        }
    }

    @ViewBuilder private func content(_ a: Article) -> some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text(a.displayTitle).font(.title2.bold())
                        Text([a.byline, a.site].compactMap { $0?.isEmpty == false ? $0 : nil }.joined(separator: " · "))
                            .font(.subheadline).foregroundStyle(.secondary)
                        if a.status != .done {
                            StatusLine(article: a, retry: { Task { await store.retry(a.id) } },
                                       resume: { Task { await store.setPaused(a.id, false) } })
                        }
                        if let w = a.warnings, !w.isEmpty {
                            DisclosureGroup("\(w.count) warning\(w.count == 1 ? "" : "s")") {
                                ForEach(w, id: \.self) { Text($0).font(.caption) }
                            }
                            .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    ForEach(a.parts ?? []) { p in
                        PartText(part: p, current: player.article?.id == a.id && player.currentPart == p.index,
                                 playable: player.article?.id == a.id ? player.canPlay(part: p.index) : p.isReady)
                            .id(p.index)
                            .onTapGesture {
                                if player.article?.id != a.id { player.open(a) }
                                player.seek(toPart: p.index)
                            }
                    }
                }
                .padding()
            }
            .onChange(of: player.currentPart) { _, n in
                if player.article?.id == a.id, player.isPlaying { withAnimation { proxy.scrollTo(n, anchor: .top) } }
            }
            .task(id: a.parts?.count ?? 0) {  // on opening: jump to the part the playhead is in, once the parts are in
                guard !scrolled, let parts = a.parts, !parts.isEmpty else { return }
                scrolled = true
                let n = playheadPart(a)
                guard n > 0 else { return }
                try? await Task.sleep(for: .milliseconds(50))  // let the parts lay out first
                proxy.scrollTo(n, anchor: .top)
            }
        }
        .safeAreaInset(edge: .bottom) {
            if player.article?.id == a.id { PlayerBar() }
        }
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    if let u = URL(string: a.url) { Link("Open original", destination: u) }
                    if a.status.isActive {
                        Button("Pause reading", systemImage: "pause.circle") { Task { await store.setPaused(a.id, true) } }
                    } else if a.status == .paused {
                        Button("Resume reading", systemImage: "play.circle") { Task { await store.setPaused(a.id, false) } }
                    }
                    Button("Retry", systemImage: "arrow.clockwise") { Task { await store.retry(a.id) } }
                        .disabled(a.status.isActive)
                } label: { Image(systemName: "ellipsis.circle") }
            }
        }
    }
}

struct StatusLine: View {
    let article: Article
    var retry: () -> Void
    var resume: () -> Void = {}

    var body: some View {
        if article.status == .error {
            VStack(alignment: .leading, spacing: 6) {
                Label(article.error ?? "Failed", systemImage: "exclamationmark.triangle").foregroundStyle(.red)
                Button("Try again", action: retry).buttonStyle(.bordered)
            }
            .font(.footnote)
        } else if article.status == .paused {
            HStack {
                Label("Paused", systemImage: "pause.circle").foregroundStyle(.secondary)
                Button("Resume", action: resume).buttonStyle(.bordered)
            }
            .font(.footnote)
        } else if article.status.isActive {
            HStack {
                ProgressView()
                StatusBadge(article: article)
            }
            .font(.footnote).foregroundStyle(.secondary)
        }
    }
}

struct PartText: View {
    let part: Article.Part
    let current: Bool
    var playable = true

    var body: some View {
        Text(attributed)
            .font(.body)
            .lineSpacing(4)
            .opacity(part.isReady || playable ? 1 : 0.55)
            .padding(8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(current ? Color.accentColor.opacity(0.12) : .clear, in: RoundedRectangle(cornerRadius: 8))
    }

    private func label(_ s: String) -> AttributedString {
        var a = AttributedString(s)
        a.foregroundColor = .secondary
        a.font = .caption.italic()
        return a
    }

    private var attributed: AttributedString {
        var out = AttributedString()
        for run in CueText.runs(part.tagged ?? part.text) {
            switch run {
            case .text(let t):
                out += AttributedString(t)
            case .cue(let c):
                out += label("[\(c)]")
            case .span(let tag, let words):
                out += label("\(tag) ")
                var w = AttributedString(words)
                w.font = .body.italic()
                w.foregroundColor = .accentColor
                out += w
            }
        }
        return out
    }
}

// MARK: player

struct PlayerBar: View {
    @Environment(PlayerController.self) private var player
    private let rates: [Float] = [0.8, 1.0, 1.2, 1.5, 1.75, 2.0]

    var body: some View {
        @Bindable var player = player
        VStack(spacing: 8) {
            ProgressView(value: min(player.time, max(player.duration, 1)), total: max(player.duration, 1))
            HStack {
                Text(formatTime(player.time))
                Spacer()
                if player.isWaiting && player.isPlaying { Text(player.time < 0.5 ? "starting…" : "waiting for audio…") }
                else if player.isLive { Label("live", systemImage: "dot.radiowaves.left.and.right") }
                Spacer()
                Text(formatTime(player.duration))
            }
            .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            HStack(spacing: 32) {
                Menu {
                    Picker("Speed", selection: $player.rate) {
                        ForEach(rates, id: \.self) { Text(String(format: "%g×", $0)).tag($0) }
                    }
                } label: { Text(String(format: "%g×", player.rate)).font(.callout.monospacedDigit()).frame(width: 44) }
                Button { player.skip(-15) } label: { Image(systemName: "gobackward.15") }
                Button { player.toggle() } label: {
                    Image(systemName: player.isPlaying ? "pause.circle.fill" : "play.circle.fill").font(.system(size: 48))
                }
                .disabled(player.readyDuration == 0 && player.livePartIndex == nil)
                Button { player.skip(15) } label: { Image(systemName: "goforward.15") }
                Color.clear.frame(width: 44, height: 1)
            }
            .font(.title2)
        }
        .padding(.horizontal).padding(.vertical, 10)
        .background(.bar)
    }
}

struct MiniPlayer: View {
    let article: Article
    var open: () -> Void
    @Environment(PlayerController.self) private var player

    var body: some View {
        HStack {
            Button(action: open) {
                VStack(alignment: .leading) {
                    Text(article.displayTitle).font(.subheadline.bold()).lineLimit(1)
                    Text("\(formatTime(player.time)) / \(formatTime(player.duration))").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .buttonStyle(.plain)
            Button { player.toggle() } label: { Image(systemName: player.isPlaying ? "pause.fill" : "play.fill").font(.title2) }
        }
        .padding()
        .background(.bar)
    }
}

// MARK: settings

struct SettingsView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var server = ReaderSettings.serverURL
    @State private var token = ReaderSettings.token
    @State private var voice = ReaderSettings.voice ?? ""
    @State private var voices: [Voice] = []
    @State private var status: String?
    @State private var ok = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("https://my-mac.tailnet.ts.net", text: $server)
                        .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                    SecureField("Token", text: $token).textInputAutocapitalization(.never).autocorrectionDisabled()
                    Button("Test connection") { Task { await test() } }
                    if let status { Text(status).font(.footnote).foregroundStyle(ok ? .green : .red) }
                } header: { Text("Server") } footer: {
                    Text("The fish-reader server on your Mac, through `tailscale serve`. Easiest: on the Mac run `./fish-reader pair` and scan the QR code with the Camera app. The token is also in the server's config.json.")
                }
                Section("Narrator") {
                    Picker("Voice", selection: $voice) {
                        Text("Server default").tag("")
                        ForEach(voices) { Text($0.name).tag($0.id) }
                    }
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { save(); dismiss() }
                }
            }
            .task { if ReaderSettings.isConfigured { await test() } }
        }
    }

    private func save() {
        ReaderSettings.serverURL = server
        ReaderSettings.token = token
        ReaderSettings.voice = voice.isEmpty ? nil : voice
    }

    private func test() async {
        guard let client = APIClient(server: server, token: token) else { status = "Enter the server URL and token"; ok = false; return }
        do {
            let h = try await client.health()
            voices = (try? await client.voices()) ?? []
            ok = h.model.state != "error"
            status = ok ? "Connected. \(h.modelLabel), tagger: \(h.tagger)" : "Reader is up, but \(h.modelLabel)"
            save()
        } catch {
            ok = false
            status = error.localizedDescription
        }
    }
}

/// An audio time as listening time: at the 1× speed (PlayerController.baseSpeed) that's real time.
func formatTime(_ audioSeconds: Double) -> String {
    let s = audioSeconds / Double(PlayerController.baseSpeed)
    guard s.isFinite, s > 0 else { return "0:00" }
    let t = Int(s.rounded())
    return t >= 3600 ? String(format: "%d:%02d:%02d", t / 3600, t / 60 % 60, t % 60) : String(format: "%d:%02d", t / 60, t % 60)
}
