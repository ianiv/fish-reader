import Foundation

/// Changes the speed of speech without changing its pitch, the way Sonic does (github.com/waywardgeek/sonic,
/// Bill Cox, Apache 2.0; this is a Swift rewrite of its method, not its code): find the voice's pitch period,
/// then drop (faster) or repeat (slower) whole periods, cross-fading over one period at each cut. Cutting at
/// pitch periods keeps the voice's timbre, where a phase vocoder (AVAudioUnitTimePitch) smears it.
///
/// Streaming: feed input with process(), get the output produced so far; flush() at the end of the input.
/// `consumed` is the absolute input sample the output so far corresponds to (± one pitch period).
final class SpeechStretcher {
    let speed: Float
    private let minPeriod: Int
    private let maxPeriod: Int
    private let skip: Int  // downsampling for the coarse pitch search
    private var input: [Float] = []
    private var pos = 0     // next unprocessed sample in `input`
    private var base: Int   // absolute input index of input[0]
    private var toCopy = 0  // samples to copy unchanged before the next cut
    private var down: [Float]

    var consumed: Int { base + pos }

    init(sampleRate: Double, speed: Float, start: Int = 0) {
        self.speed = min(max(speed, 0.5), 4)
        minPeriod = max(2, Int(sampleRate / 400))  // 400 Hz voice
        maxPeriod = Int(sampleRate / 65)           // 65 Hz voice
        skip = max(1, Int(sampleRate / 4000))
        base = start
        down = [Float](repeating: 0, count: 2 * maxPeriod / skip + 1)
    }

    var isIdentity: Bool { abs(speed - 1) < 0.001 }

    func process(_ samples: [Float]) -> [Float] {
        if isIdentity {
            base += samples.count
            return samples
        }
        input.append(contentsOf: samples)
        var out: [Float] = []
        out.reserveCapacity(Int(Float(samples.count) / speed) + 2 * maxPeriod)
        while true {
            if toCopy > 0 {
                let n = min(toCopy, input.count - pos)
                if n == 0 { break }
                out.append(contentsOf: input[pos..<pos + n])
                pos += n
                toCopy -= n
                continue
            }
            guard input.count - pos >= 2 * maxPeriod else { break }
            let p = period(at: pos)
            if speed > 1 {  // drop one period: cross-fade it into the next
                let n: Int
                if speed >= 2 {
                    n = Int(Float(p) / (speed - 1))
                } else {
                    n = p
                    toCopy = Int(Float(p) * (2 - speed) / (speed - 1))
                }
                overlap(&out, n: n, down: pos, up: pos + p)
                pos += p + n
            } else {  // repeat one period: play it, then cross-fade back into it
                let n: Int
                if speed <= 0.5 {
                    n = Int(Float(p) * speed / (1 - speed))
                } else {
                    n = p
                    toCopy = Int(Float(p) * (2 * speed - 1) / (1 - speed))
                }
                out.append(contentsOf: input[pos..<pos + p])
                overlap(&out, n: n, down: pos + p, up: pos)
                pos += n
            }
        }
        if pos > 1 << 16 {
            input.removeFirst(pos)
            base += pos
            pos = 0
        }
        return out
    }

    /// The rest of the input (under two pitch periods, ~30 ms), unchanged.
    func flush() -> [Float] {
        let out = Array(input[pos...])
        base += input.count
        input = []
        pos = 0
        toCopy = 0
        return out
    }

    private func overlap(_ out: inout [Float], n: Int, down d: Int, up u: Int) {
        guard n > 0 else { return }
        let inv = 1 / Float(n)
        for i in 0..<n {
            let t = Float(i) * inv
            out.append(input[d + i] * (1 - t) + input[u + i] * t)
        }
    }

    /// The pitch period at `s` (needs 2 * maxPeriod samples): average magnitude difference function, coarse on
    /// a downsampled copy, then refined at full rate.
    private func period(at s: Int) -> Int {
        let m = 2 * maxPeriod / skip
        let scale = 1 / Float(skip)
        for i in 0..<m {
            var sum: Float = 0
            let j = s + i * skip
            for k in 0..<skip { sum += input[j + k] }
            down[i] = sum * scale
        }
        let coarse = bestPeriod(minPeriod / skip, maxPeriod / skip, limit: m) { down[$0] }
        guard skip > 1 else { return coarse }
        let lo = max(minPeriod, (coarse - 1) * skip), hi = min(maxPeriod, (coarse + 1) * skip)
        return bestPeriod(lo, hi, limit: 2 * maxPeriod) { input[s + $0] }
    }

    private func bestPeriod(_ lo: Int, _ hi: Int, limit: Int, _ x: (Int) -> Float) -> Int {
        var best = max(lo, 1), bestDiff = Float.greatestFiniteMagnitude
        guard lo <= hi else { return best }
        for p in max(lo, 1)...hi where 2 * p <= limit {
            var d: Float = 0
            for i in 0..<p { d += abs(x(i) - x(i + p)) }
            d /= Float(p)
            if d < bestDiff {
                bestDiff = d
                best = p
            }
        }
        return best
    }
}
