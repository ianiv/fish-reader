import AVFoundation
import MediaPlayer
import Observation

/// Plays an article as one timeline of parts (article time = sum of the earlier parts' durations + time in the
/// part). Finished parts play from their downloaded m4a files with an AVQueuePlayer ("file" mode). The first part
/// that isn't finished yet plays live while Drama 3 renders it ("live" mode): its PCM streams from
/// /parts/<n>/live into an AVAudioEngine, so sound starts about a second after rendering does. When a live part
/// ends, playback goes on with the next part, from its file if it's ready, else live again.
/// Position is remembered per article.
@MainActor @Observable
final class PlayerController {
    static let shared = PlayerController()

    private(set) var article: Article?
    private(set) var currentPart = 0
    private(set) var time: Double = 0
    private(set) var isPlaying = false
    private(set) var isWaiting = false  // playing, but the audio isn't there yet (part starting / buffering)
    private(set) var isLive = false
    var rate: Float = 1.0 {
        didSet {
            player.defaultRate = rate
            if isPlaying && !isLive { player.rate = rate }
            pitch.rate = rate
            updateNowPlaying()
        }
    }

    // file mode
    private let player = AVQueuePlayer()
    private var queued: [Int] = []  // part index of each AVPlayerItem in the queue, in order
    private var downloading: Set<Int> = []

    // live mode
    private let engine = AVAudioEngine()
    private let node = AVAudioPlayerNode()
    private let pitch = AVAudioUnitTimePitch()
    private var live: LiveStream?
    private var liveCursor = 0      // next sample to schedule
    private var liveBase = 0        // sample the node started playing from (after the last stop)
    private var livePending = 0     // scheduled buffers not yet played
    private var liveGeneration = 0  // bumps on every stop, so stale buffer completions are ignored
    private var liveFormat: AVAudioFormat?
    private var streamed: [Int: Double] = [:]  // durations of parts heard live, until the server reports them

    private var timer: Timer?
    private var endObserver: NSObjectProtocol?

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
        player.actionAtItemEnd = .advance
        engine.attach(node)
        engine.attach(pitch)
        endObserver = NotificationCenter.default.addObserver(forName: AVPlayerItem.didPlayToEndTimeNotification, object: nil, queue: .main) { [weak self] n in
            MainActor.assumeIsolated {
                guard let self, let item = n.object as? AVPlayerItem, self.player.items().first === item || self.player.currentItem === item else { return }
                self.fileItemEnded()
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
            article = a
            time = UserDefaults.standard.double(forKey: "pos.\(a.id)")
            currentPart = part(at: time)
        } else {
            article = a
            if !isLive { Task { await fillQueue() } }
            if isWaiting && isPlaying && !isLive { seek(to: time, play: true) }  // e.g. the next part just got ready
        }
        updateNowPlaying()
    }

    func toggle() { isPlaying ? pause() : play() }

    func play() {
        guard article != nil else { return }
        try? AVAudioSession.sharedInstance().setActive(true)
        if isLive {
            isPlaying = true
            startEngine()
            node.play()
        } else if player.items().isEmpty {
            seek(to: time, play: true)
            return
        } else {
            isPlaying = true
            player.playImmediately(atRate: rate)
        }
        updateNowPlaying()
    }

    func pause() {
        isPlaying = false
        if isLive { node.pause() } else { player.pause() }
        if let id = article?.id { savePosition(id) }
        updateNowPlaying()
    }

    func skip(_ seconds: Double) {
        let limit = livePartIndex.flatMap { i in start(of: i).map { $0 + liveAvailable(i) } } ?? readyDuration
        seek(to: max(0, min(time + seconds, max(0, limit - 0.5))), play: isPlaying)
    }

    func seek(toPart index: Int) {
        guard canPlay(part: index), let s = start(of: index) else { return }
        seek(to: s, play: true)
    }

    /// Go to article time `t`: a finished part plays from its file, the live part from the stream.
    func seek(to t: Double, play: Bool) {
        guard let a = article else { return }
        let index = part(at: t)
        currentPart = index
        time = t
        isPlaying = play
        let offset = max(0, t - (start(of: index) ?? t))
        if index < a.readyParts.count {
            stopLive()
            player.pause()
            player.removeAllItems()
            queued = []
            isWaiting = play
            Task {
                await fillQueue()
                if let first = player.items().first, queued.first == index {
                    await first.seek(to: CMTime(seconds: offset, preferredTimescale: 600))
                }
                isWaiting = false
                if self.isPlaying { player.playImmediately(atRate: rate) }
                updateNowPlaying()
            }
        } else if index == livePartIndex {
            player.pause()
            player.removeAllItems()
            queued = []
            playLive(index, from: offset)
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
        stopLive()
        player.pause()
        player.removeAllItems()
        queued = []
        isWaiting = false
    }

    // MARK: file mode

    /// Append every ready part after the last queued one (downloading as needed).
    private func fillQueue() async {
        guard let a = article, let client = APIClient(), !isLive else { return }
        for p in a.readyParts where p.index >= (queued.last.map { $0 + 1 } ?? currentPart) {
            let file = AudioCache.file(a.id, part: p.index)
            if !FileManager.default.fileExists(atPath: file.path) {
                guard !downloading.contains(p.index), let url = p.audioUrl else { return }
                downloading.insert(p.index)
                defer { downloading.remove(p.index) }
                do { try await client.download(url, to: file) } catch { return }
            }
            guard article?.id == a.id, !isLive, !queued.contains(p.index),
                  (queued.last.map { $0 + 1 } ?? currentPart) == p.index else { return }
            player.insert(AVPlayerItem(url: file), after: nil)
            queued.append(p.index)
        }
    }

    private func fileItemEnded() {
        guard !queued.isEmpty, !isLive else { return }
        queued.removeFirst()
        if let n = queued.first {
            currentPart = n
        } else {
            partFinished(currentPart)
        }
        updateNowPlaying()
    }

    /// A part played to its end: go on with the next one (file or live), or stop at the end of the article.
    private func partFinished(_ index: Int) {
        guard let a = article, let parts = a.parts else { return }
        let next = index + 1
        if next < parts.count || (a.status.isActive && parts.isEmpty) {
            if let s = start(of: next) {
                seek(to: s, play: isPlaying)
            } else {
                isWaiting = true
            }
        } else {
            stopAll()
            isPlaying = false
            time = 0
            UserDefaults.standard.removeObject(forKey: "pos.\(a.id)")
        }
    }

    // MARK: live mode

    private func liveAvailable(_ index: Int) -> Double {
        guard let live, live.index == index else { return 0 }
        return Double(live.samples.count) / live.sampleRate
    }

    private func playLive(_ index: Int, from offset: Double) {
        guard let a = article, let client = APIClient() else { return }
        if live?.index != index || live?.articleID != a.id || live?.failed == true {
            stopLive()
            let stream = LiveStream(articleID: a.id, index: index)
            stream.onData = { [weak self] in self?.liveData() }
            stream.onEnd = { [weak self] status in self?.liveEnded(status) }
            live = stream
            stream.start(client)
        } else {
            resetNode()
        }
        isLive = true
        liveCursor = Int(offset * (live?.sampleRate ?? 44100))
        liveBase = liveCursor
        isWaiting = true
        liveData()
    }

    private func stopLive() {
        live?.cancel()
        live = nil
        resetNode()
        isLive = false
    }

    private func resetNode() {
        liveGeneration += 1
        node.stop()
        livePending = 0
    }

    private func startEngine() {
        guard let live else { return }
        let fmt = AVAudioFormat(standardFormatWithSampleRate: live.sampleRate, channels: 1)!
        if liveFormat != fmt {
            engine.stop()
            engine.disconnectNodeOutput(node)
            engine.disconnectNodeOutput(pitch)
            engine.connect(node, to: pitch, format: fmt)
            engine.connect(pitch, to: engine.mainMixerNode, format: fmt)
            liveFormat = fmt
        }
        pitch.rate = rate
        if !engine.isRunning { try? engine.start() }
    }

    /// New samples arrived (or playback moved): keep about 2 s scheduled ahead.
    private func liveData() {
        guard isLive, let live else { return }
        startEngine()
        guard let fmt = liveFormat else { return }
        let chunk = Int(live.sampleRate / 4)  // 0.25 s buffers
        let generation = liveGeneration
        while livePending < 8, liveCursor < live.samples.count {
            let n = min(chunk, live.samples.count - liveCursor)
            if n < chunk && !live.finished && livePending > 0 { break }  // wait for a full buffer while ahead
            guard let buf = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: AVAudioFrameCount(n)) else { break }
            buf.frameLength = AVAudioFrameCount(n)
            live.samples.withUnsafeBufferPointer { src in
                buf.floatChannelData![0].update(from: src.baseAddress! + liveCursor, count: n)
            }
            liveCursor += n
            livePending += 1
            node.scheduleBuffer(buf, completionCallbackType: .dataPlayedBack) { [weak self] _ in
                Task { @MainActor in
                    guard let self, self.liveGeneration == generation else { return }
                    self.livePending -= 1
                    self.liveData()
                    self.checkLiveEnd()
                }
            }
        }
        if livePending > 0 && isWaiting {
            isWaiting = false
        }
        if isPlaying && livePending > 0 && !node.isPlaying { node.play() }
        if livePending == 0 && isPlaying { isWaiting = true }
    }

    private func liveEnded(_ status: LiveStream.End) {
        switch status {
        case .finished:
            liveData()
            checkLiveEnd()
        case .partReady, .failed:
            // finished meanwhile (play its file), or the stream broke: reload and play from here
            guard let a = article, let client = APIClient() else { return }
            let t = time, play = isPlaying
            Task {
                try? await Task.sleep(for: .seconds(status == .failed ? 2 : 0.3))
                if let fresh = try? await client.article(a.id), self.article?.id == a.id {
                    self.article = fresh
                    self.live?.failed = true
                    self.seek(to: t, play: play)
                }
            }
        }
    }

    private func checkLiveEnd() {
        guard isLive, let live, live.finished, liveCursor >= live.samples.count, livePending == 0 else { return }
        let index = live.index
        streamed[index] = Double(live.samples.count) / live.sampleRate  // until the server's number arrives
        stopLive()
        partFinished(index)
    }

    // MARK: clock

    private func tick() {
        guard let a = article else { return }
        if isLive, let live, let start = start(of: live.index) {
            var played = liveBase
            if let last = node.lastRenderTime, let pt = node.playerTime(forNodeTime: last) {
                played += Int(pt.sampleTime)
            }
            time = start + Double(min(played, live.samples.count)) / live.sampleRate
        } else if !isLive, let first = queued.first, player.currentItem != nil, let start = start(of: first) {
            let s = player.currentTime().seconds
            if s.isFinite {
                currentPart = first
                time = start + s
            }
        }
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
            MainActor.assumeIsolated { self?.seek(to: e.positionTime, play: self?.isPlaying ?? false) }
            return .success
        }
    }

    private func updateNowPlaying() {
        guard let a = article else { MPNowPlayingInfoCenter.default().nowPlayingInfo = nil; return }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = [
            MPMediaItemPropertyTitle: a.displayTitle,
            MPMediaItemPropertyArtist: a.byline?.isEmpty == false ? a.byline! : (a.site ?? ""),
            MPMediaItemPropertyPlaybackDuration: max(duration, time),
            MPNowPlayingInfoPropertyElapsedPlaybackTime: time,
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
