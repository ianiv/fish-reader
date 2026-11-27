import SwiftUI

/// What to make from a page, for the add sheet and the share sheet: a reading (one narrator voice) or a podcast
/// (Claude writes a two-host conversation about it: two host voices and a length). Rows for a Form.
struct ReadingOptions: View {
    @Bindable var model: ReadingChoice
    let voices: [Voice]

    var body: some View {
        Picker("Make", selection: $model.podcast) {
            Text("Reading").tag(false)
            Text("Podcast").tag(true)
        }
        .pickerStyle(.segmented)
        if model.podcast {
            voicePicker("Host A", $model.hostA, allowDefault: false)
            voicePicker("Host B", $model.hostB, allowDefault: false)
            Picker("Length", selection: $model.length) {
                ForEach(PodcastOptions.lengths, id: \.0) { Text($0.1).tag($0.0) }
            }
            if let problem = model.problem { Text(problem).font(.footnote).foregroundStyle(.secondary) }
        } else {
            voicePicker("Voice", $model.voice, allowDefault: true)
        }
    }

    private func voicePicker(_ title: String, _ selection: Binding<String>, allowDefault: Bool) -> some View {
        Picker(title, selection: selection) {
            if allowDefault { Text("Server default").tag("") } else { Text("Choose…").tag("") }
            ForEach(voices) { Text($0.name).tag($0.id) }
            if !selection.wrappedValue.isEmpty, !voices.contains(where: { $0.id == selection.wrappedValue }) {
                Text(selection.wrappedValue).tag(selection.wrappedValue)
            }
        }
    }
}

/// The choices behind ReadingOptions, starting from the last ones used.
@MainActor @Observable
final class ReadingChoice {
    var podcast = false
    var voice = ReaderSettings.voice ?? ""  // "" = the server's default
    var hostA = ReaderSettings.podcastHosts.first ?? ""
    var hostB = ReaderSettings.podcastHosts.dropFirst().first ?? ""
    var length = ReaderSettings.podcastLength

    /// Why a podcast can't be made yet (nil when it can).
    var problem: String? {
        if hostA.isEmpty || hostB.isEmpty { return "Pick a voice for each host." }
        if hostA == hostB { return "Pick two different voices." }
        return nil
    }

    var canSend: Bool { !podcast || problem == nil }

    var options: PodcastOptions? { podcast ? PodcastOptions(hosts: [hostA, hostB], length: length) : nil }
    var narrator: String? { podcast || voice.isEmpty ? nil : voice }

    /// Fill empty hosts once the voices are known: the narrator first, then another voice.
    func fillHosts(_ voices: [Voice]) {
        let ids = voices.map(\.id)
        if hostA.isEmpty || !ids.contains(hostA) { hostA = ids.contains(voice) ? voice : (ids.first ?? "") }
        if hostB.isEmpty || !ids.contains(hostB) || hostB == hostA { hostB = ids.first { $0 != hostA } ?? "" }
    }

    /// Remember the podcast choices for next time.
    func remember() {
        guard podcast else { return }
        ReaderSettings.podcastHosts = [hostA, hostB]
        ReaderSettings.podcastLength = length
    }
}
