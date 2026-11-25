import XCTest
@testable import ArticleReader

final class SpeechStretcherTests: XCTestCase {
    let sr = 44100.0

    /// A 150 Hz "voice" (fundamental + harmonics), `seconds` long.
    func voice(_ seconds: Double) -> [Float] {
        (0..<Int(seconds * sr)).map { i in
            let t = Double(i) / sr
            return Float(0.5 * sin(2 * .pi * 150 * t) + 0.25 * sin(2 * .pi * 300 * t) + 0.1 * sin(2 * .pi * 450 * t))
        }
    }

    /// Fundamental estimated from upward zero crossings.
    func pitch(_ x: [Float]) -> Double {
        var n = 0
        for i in 1..<x.count where x[i - 1] < 0 && x[i] >= 0 { n += 1 }
        return Double(n) / (Double(x.count) / sr)
    }

    func stretch(_ x: [Float], _ speed: Float) -> (out: [Float], consumed: Int) {
        let s = SpeechStretcher(sampleRate: sr, speed: speed)
        var out: [Float] = []
        for start in stride(from: 0, to: x.count, by: 11025) {  // 0.25 s chunks, as the player feeds it
            out += s.process(Array(x[start..<min(start + 11025, x.count)]))
        }
        out += s.flush()
        return (out, s.consumed)
    }

    func testSpeedsChangeLengthNotPitch() {
        let x = voice(4)
        for speed: Float in [0.8, 1.2, 1.5, 2.0] {
            let (out, consumed) = stretch(x, speed)
            XCTAssertEqual(consumed, x.count)
            let expected = Double(x.count) / Double(speed)
            XCTAssertEqual(Double(out.count), expected, accuracy: expected * 0.03, "length at \(speed)×")
            XCTAssertEqual(pitch(out), 150, accuracy: 5, "pitch at \(speed)×")
            XCTAssertLessThanOrEqual(out.map(abs).max() ?? 0, 0.86, "no clipping at \(speed)×")
        }
    }

    func testOneIsUntouched() {
        let x = voice(1)
        XCTAssertEqual(stretch(x, 1).out, x)
    }
}
