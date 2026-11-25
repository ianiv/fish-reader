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
    var voice = ReaderSettings.voice ?? ""  // "" = the server's default
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
    }

    func send() async {
        guard let client = APIClient() else { return fail("Open the Reader app and set the server first.") }
        state = "Sending…"
        failed = false
        sending = true
        defer { sending = false }
        do {
            _ = try await client.add(url: url, voice: voice.isEmpty ? nil : voice, html: html)
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
            VStack(spacing: 16) {
                Image(systemName: model.sent ? "checkmark.circle.fill" : "waveform")
                    .font(.system(size: 44)).foregroundStyle(model.sent ? .green : .accentColor)
                Text(model.title.isEmpty ? model.url : model.title).font(.headline).multilineTextAlignment(.center).lineLimit(3)
                if model.ready && !model.sent {
                    LabeledContent("Voice") {
                    Picker("Voice", selection: $model.voice) {
                        Text("Server default").tag("")
                        ForEach(model.voices) { Text($0.name).tag($0.id) }
                        if !model.voice.isEmpty, !model.voices.contains(where: { $0.id == model.voice }) {
                            Text(model.voice).tag(model.voice)
                        }
                    }
                    .pickerStyle(.menu)
                    }
                    .frame(maxWidth: 320)
                    Button(model.failed ? "Try again" : "Read") { Task { await model.send() } }
                        .buttonStyle(.borderedProminent).controlSize(.large).disabled(model.sending)
                }
                if !model.state.isEmpty {
                    Text(model.state).font(.subheadline).foregroundStyle(model.failed ? .red : .secondary).multilineTextAlignment(.center)
                }
            }
            .padding()
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .navigationTitle("Read Aloud")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Close") { model.done() } }
            }
        }
    }
}
