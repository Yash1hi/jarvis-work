// Mic in and speaker out through Apple's voice processing, so the mic doesn't hear
// what we play: the echo cancellation FaceTime and the ChatGPT app get for free.
// audio.py builds and runs this; the OS can only cancel audio played by the same
// engine that records, which is why playback lives here too.
//
// Framed pipes, each message [kind: 1 byte][length: UInt32 LE][payload]:
//   stdin   a  pcm16 24 kHz mono to play
//           c  cut: stop playing now and drop the queue
//   stdout  r  ready
//           m  mic audio, pcm16 24 kHz mono, echo removed
//           p  UInt64 frames actually played since the last cut (reply to c)
//           d  UInt64 buffers finished so far, played or dropped (idle when it equals buffers sent)
import AVFoundation
import Foundation

let rate = 24000.0
let outLock = NSLock()

func send(_ kind: Character, _ payload: Data = Data()) {
    var length = UInt32(payload.count).littleEndian
    var message = Data([kind.asciiValue!])
    message.append(Data(bytes: &length, count: 4))
    message.append(payload)
    outLock.lock()
    defer { outLock.unlock() }
    FileHandle.standardOutput.write(message)
}

func u64(_ value: UInt64) -> Data {
    var v = value.littleEndian
    return Data(bytes: &v, count: 8)
}

func readExact(_ n: Int) -> Data? {
    var buf = [UInt8](repeating: 0, count: n)
    var got = 0
    while got < n {
        let r = buf.withUnsafeMutableBytes { read(0, $0.baseAddress! + got, n - got) }
        if r <= 0 { return nil }
        got += r
    }
    return Data(buf)
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

let engine = AVAudioEngine()
// Create the output side first: if it's created after voice processing is switched on,
// it comes up without it and the engine fails to start (-10875).
_ = engine.outputNode
_ = engine.mainMixerNode
let input = engine.inputNode
do {
    try input.setVoiceProcessingEnabled(true)
    try engine.outputNode.setVoiceProcessingEnabled(true)
} catch {
    fail("voice processing unavailable: \(error)")
}
// By default voice processing turns other apps' audio way down while it runs.
input.voiceProcessingOtherAudioDuckingConfiguration = .init(enableAdvancedDucking: false, duckingLevel: .min)

let player = AVAudioPlayerNode()
engine.attach(player)
let playFormat = AVAudioFormat(standardFormatWithSampleRate: rate, channels: 1)!
engine.connect(player, to: engine.mainMixerNode, format: playFormat)

let inFormat = input.outputFormat(forBus: 0)
let monoIn = AVAudioFormat(standardFormatWithSampleRate: inFormat.sampleRate, channels: 1)!
let micFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: rate, channels: 1, interleaved: true)!
let converter = AVAudioConverter(from: monoIn, to: micFormat)!

input.installTap(onBus: 0, bufferSize: 1024, format: inFormat) { buf, _ in
    // The processed voice is channel 0; voice processing can report extra channels.
    guard let src = buf.floatChannelData,
          let mono = AVAudioPCMBuffer(pcmFormat: monoIn, frameCapacity: buf.frameLength) else { return }
    mono.frameLength = buf.frameLength
    mono.floatChannelData![0].update(from: src[0], count: Int(buf.frameLength))
    let capacity = AVAudioFrameCount(Double(buf.frameLength) * rate / inFormat.sampleRate) + 64
    guard let out = AVAudioPCMBuffer(pcmFormat: micFormat, frameCapacity: capacity) else { return }
    var fed = false
    var error: NSError?
    converter.convert(to: out, error: &error) { _, status in
        if fed {
            status.pointee = .noDataNow
            return nil
        }
        fed = true
        status.pointee = .haveData
        return mono
    }
    if out.frameLength > 0 {
        send("m", Data(bytes: out.int16ChannelData![0], count: Int(out.frameLength) * 2))
    }
}

// Playback bookkeeping. A cut bumps the generation so completions from dropped
// buffers don't count as played.
let state = NSLock()
var generation = 0
var scheduled: UInt64 = 0
var finished: UInt64 = 0
var playedFrames: UInt64 = 0

func play(_ data: Data) {
    let n = data.count / 2
    guard n > 0, let buf = AVAudioPCMBuffer(pcmFormat: playFormat, frameCapacity: AVAudioFrameCount(n)) else { return }
    buf.frameLength = AVAudioFrameCount(n)
    let dst = buf.floatChannelData![0]
    data.withUnsafeBytes { raw in
        let samples = raw.bindMemory(to: Int16.self)
        for i in 0..<n { dst[i] = Float(Int16(littleEndian: samples[i])) / 32768 }
    }
    state.lock()
    let gen = generation
    scheduled += 1
    state.unlock()
    player.scheduleBuffer(buf, completionCallbackType: .dataPlayedBack) { _ in
        state.lock()
        if gen == generation {
            finished += 1
            playedFrames += UInt64(n)
        }
        let done = finished
        state.unlock()
        send("d", u64(done))
    }
}

func cut() {
    state.lock()
    generation += 1
    state.unlock()
    player.stop()
    state.lock()
    let played = playedFrames
    playedFrames = 0
    finished = scheduled
    let done = finished
    state.unlock()
    player.play()
    send("p", u64(played))
    send("d", u64(done))
}

engine.prepare()
do {
    try engine.start()
} catch {
    fail("audio engine failed to start: \(error)")
}
player.play()
send("r")

while let header = readExact(5) {
    let length = Int(header[1]) | Int(header[2]) << 8 | Int(header[3]) << 16 | Int(header[4]) << 24
    let payload = length > 0 ? (readExact(length) ?? Data()) : Data()
    switch header[0] {
    case Character("a").asciiValue!: play(payload)
    case Character("c").asciiValue!: cut()
    default: break
    }
}
engine.stop()
