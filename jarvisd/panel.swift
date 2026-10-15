// Jarvis's workspace: a translucent panel docked in the top-right corner of the main screen, showing
// jarvisd's workspace page (what's being said, the agents at work, whatever Jarvis puts up). It floats
// over other apps, on every Space and over full-screen apps, and clicking it doesn't take focus from
// the app you're in.
//
// jarvisd (workspace.py) starts it with the page URL and steers it over stdin, one command per line:
//   show | hide | toggle | opacity <0.15–1, or ±step> | clickthrough on|off
//   size small|medium|large|wide|tall|reset, or <width> <height> in points
//   web <url> | web close          a web page in a browser view filling the panel below the page's web bar
// After every change it prints its state as one line of JSON on stdout, so Jarvis knows what it did.
// It exits when stdin closes, i.e. when jarvisd does.
//
// Hotkeys, from anywhere:  ⌃⌥J show/hide · ⌃⌥↑ / ⌃⌥↓ opacity · ⌃⌥K click-through (clicks pass to
// whatever is underneath, for when it's covering something you need)
//
// Opacity fades the glass, not the whole window: macOS stops blurring behind a window that is itself
// translucent, which leaves text over a busy screen hard to read. So the window stays opaque, the glass
// layer under the page fades, and the page dims its tint and (only near the bottom) its text to match.
// A web page fades with it, down to 30%.
//
// Sizes Jarvis picks are for the moment; only a size Yash drags to is remembered ("size reset" returns to it).
import Carbon.HIToolbox
import Cocoa
import WebKit

let pageURL = URL(string: CommandLine.arguments.dropFirst().first ?? "http://127.0.0.1:8792/")!
let prefs = UserDefaults(suiteName: "com.jarvis.workspace")!
let margin: CGFloat = 12
let minOpacity: CGFloat = 0.15  // never invisible: you'd lose it

func mainScreen() -> NSRect { (NSScreen.screens.first ?? NSScreen.main!).visibleFrame }

final class Panel: NSPanel {
    override var canBecomeKey: Bool { true }  // so the page scrolls and takes clicks and typing
    override var canBecomeMain: Bool { false }
}

final class Workspace: NSObject, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    let panel: Panel
    let web: WKWebView              // jarvisd's page
    var browser: WKWebView?         // a web page Jarvis opened, over the lower part of the panel
    var browserTop: CGFloat = 80    // where the page's web bar ends; the page reports it
    let glass = NSVisualEffectView()
    var opacity = CGFloat(prefs.object(forKey: "opacity") as? Double ?? 0.88)
    var clickThrough = false
    var resizingForJarvis = false   // animated resizes look like drags to macOS; those aren't Yash's size
    var liveResizeIsJarvis = false

    override init() {
        panel = Panel(contentRect: NSRect(x: 0, y: 0, width: 420, height: 560),
                      styleMask: [.titled, .resizable, .fullSizeContentView, .nonactivatingPanel],
                      backing: .buffered, defer: false)
        let config = WKWebViewConfiguration()
        web = WKWebView(frame: .zero, configuration: config)
        super.init()
        config.userContentController.add(self, name: "panel")

        panel.titlebarAppearsTransparent = true
        panel.titleVisibility = .hidden
        for button in [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton] {
            panel.standardWindowButton(button)?.isHidden = true
        }
        panel.isMovable = false  // docked: it stays in the corner, and resizing keeps it there
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        panel.hidesOnDeactivate = false
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.minSize = NSSize(width: 320, height: 260)
        panel.delegate = self

        glass.material = .hudWindow
        glass.blendingMode = .behindWindow
        glass.state = .active
        web.setValue(false, forKey: "drawsBackground")  // the page is see-through; the glass shows behind it
        web.navigationDelegate = self
        web.uiDelegate = self
        let content = NSView()
        for view in [glass, web] as [NSView] {  // siblings, so fading the glass leaves the page alone
            view.autoresizingMask = [.width, .height]
            content.addSubview(view)
        }
        panel.contentView = content
        glass.frame = content.bounds
        web.frame = content.bounds

        dock(size: savedSize())
        glass.alphaValue = opacity
        web.load(URLRequest(url: pageURL))
        if !prefs.bool(forKey: "hidden") { panel.orderFrontRegardless() }
        report()

        NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification,
                                               object: nil, queue: .main) { [weak self] _ in
            guard let self else { return }
            self.dock(size: self.panel.frame.size)
        }
    }

    // --- placement ---------------------------------------------------------------------------

    func savedSize() -> NSSize {
        let screen = mainScreen()
        let w = prefs.double(forKey: "width"), h = prefs.double(forKey: "height")
        if w > 0 && h > 0 { return NSSize(width: w, height: h) }
        return NSSize(width: min(max(screen.width * 0.28, 360), 540), height: screen.height * 0.55)
    }

    /// Pin the top-right corner to the top-right of the main screen (below the menu bar).
    func dock(size: NSSize, animate: Bool = false) {
        let screen = mainScreen()
        let w = min(max(size.width, panel.minSize.width), screen.width - 2 * margin)
        let h = min(max(size.height, panel.minSize.height), screen.height - 2 * margin)
        panel.setFrame(NSRect(x: screen.maxX - w - margin, y: screen.maxY - h - margin, width: w, height: h),
                       display: true, animate: animate && panel.isVisible)
    }

    func resize(_ args: [String]) {
        let screen = mainScreen(), current = panel.frame.size
        let size: NSSize
        switch args.first {
        case "small": size = NSSize(width: 340, height: 300)
        case "large": size = NSSize(width: min(screen.width * 0.45, 820), height: screen.height * 0.85)
        case "wide": size = NSSize(width: min(screen.width * 0.62, 1100), height: current.height)
        case "tall": size = NSSize(width: current.width, height: screen.height)
        case "medium", "reset", "default": size = savedSize()
        default:
            guard args.count == 2, let w = Double(args[0]), let h = Double(args[1]) else { return }
            size = NSSize(width: w, height: h)
        }
        resizingForJarvis = true
        dock(size: size, animate: true)
        resizingForJarvis = false
    }

    func windowWillStartLiveResize(_ notification: Notification) { liveResizeIsJarvis = resizingForJarvis }

    func windowDidEndLiveResize(_ notification: Notification) {
        if liveResizeIsJarvis || resizingForJarvis {
            liveResizeIsJarvis = false
            return
        }
        prefs.set(Double(panel.frame.width), forKey: "width")
        prefs.set(Double(panel.frame.height), forKey: "height")
        dock(size: panel.frame.size)  // resizing from the left or bottom edge mustn't pull it out of the corner
        report()
    }

    // --- controls ----------------------------------------------------------------------------

    func setOpacity(_ value: CGFloat) {
        opacity = min(1, max(minOpacity, value))
        glass.alphaValue = opacity
        browser?.alphaValue = max(opacity, 0.3)
        prefs.set(Double(opacity), forKey: "opacity")
        tellPage()
    }

    func setVisible(_ visible: Bool) {
        if visible { panel.orderFrontRegardless() } else { panel.orderOut(nil) }
        prefs.set(!visible, forKey: "hidden")
    }

    func setClickThrough(_ on: Bool) {
        clickThrough = on
        panel.ignoresMouseEvents = on
        tellPage()
    }

    /// The page shows the opacity briefly when it changes, and a marker while clicks pass through.
    func tellPage() {
        web.evaluateJavaScript("window.panelState && window.panelState({opacity: \(opacity), clickThrough: \(clickThrough)})")
    }

    /// One line of JSON on stdout: jarvisd keeps the latest, and Jarvis's workspace tool returns it.
    func report() {
        let state: [String: Any] = [
            "visible": panel.isVisible, "opacity": (Double(opacity) * 100).rounded() / 100,
            "width": Double(panel.frame.width).rounded(), "height": Double(panel.frame.height).rounded(),
            "click_through": clickThrough, "web_page": browser?.url?.absoluteString ?? NSNull(),
        ]
        if let data = try? JSONSerialization.data(withJSONObject: state), let line = String(data: data, encoding: .utf8) {
            print(line)
            fflush(stdout)
        }
    }

    func command(_ line: String) {
        let parts = line.split(separator: " ").map(String.init)
        let args = Array(parts.dropFirst())
        switch parts.first {
        case "show": setVisible(true)
        case "hide": setVisible(false)
        case "toggle": setVisible(!panel.isVisible)
        case "opacity":
            guard let arg = args.first, let v = Double(arg) else { break }
            setVisible(true)
            setOpacity(arg.hasPrefix("+") || arg.hasPrefix("-") ? opacity + CGFloat(v) : CGFloat(v))
        case "clickthrough": setClickThrough(args.first == "on")
        case "size": resize(args)
        case "web": if args.first == "close" { closeBrowser() } else if let url = args.first.flatMap(URL.init) { openBrowser(url) }
        default: break
        }
        report()
    }

    func hotKey(_ id: UInt32) {
        switch id {
        case 1: setVisible(!panel.isVisible)
        case 2: setVisible(true); setOpacity(opacity + 0.1)
        case 3: setVisible(true); setOpacity(opacity - 0.1)
        case 4: setClickThrough(!clickThrough)
        default: break
        }
        report()
    }

    // --- a web page in the panel ---------------------------------------------------------------

    func openBrowser(_ url: URL) {
        if browser == nil {
            let view = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
            view.navigationDelegate = self
            view.uiDelegate = self
            view.autoresizingMask = [.width, .height]  // keeps its distance from the top as the panel resizes
            panel.contentView?.addSubview(view)        // above the page
            browser = view
        }
        layoutBrowser()
        browser?.alphaValue = max(opacity, 0.3)
        browser?.load(URLRequest(url: url))
        setVisible(true)
    }

    func closeBrowser() {
        browser?.removeFromSuperview()
        browser = nil
    }

    func layoutBrowser() {
        guard let browser, let bounds = panel.contentView?.bounds else { return }
        browser.frame = NSRect(x: 0, y: 0, width: bounds.width, height: max(0, bounds.height - browserTop))
    }

    /// The page says where its web bar ends (it moves when captions come and go).
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let top = body["browserTop"] as? Double else { return }
        browserTop = CGFloat(top)
        layoutBrowser()
    }

    /// The page's title and address, for its web bar.
    func tellPageAboutBrowser() {
        guard let browser,
              let data = try? JSONSerialization.data(withJSONObject: [browser.title ?? "", browser.url?.absoluteString ?? ""]),
              let args = String(data: data, encoding: .utf8) else { return }
        web.evaluateJavaScript("window.browserPage && window.browserPage.apply(null, \(args))")
    }

    // --- navigation ----------------------------------------------------------------------------

    /// Links in Jarvis's page open in your browser; links in a web page stay in the panel.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if webView === web, action.navigationType == .linkActivated, let url = action.request.url {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
        } else {
            decisionHandler(.allow)
        }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url {
            if webView === browser { browser?.load(URLRequest(url: url)) } else { NSWorkspace.shared.open(url) }
        }
        return nil
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if webView === web { tellPage(); tellPageAboutBrowser() } else { tellPageAboutBrowser(); report() }
    }

    // jarvisd's server may not be listening yet: keep trying.
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        guard webView === web else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { webView.load(URLRequest(url: pageURL)) }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { webView.reload() }
}

// --- hotkeys (Carbon's work without Accessibility permission) ---------------------------------

var workspace: Workspace!

func registerHotKeys() {
    let mods = UInt32(controlKey | optionKey)
    let keys: [(UInt32, Int)] = [(1, kVK_ANSI_J), (2, kVK_UpArrow), (3, kVK_DownArrow), (4, kVK_ANSI_K)]
    for (id, key) in keys {
        var ref: EventHotKeyRef?
        RegisterEventHotKey(UInt32(key), mods, EventHotKeyID(signature: OSType(0x4A52_5653), id: id),  // 'JRVS'
                            GetApplicationEventTarget(), 0, &ref)
    }
    var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
    InstallEventHandler(GetApplicationEventTarget(), { _, event, _ in
        var hotKey = EventHotKeyID()
        GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID), nil,
                          MemoryLayout<EventHotKeyID>.size, nil, &hotKey)
        workspace.hotKey(hotKey.id)
        return noErr
    }, 1, &spec, nil, nil)
}

// --- commands from jarvisd --------------------------------------------------------------------

func readCommands() {
    var pending = ""
    FileHandle.standardInput.readabilityHandler = { handle in
        let data = handle.availableData
        if data.isEmpty {  // jarvisd is gone
            DispatchQueue.main.async { NSApp.terminate(nil) }
            return
        }
        pending += String(decoding: data, as: UTF8.self)
        while let newline = pending.firstIndex(of: "\n") {
            let line = String(pending[..<newline])
            pending = String(pending[pending.index(after: newline)...])
            DispatchQueue.main.async { workspace.command(line) }
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)  // no Dock icon, no menu bar
workspace = Workspace()
registerHotKeys()
readCommands()
app.run()
