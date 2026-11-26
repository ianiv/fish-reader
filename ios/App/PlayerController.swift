import AVFoundation
import UIKit
import MediaPlayer
import Observation

/// Plays an article as one timeline of parts (article time = sum of the earlier parts' durations + time in the
/// part). Every part goes through one AVAudioEngine: finished parts are downloaded (m4a) and decoded to samples;
/// the first part that isn't finished yet streams live while Drama 3 renders it (/parts/<n>/live), so sound
/// starts about a second after rendering does. Speed changes go through SpeechStretcher (pitch-period
/// cutting, kinder to voices than AVFoundation's stretchers). When a part runs out, the next one follows
/// without a gap if it's ready, else playback waits for it.
/// Position is remembered per article.
@MainActor @Observable
final class PlayerController {
    static let shared = PlayerController()

    private(set) var article: Article?
    private(set) var currentPart = 0
    private(set) var time: Double = 0
    private(set) var isPlaying = false
    private(set) var isWaiting = false  // playing, but the audio isn't there yet (part starting / buffering)
    var isLive: Bool { source?.live != nil }
    /// Drama 3 narrates slowly, so the speed shown as 1× plays at 1.2× (and 1.5× at 1.8×, and so on).
    static let baseSpeed: Float = 1.2
    /// The speed the user picked, relative to baseSpeed.
    var rate: Float = 1.0 {
        didSet {
            guard rate != oldValue else { return }
            if source != nil {  // restart from here with the new speed (scheduled audio is ~1.5 s ahead)
                updateClock()
                seek(to: time, play: isPlaying)
            }
            updateNowPlaying()
        }
    }

    /// The actual playback speed.
    private var speed: Float { rate * Self.baseSpeed }

    /// The part being fed to the engine: decoded file samples, or a live stream that is still growing.
    private struct Source {
        let index: Int
        let sampleRate: Double
        var samples: [Float] = []
        var live: LiveStream?
    }

    /// Scheduled output and the input it came from, for the clock.
    private struct Segment {
        let part: Int, outStart: Int, outEnd: Int, inStart: Int, inEnd: Int
        let sampleRate: Double
    }

    private let engine = AVAudioEngine()
    private let node = AVAudioPlayerNode()
    private var format: AVAudioFormat?
    private var source: Source?
    private var cursor = 0          // next input sample of `source` to stretch
    private var flushed = false     // the stretcher's tail of `source` is scheduled
    private var stretcher = SpeechStretcher(sampleRate: 44100, speed: 1)
    private var segments: [Segment] = []
    private var scheduledOut = 0    // output frames scheduled since the node last started
    private var pending = 0         // scheduled buffers not yet played
    private var generation = 0      // bumps on every node stop, so stale buffer completions are ignored
    private var seekToken = 0       // bumps on every seek, so a slow decode doesn't start a stale part
    private var decoded: [Int: (samples: [Float], sampleRate: Double)] = [:]  // current and next parts
    private var loading: Set<Int> = []
    private var downloading = false
    private var streamed: [Int: Double] = [:]  // durations of parts heard live, until the server reports them

    private var timer: Timer?

    // MARK: timeline

    var duration: Double {
        guard let a = article else { return 0 }
        return a.durationS ?? (a.parts ?? []).compactMap(\.durationS).reduce(0, +)
    }

    var readyDuration: Double { (article?.readyParts ?? []).compactMap(\.durationS).reduce(0, +) }

    /// Where part `index` starts, when every earlier part is finished (so its duration is known).
    func start(of index: Int) -> Double? {
        guard let parts = article?.parts, !parts.isEmpty else { return index == 0 ? 0 : nil }
        guard index <= parts.count else { return nil }
        var t = 0.0
        for p in parts.prefix(index) {
            guard let d = p.durationS ?? streamed[p.index] else { return nil }
            t += d
        }
        return t
    }

    /// The part that is playable live right now: the first unfinished one, while the article is being made.
    var livePartIndex: Int? {
        guard let a = article, a.status.isActive || a.status == .queued else { return nil }
        let ready = a.readyParts.count
        if let parts = a.parts, !parts.isEmpty { return ready < parts.count ? ready : nil }
        return 0  // not split yet: part 0 will be first
    }

    func canPlay(part index: Int) -> Bool {
        guard let a = article else { return false }
        return index < a.readyParts.count || index == livePartIndex
    }

    private init() {
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .spokenAudio)
        engine.attach(node)
        let nc = NotificationCenter.default
        nc.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil, queue: .main) { [weak self] n in
            let reason = (n.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt).flatMap(AVAudioSession.RouteChangeReason.init)
            MainActor.assumeIsolated { if reason == .oldDeviceUnavailable { self?.pause() } }  // headphones out
        }
        nc.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { [weak self] n in
            let type = (n.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt).flatMap(AVAudioSession.InterruptionType.init)
            MainActor.assumeIsolated { if type == .began { self?.pause() } }  // a call, Siri, an alarm
        }
        nc.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {  // the engine stopped (output changed): carry on from here
                guard let self, self.source != nil else { return }
                self.updateClock()
                self.seek(to: self.time, play: self.isPlaying)
            }
        }
        timer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.tick() }
        }
        setUpRemoteCommands()
    }

    // MARK: public

    /// Open (or update) an article. A new article starts at its saved position, paused.
    func open(_ a: Article) {
        if article?.id != a.id {
            if let old = article { savePosition(old.id) }
            stopAll()
            isPlaying = false
            streamed = [:]
            decoded = [:]
            article = a
            time = UserDefaults.standard.double(forKey: "pos.\(a.id)")
            currentPart = part(at: time)
        } else {
            article = a
            prepare(currentPart + 1)
            if isPlaying && pending == 0 && exhausted { feed() }  // e.g. the next part just got ready
        }
        downloadReady()
        updateNowPlaying()
    }

    /// The article was deleted: stop it, forget its position and leave the player empty (hides the player bar).
    func close(_ id: String) {
        guard article?.id == id else { return }
        stopAll()
        isPlaying = false
        article = nil
        decoded = [:]
        streamed = [:]
        time = 0
        currentPart = 0
        UserDefaults.standard.removeObject(forKey: "pos.\(id)")
        updateNowPlaying()
    }

    func toggle() { isPlaying ? pause() : play() }

    func play() {
        guard article != nil else { return }
        try? AVAudioSession.sharedInstance().setActive(true)
        guard source != nil else {
            seek(to: time, play: true)
            return
        }
        isPlaying = true
        feed()
        updateNowPlaying()
    }

    func pause() {
        updateClock()
        isPlaying = false
        isWaiting = false
        node.pause()
        engine.pause()  // a running engine counts as playing to iOS: the lock screen would keep showing Pause
        if let id = article?.id { savePosition(id) }
        updateNowPlaying()
    }

    /// Skip by `seconds` of listening time (at 1×), i.e. seconds * baseSpeed of audio.
    func skip(_ seconds: Double) {
        let seconds = seconds * Double(Self.baseSpeed)
        updateClock()
        let limit = livePartIndex.flatMap { i in start(of: i).map { $0 + liveAvailable(i) } } ?? readyDuration
        seek(to: max(0, min(time + seconds, max(0, limit - 0.5))), play: isPlaying)
    }

    func seek(toPart index: Int) {
        guard canPlay(part: index), let s = start(of: index) else { return }
        seek(to: s, play: true)
    }

    /// Go to article time `t`: a finished part plays from its (decoded) file, the live part from the stream.
    func seek(to t: Double, play: Bool) {
        guard let a = article else { return }
        let index = part(at: t)
        let offset = max(0, t - (start(of: index) ?? t))
        resetNode()
        seekToken += 1
        currentPart = index
        time = t
        isPlaying = play
        if index < a.readyParts.count {
            source?.live?.cancel()
            source = nil
            if let d = decoded[index] {
                begin(Source(index: index, sampleRate: d.sampleRate, samples: d.samples), at: offset)
            } else {
                isWaiting = play
                let token = seekToken
                load(index) { [weak self] in
                    guard let self, self.seekToken == token, let d = self.decoded[index] else { return }
                    self.begin(Source(index: index, sampleRate: d.sampleRate, samples: d.samples), at: offset)
                }
            }
        } else if index == livePartIndex {
            begin(liveSource(index), at: offset)
        } else {
            stopAll()
            isPlaying = false
        }
        updateNowPlaying()
    }

    /// The part containing `t`, limited to the parts that can play (the ready ones plus the live one).
    private func part(at t: Double) -> Int {
        guard let a = article, let parts = a.parts, !parts.isEmpty else { return 0 }
        var start = 0.0
        for p in parts {
            guard let d = p.durationS, p.isReady else { return p.index }  // the first unfinished part
            if t < start + d - 0.05 { return p.index }
            start += d
        }
        return parts.count - 1
    }

    private func stopAll() {
        resetNode()
        engine.pause()
        source?.live?.cancel()
        source = nil
        isWaiting = false
    }

    // MARK: sources

    private func liveSource(_ index: Int) -> Source {
        if let live = source?.live, live.index == index, live.articleID == article?.id, !live.failed {
            return Source(index: index, sampleRate: live.sampleRate, live: live)
        }
        source?.live?.cancel()
        let stream = LiveStream(articleID: article?.id ?? "", index: index)
        stream.onData = { [weak self] in self?.feed() }
        stream.onEnd = { [weak self] status in self?.liveEnded(status) }
        if let client = APIClient() { stream.start(client) }
        return Source(index: index, sampleRate: stream.sampleRate, live: stream)
    }

    private func begin(_ s: Source, at offset: Double) {
        source = s
        cursor = Int(offset * s.sampleRate)
        if s.live == nil { cursor = min(cursor, s.samples.count) }
        flushed = false
        stretcher = SpeechStretcher(sampleRate: s.sampleRate, speed: speed, start: cursor)
        isWaiting = isPlaying
        configure(s.sampleRate)
        feed()
        prepare(s.index + 1)
    }

    private func available(_ s: Source) -> Int { s.live?.samples.count ?? s.samples.count }
    private func isComplete(_ s: Source) -> Bool { s.live?.finished ?? true }

    /// Everything of the current part is scheduled.
    private var exhausted: Bool {
        guard let s = source else { return true }
        return isComplete(s) && cursor >= available(s) && flushed
    }

    private func liveAvailable(_ index: Int) -> Double {
        guard let live = source?.live, live.index == index else { return 0 }
        return Double(live.samples.count) / live.sampleRate
    }

    /// The next part follows on directly (no gap) when its audio is decoded; false if it isn't there yet.
    private func advance() -> Bool {
        guard let s = source, let a = article, let parts = a.parts else { return false }
        if let live = s.live { streamed[s.index] = Double(live.samples.count) / live.sampleRate }
        let next = s.index + 1
        guard next < parts.count else { return false }
        if next < a.readyParts.count, let d = decoded[next], d.sampleRate == format?.sampleRate {
            s.live?.cancel()
            source = Source(index: next, sampleRate: d.sampleRate, samples: d.samples)
        } else if next == livePartIndex {
            source = liveSource(next)
        } else {
            return false
        }
        cursor = 0
        flushed = false
        stretcher = SpeechStretcher(sampleRate: source!.sampleRate, speed: speed)
        decoded = decoded.filter { $0.key >= next }
        prepare(next + 1)
        return true
    }

    /// Download (if needed) and decode part `index` off the main thread; keeps only the current and next parts.
    private func load(_ index: Int, then done: @escaping () -> Void = {}) {
        guard let a = article, index < a.readyParts.count else { return }
        if decoded[index] != nil { done(); return }
        guard !loading.contains(index), let client = APIClient() else { return }
        let part = a.readyParts[index]
        loading.insert(index)
        Task {
            defer { loading.remove(index) }
            let file = AudioCache.file(a.id, part: index)
            if !FileManager.default.fileExists(atPath: file.path) {
                guard let url = part.audioUrl, (try? await client.download(url, to: file)) != nil else { return }
            }
            guard let d = await Task.detached(operation: { try? Self.decode(file) }).value,
                  article?.id == a.id else { return }
            decoded[index] = d
            decoded = decoded.filter { $0.key >= currentPart }
            done()
            if isPlaying && pending == 0 && exhausted { feed() }
        }
    }

    private func prepare(_ index: Int) { load(index) }

    /// Keep every finished part on disk, so the article plays offline.
    private func downloadReady() {
        guard !downloading, let a = article, let client = APIClient() else { return }
        downloading = true
        Task {
            defer { downloading = false }
            for p in a.readyParts {
                let file = AudioCache.file(a.id, part: p.index)
                guard !FileManager.default.fileExists(atPath: file.path), let url = p.audioUrl else { continue }
                guard article?.id == a.id, (try? await client.download(url, to: file)) != nil else { return }
            }
        }
    }

    nonisolated private static func decode(_ url: URL) throws -> (samples: [Float], sampleRate: Double) {
        let f = try AVAudioFile(forReading: url)
        guard let buf = AVAudioPCMBuffer(pcmFormat: f.processingFormat, frameCapacity: AVAudioFrameCount(f.length)) else {
            throw CocoaError(.fileReadCorruptFile)
        }
        try f.read(into: buf)
        let samples = Array(UnsafeBufferPointer(start: buf.floatChannelData![0], count: Int(buf.frameLength)))
        return (samples, f.processingFormat.sampleRate)
    }

    // MARK: engine

    private func configure(_ sampleRate: Double) {
        let fmt = AVAudioFormat(standardFormatWithSampleRate: sampleRate, channels: 1)!
        if format != fmt {
            engine.stop()
            engine.disconnectNodeOutput(node)
            engine.connect(node, to: engine.mainMixerNode, format: fmt)
            format = fmt
        }
    }

    private func resetNode() {
        generation += 1
        node.stop()
        pending = 0
        scheduledOut = 0
        segments = []
    }

    /// Keep about 1.5 s scheduled ahead: stretch the next input and schedule it; at the end of a part go on
    /// with the next one, or finish the part once everything has played.
    private func feed() {
        guard var s = source, let fmt = format else { return }
        if pending == 0 && scheduledOut > 0 { resetNode() }  // ran dry (live part behind): restart the clock
        let chunk = Int(s.sampleRate / 4)  // 0.25 s of input per buffer
        while pending < 6 {
            let avail = available(s)
            let inStart = stretcher.consumed
            var out: [Float]
            if cursor < avail {
                let n = min(chunk, avail - cursor)
                if n < chunk && !isComplete(s) && pending > 0 { break }  // wait for a full buffer while ahead
                let input: [Float] = s.live.map { Array($0.samples[cursor..<cursor + n]) }
                    ?? Array(s.samples[cursor..<cursor + n])
                cursor += n
                out = stretcher.process(input)
            } else if isComplete(s) && !flushed {
                out = stretcher.flush()
                flushed = true
            } else if isComplete(s) && advance() {
                s = source!
                continue
            } else {
                break
            }
            guard !out.isEmpty, let buf = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: AVAudioFrameCount(out.count)) else { continue }
            buf.frameLength = AVAudioFrameCount(out.count)
            out.withUnsafeBufferPointer { buf.floatChannelData![0].update(from: $0.baseAddress!, count: out.count) }
            segments.append(Segment(part: s.index, outStart: scheduledOut, outEnd: scheduledOut + out.count,
                                    inStart: inStart, inEnd: stretcher.consumed, sampleRate: s.sampleRate))
            scheduledOut += out.count
            pending += 1
            let gen = generation
            node.scheduleBuffer(buf, completionCallbackType: .dataPlayedBack) { [weak self] _ in
                Task { @MainActor in
                    guard let self, self.generation == gen else { return }
                    self.pending -= 1
                    self.feed()
                }
            }
        }
        if pending > 0 {
            if isPlaying {
                if !engine.isRunning { try? engine.start() }
                if !node.isPlaying { node.play() }
            }
            isWaiting = false
        } else if exhausted {
            partFinished(source?.index ?? currentPart)
        } else {
            isWaiting = isPlaying
        }
    }

    /// A part played to its end and the next one couldn't follow on directly: seek to it (that loads or streams
    /// it), wait for it, or stop at the end of the article.
    private func partFinished(_ index: Int) {
        guard let a = article, isPlaying else { return }
        let parts = a.parts ?? []
        let next = index + 1
        if next < parts.count || (a.status.isActive && parts.isEmpty) {
            if canPlay(part: next), let s = start(of: next) {
                seek(to: s, play: true)
            } else {
                isWaiting = true
            }
        } else {
            stopAll()
            isPlaying = false
            time = 0
            UserDefaults.standard.removeObject(forKey: "pos.\(a.id)")
            updateNowPlaying()
        }
    }

    private func liveEnded(_ status: LiveStream.End) {
        switch status {
        case .finished:
            feed()
        case .partReady, .failed:
            // finished meanwhile (play its file), or the stream broke: reload and play from here
            guard let a = article, let client = APIClient() else { return }
            updateClock()
            let t = time, play = isPlaying
            Task {
                try? await Task.sleep(for: .seconds(status == .failed ? 2 : 0.3))
                if let fresh = try? await client.article(a.id), self.article?.id == a.id {
                    self.article = fresh
                    self.source?.live?.failed = true
                    self.seek(to: t, play: play)
                }
            }
        }
    }

    // MARK: clock

    /// Article time from what the node has played: the segment playing now, interpolated in its input.
    private func updateClock() {
        guard source != nil, !segments.isEmpty, let last = node.lastRenderTime,
              let pt = node.playerTime(forNodeTime: last) else { return }
        let played = Int(pt.sampleTime)
        guard let k = segments.lastIndex(where: { $0.outStart <= played }) else { return }
        let g = segments[k]
        let f = min(1, Double(played - g.outStart) / Double(max(1, g.outEnd - g.outStart)))
        let input = Double(g.inStart) + f * Double(g.inEnd - g.inStart)
        if let start = start(of: g.part) {
            currentPart = g.part
            time = start + input / g.sampleRate
        }
        if k > 8 { segments.removeFirst(k - 8) }
    }

    private func tick() {
        guard let a = article else { return }
        if isPlaying && !isWaiting { updateClock() }
        if isPlaying, Int(time * 4) % 40 == 0 { savePosition(a.id) }
        if isPlaying { updateNowPlaying() }
    }

    private func savePosition(_ id: String) { UserDefaults.standard.set(time, forKey: "pos.\(id)") }

    // MARK: lock screen

    private func setUpRemoteCommands() {
        let c = MPRemoteCommandCenter.shared()
        c.playCommand.addTarget { [weak self] _ in MainActor.assumeIsolated { self?.play() }; return .success }
        c.pauseCommand.addTarget { [weak self] _ in MainActor.assumeIsolated { self?.pause() }; return .success }
        c.togglePlayPauseCommand.addTarget { [weak self] _ in MainActor.assumeIsolated { self?.toggle() }; return .success }
        c.skipForwardCommand.preferredIntervals = [15]
        c.skipBackwardCommand.preferredIntervals = [15]
        c.skipForwardCommand.addTarget { [weak self] _ in MainActor.assumeIsolated { self?.skip(15) }; return .success }
        c.skipBackwardCommand.addTarget { [weak self] _ in MainActor.assumeIsolated { self?.skip(-15) }; return .success }
        c.changePlaybackPositionCommand.addTarget { [weak self] e in
            guard let e = e as? MPChangePlaybackPositionCommandEvent else { return .commandFailed }
            MainActor.assumeIsolated { self?.seek(to: e.positionTime * Double(Self.baseSpeed), play: self?.isPlaying ?? false) }
            return .success
        }
    }

    /// The app icon on the lock screen and in Control Center (NowPlayingArtwork.png, rendered from AppIcon.icon).
    private static let artwork: MPMediaItemArtwork? = {
        guard let image = UIImage(named: "NowPlayingArtwork") else { return nil }
        return MPMediaItemArtwork(boundsSize: image.size) { _ in image }
    }()

        private func updateNowPlaying() {
        guard let a = article else { MPNowPlayingInfoCenter.default().nowPlayingInfo = nil; return }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = [
            MPMediaItemPropertyTitle: a.displayTitle,
            MPMediaItemPropertyArtist: a.byline?.isEmpty == false ? a.byline! : (a.site ?? ""),
            MPMediaItemPropertyArtwork: Self.artwork as Any,
            // in listening time (audio time / baseSpeed), like the app shows it
            MPMediaItemPropertyPlaybackDuration: max(duration, time) / Double(Self.baseSpeed),
            MPNowPlayingInfoPropertyElapsedPlaybackTime: time / Double(Self.baseSpeed),
            MPNowPlayingInfoPropertyPlaybackRate: isPlaying && !isWaiting ? Double(rate) : 0,
        ]
    }
}

/// One part's live PCM (GET /api/articles/<id>/parts/<n>/live: s16le mono), decoded to Float samples as it
/// arrives. All samples are kept (a 75 s part is ~13 MB), so skipping back within the part works.
@MainActor
final class LiveStream {
    enum End { case finished, partReady, failed }

    let articleID: String
    let index: Int
    private(set) var samples: [Float] = []
    private(set) var sampleRate: Double = 44100
    private(set) var finished = false
    var failed = false
    var onData: () -> Void = {}
    var onEnd: (End) -> Void = { _ in }
    private var task: Task<Void, Never>?

    init(articleID: String, index: Int) {
        self.articleID = articleID
        self.index = index
    }

    func start(_ client: APIClient) {
        var req = client.request("GET", "/api/articles/\(articleID)/parts/\(index)/live")
        req.timeoutInterval = 180  // the server waits for the part to start
        let session = client.session
        task = Task.detached { [weak self] in  // read off the main actor; hand over ~0.1 s at a time
            do {
                let (bytes, resp) = try await session.bytes(for: req)
                let http = resp as? HTTPURLResponse
                guard http?.statusCode == 200 else {
                    let how: End = http?.statusCode == 409 ? .partReady : .failed
                    await MainActor.run { self?.end(how) }
                    return
                }
                if let r = http?.value(forHTTPHeaderField: "X-Sample-Rate").flatMap(Double.init) {
                    await MainActor.run { self?.sampleRate = r }
                }
                var pending: [UInt8] = []
                pending.reserveCapacity(8820)
                for try await byte in bytes {
                    pending.append(byte)
                    if pending.count >= 8820 {
                        let chunk = pending
                        pending.removeAll(keepingCapacity: true)
                        await MainActor.run { self?.append(chunk) }
                    }
                }
                let rest = pending
                await MainActor.run {
                    self?.append(rest)
                    self?.end(.finished)
                }
            } catch {
                if !Task.isCancelled { await MainActor.run { self?.end(.failed) } }
            }
        }
    }

    func cancel() {
        task?.cancel()
        task = nil
    }

    private func append(_ bytes: [UInt8]) {
        let n = bytes.count / 2
        guard n > 0 else { return }
        samples.reserveCapacity(samples.count + n)
        bytes.withUnsafeBytes { raw in
            for i in 0..<n {
                let v = Int16(littleEndian: raw.loadUnaligned(fromByteOffset: i * 2, as: Int16.self))
                samples.append(Float(v) / 32768)
            }
        }
        onData()
    }

    private func end(_ how: End) {
        guard !finished else { return }
        if how == .finished { finished = true } else { failed = true }
        onEnd(how)
    }
}
