import SwiftUI
import UIKit
import UniformTypeIdentifiers

/// "Read Aloud" share extension: takes the shared page (URL, plus its HTML when shared from Safari, via
/// GetPage.js), lets you pick the voice and posts it to the reader server. Sending the HTML lets members-only posts work, since Safari
/// has your login.
final class ShareViewController: UIViewController {
    private let model = ShareModel()

    override func viewDidLoad() {
        super.viewDidLoad()
        model.done = { [weak self] in self?.extensionContext?.completeRequest(returningItems: nil) }
        let host = UIHostingController(rootView: ShareView(model: model))
        addChild(host)
        host.view.frame = view.bounds
        host.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        view.addSubview(host.view)
        host.didMove(toParent: self)
        Task { await model.load(extensionContext?.inputItems as? [NSExtensionItem] ?? []) }
    }
}

@MainActor @Observable
final class ShareModel {
    var title = ""
    var url = ""
    var state = "Getting the page…"
    var failed = false
    var ready = false   // the page is loaded: pick a voice, then Read
    var sending = false
    var sent = false
    let choice = ReadingChoice()
    var voices: [Voice] = []
    var done: () -> Void = {}
    private var html: String?

    func load(_ items: [NSExtensionItem]) async {
        let providers = items.flatMap { $0.attachments ?? [] }
        let pageType = UTType.propertyList.identifier
        if let p = providers.first(where: { $0.hasItemConformingToTypeIdentifier(pageType) }),
           let dict = try? await p.loadItem(forTypeIdentifier: pageType) as? NSDictionary,
           let results = dict[NSExtensionJavaScriptPreprocessingResultsKey] as? [String: Any] {
            url = results["url"] as? String ?? ""
            title = results["title"] as? String ?? ""
            html = results["html"] as? String
        } else if let p = providers.first(where: { $0.hasItemConformingToTypeIdentifier(UTType.url.identifier) }),
                  let u = try? await p.loadItem(forTypeIdentifier: UTType.url.identifier) as? URL {
            url = u.absoluteString
        }
        guard !url.isEmpty else { return fail("Nothing to read here: share a web page.") }
        guard let client = APIClient() else { return fail("Open the Reader app and set the server first.") }
        ready = true
        state = ""
        voices = (try? await client.voices()) ?? []
        choice.fillHosts(voices)
    }

    func send() async {
        guard let client = APIClient() else { return fail("Open the Reader app and set the server first.") }
        state = "Sending…"
        failed = false
        sending = true
        defer { sending = false }
        do {
            _ = try await client.add(url: url, voice: choice.narrator, html: html, podcast: choice.options)
            choice.remember()
            sent = true
            state = "Added. It'll be ready in the Reader app."
            try? await Task.sleep(for: .seconds(1.2))
            done()
        } catch {
            fail(error.localizedDescription)
        }
    }

    private func fail(_ msg: String) {
        state = msg
        failed = true
    }
}

struct ShareView: View {
    @Bindable var model: ShareModel

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    HStack(spacing: 12) {
                        Image(systemName: model.sent ? "checkmark.circle.fill" : "waveform")
                            .font(.title).foregroundStyle(model.sent ? .green : .accentColor)
                        Text(model.title.isEmpty ? model.url : model.title).font(.headline).lineLimit(3)
                    }
                }
                if model.ready && !model.sent {
                    Section {
                        ReadingOptions(model: model.choice, voices: model.voices)
                    }
                    Section {
                        Button(model.failed ? "Try again" : (model.choice.podcast ? "Make podcast" : "Read")) {
                            Task { await model.send() }
                        }
                        .frame(maxWidth: .infinity)
                        .disabled(model.sending || !model.choice.canSend)
                    }
                }
                if !model.state.isEmpty {
                    Text(model.state).font(.subheadline).foregroundStyle(model.failed ? .red : .secondary)
                }
            }
            .navigationTitle("Read Aloud")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Close") { model.done() } }
            }
        }
    }
}
