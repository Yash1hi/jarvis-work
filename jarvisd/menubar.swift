// Jarvis Controls: Jarvis's menu-bar item. Turn Jarvis on and off, and see what it has cost.
//
// The title is today's cost ("$0.42"), with a dot while Jarvis is in a conversation, or a slashed
// mic when Jarvis is off. Turning off stops the jarvisd login agent and keeps it off across restarts
// (launchctl disable); turning on undoes both. Costs come from the files jarvisd/meter.py keeps:
//   logs/sessions.jsonl  one line per finished conversation ({"at": ..., "cost": ...})
//   logs/live.json       the conversation in progress, if any
import AppKit

let info = Bundle.main.infoDictionary ?? [:]
let logs = URL(fileURLWithPath: info["JarvisLogs"] as? String ?? CommandLine.arguments.dropFirst().first ?? "logs")
let agent = "com.jarvis.jarvisd"
let domain = "gui/\(getuid())"
let agentPlist = NSString(string: "~/Library/LaunchAgents/\(agent).plist").expandingTildeInPath
let daemonLog = NSString(string: "~/Library/Logs/jarvisd.log").expandingTildeInPath
let controlsLog = NSString(string: "~/Library/Logs/jarvis-controls.log").expandingTildeInPath

@discardableResult
func launchctl(_ args: String...) -> (status: Int32, output: String) {
    let p = Process()
    let pipe = Pipe()
    p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
    p.arguments = args
    p.standardOutput = pipe
    p.standardError = pipe
    try? p.run()
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    return (p.terminationStatus, String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "")
}

func note(_ message: String) {
    let line = "\(Date()) \(message)\n"
    if let h = FileHandle(forWritingAtPath: controlsLog) {
        h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); h.closeFile()
    } else {
        FileManager.default.createFile(atPath: controlsLog, contents: line.data(using: .utf8))
    }
}

func jarvisIsOn() -> Bool { launchctl("print", "\(domain)/\(agent)").status == 0 }

/// Whether jarvisd says it's ready: it writes its pid to logs/ready once listening, and removes it on exit.
func jarvisIsReady() -> Bool {
    guard let text = try? String(contentsOf: logs.appendingPathComponent("ready"), encoding: .utf8),
          let pid = Int32(text.trimmingCharacters(in: .whitespacesAndNewlines)) else { return false }
    return kill(pid, 0) == 0
}

/// Turn Jarvis on or off and wait until macOS says it really is. Returns an error message, or nil.
/// launchctl answers before it has finished: right after bootout the job can still look loaded, and a
/// bootstrap while that teardown finishes fails ("5: Input/output error"), so both sides poll and retry.
func setJarvis(on: Bool) -> String? {
    var last = ""
    if on {
        launchctl("enable", "\(domain)/\(agent)")
        for _ in 0..<20 {
            let r = launchctl("bootstrap", domain, agentPlist)
            if r.status == 0 || jarvisIsOn() { return nil }
            last = r.output
            usleep(500_000)
        }
        return "Couldn't start Jarvis: \(last)"
    }
    launchctl("disable", "\(domain)/\(agent)")  // stays off across restarts and logins
    last = launchctl("bootout", "\(domain)/\(agent)").output
    for _ in 0..<40 {
        if !jarvisIsOn() { return nil }
        usleep(250_000)
    }
    return "Jarvis didn't stop: \(last)"
}

struct Totals {
    var today = 0.0, week = 0.0, month = 0.0, all = 0.0, conversationsToday = 0
    var live: Double? = nil
}

func readTotals() -> Totals {
    var t = Totals()
    let now = Date()
    let day = DateFormatter()
    day.dateFormat = "yyyy-MM-dd"
    let today = day.string(from: now)
    let month = String(today.prefix(7))
    var cal = Calendar(identifier: .iso8601)
    cal.timeZone = .current
    let weekStart = day.string(from: cal.dateInterval(of: .weekOfYear, for: now)?.start ?? now)

    if let text = try? String(contentsOf: logs.appendingPathComponent("sessions.jsonl"), encoding: .utf8) {
        for line in text.split(separator: "\n") {
            guard let data = line.data(using: .utf8),
                  let entry = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let at = entry["at"] as? String, let cost = entry["cost"] as? Double else { continue }
            let date = String(at.prefix(10))
            t.all += cost
            if date.hasPrefix(month) { t.month += cost }
            if date >= weekStart { t.week += cost }
            if date == today {
                t.today += cost
                if (entry["kind"] as? String ?? "conversation") == "conversation" { t.conversationsToday += 1 }
            }
        }
    }
    if let data = try? Data(contentsOf: logs.appendingPathComponent("live.json")),
       let live = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
       let cost = live["cost"] as? Double {
        t.live = cost
        t.today += cost; t.week += cost; t.month += cost; t.all += cost
    }
    return t
}

func money(_ v: Double) -> String { String(format: v < 10 ? "$%.2f" : "$%.0f", v) }

final class Controls: NSObject, NSApplicationDelegate {
    var item: NSStatusItem!
    var on = jarvisIsOn()
    var switching: Bool? = nil  // the state being switched to, while it happens
    var problem: String? = nil

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Opened again (e.g. from Spotlight) while already in the menu bar: the running one is enough.
        // But a copy that's just quitting (a reinstall) doesn't count, so give it a few seconds to go.
        let mine = Bundle.main.bundleIdentifier ?? ""
        for attempt in 0..<12 {
            if NSRunningApplication.runningApplications(withBundleIdentifier: mine).count <= 1 { break }
            if attempt == 11 { exit(0) }
            Thread.sleep(forTimeInterval: 0.5)
        }

        // New items land at the far left of the status area, which notched Macs hide first when the
        // front app has a long menu. Ask for a starting spot near the clock (points from the right);
        // Cmd-drag moves it after that, and macOS remembers it under the autosave name.
        let positionKey = "NSStatusItem Preferred Position jarvis-meter"
        if UserDefaults.standard.object(forKey: positionKey) == nil {
            UserDefaults.standard.set(260.0, forKey: positionKey)
        }
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.autosaveName = "jarvis-meter"
        item.button?.font = NSFont.monospacedDigitSystemFont(ofSize: NSFont.systemFontSize, weight: .regular)
        refresh()
        Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in self.refresh() }
        Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { _ in self.pollAgent() }
    }

    func pollAgent() {
        guard switching == nil else { return }
        DispatchQueue.global().async {
            let on = jarvisIsOn()
            DispatchQueue.main.async { self.on = on; self.refresh() }
        }
    }

    func refresh() {
        let t = readTotals()
        if switching != nil {
            item.button?.image = nil
            item.button?.title = "…"
        } else if on {
            item.button?.image = nil
            item.button?.title = (t.live != nil ? "●" : "") + money(t.today)  // short: notched Macs hide wide items
        } else {
            item.button?.title = ""
            item.button?.image = NSImage(systemSymbolName: "mic.slash", accessibilityDescription: "Jarvis is off")
        }

        let menu = NSMenu()
        menu.autoenablesItems = false  // so the switch can be greyed out while it works
        let status: String
        if let target = switching {
            status = target ? "Turning Jarvis on…" : "Turning Jarvis off…"
        } else if !on {
            status = "Jarvis is off"
        } else if t.live != nil {
            status = "Jarvis is in a conversation"
        } else {
            status = jarvisIsReady() ? "Jarvis is listening" : "Jarvis is starting…"
        }
        menu.addItem(withTitle: status, action: nil, keyEquivalent: "").isEnabled = false
        if let problem = problem {
            menu.addItem(withTitle: "⚠︎ \(problem)", action: nil, keyEquivalent: "").isEnabled = false
        }
        let toggle = menu.addItem(withTitle: on ? "Turn Jarvis Off" : "Turn Jarvis On", action: #selector(toggle), keyEquivalent: "")
        toggle.target = self
        toggle.isEnabled = switching == nil
        menu.addItem(.separator())
        if let live = t.live { menu.addItem(withTitle: "This conversation: \(String(format: "$%.3f", live))", action: nil, keyEquivalent: "") }
        menu.addItem(withTitle: "Today: \(money(t.today)) · \(t.conversationsToday) conversation\(t.conversationsToday == 1 ? "" : "s")",
                     action: nil, keyEquivalent: "")
        menu.addItem(withTitle: "This week: \(money(t.week))", action: nil, keyEquivalent: "")
        menu.addItem(withTitle: "This month: \(money(t.month))", action: nil, keyEquivalent: "")
        menu.addItem(withTitle: "All time: \(money(t.all))", action: nil, keyEquivalent: "")
        menu.addItem(withTitle: "Estimated from OpenAI token counts", action: nil, keyEquivalent: "")
        menu.addItem(.separator())
        for (title, selector) in [("Open Jarvis Log", #selector(openDaemonLog)), ("Open Cost Log", #selector(openCostLog)),
                                  ("Quit Jarvis Controls", #selector(quit))] {
            menu.addItem(withTitle: title, action: selector, keyEquivalent: "").target = self
        }
        for item in menu.items where item.action == nil && !item.isSeparatorItem { item.isEnabled = false }
        item.menu = menu
    }

    @objc func toggle() {
        guard switching == nil else { return }
        let turnOn = !on
        switching = turnOn
        problem = nil
        refresh()
        DispatchQueue.global().async {
            let error = setJarvis(on: turnOn)
            note("turn \(turnOn ? "on" : "off"): \(error ?? "ok")")
            let on = jarvisIsOn()
            DispatchQueue.main.async {
                self.switching = nil
                self.problem = error
                self.on = on
                self.refresh()
            }
        }
    }

    @objc func openDaemonLog() { NSWorkspace.shared.open(URL(fileURLWithPath: daemonLog)) }
    @objc func openCostLog() { NSWorkspace.shared.open(logs.appendingPathComponent("sessions.jsonl")) }
    @objc func quit() { NSApp.terminate(nil) }
}

let app = NSApplication.shared
let controls = Controls()
app.delegate = controls
app.setActivationPolicy(.accessory)
app.run()
