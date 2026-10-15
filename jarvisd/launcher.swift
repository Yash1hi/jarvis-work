// Jarvis.app's executable. It runs jarvisd (`uv run python -m jarvisd`) as a child and stays its
// parent, so macOS attributes the microphone to "Jarvis", one clear entry in
// Privacy & Security, rather than to uv or python. launchd starts it (launchd/com.jarvis.jarvisd.plist);
// it passes stop signals on and exits when jarvisd does.
import Foundation

let root = Bundle.main.object(forInfoDictionaryKey: "JarvisRoot") as? String ?? FileManager.default.currentDirectoryPath
let uv = Bundle.main.object(forInfoDictionaryKey: "JarvisUV") as? String ?? "/usr/local/bin/uv"

let jarvisd = Process()
jarvisd.executableURL = URL(fileURLWithPath: uv)
jarvisd.arguments = ["run", "--quiet", "python", "-m", "jarvisd"]
jarvisd.currentDirectoryURL = URL(fileURLWithPath: root)
jarvisd.terminationHandler = { exit($0.terminationStatus) }

var handlers: [DispatchSourceSignal] = []
for sig in [SIGTERM, SIGINT, SIGHUP] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler { kill(jarvisd.processIdentifier, sig) }
    source.resume()
    handlers.append(source)
}

do {
    try jarvisd.run()
} catch {
    FileHandle.standardError.write("couldn't start jarvisd: \(error)\n".data(using: .utf8)!)
    exit(1)
}
dispatchMain()
