import Cocoa

let CONFIG_PATH = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent(".config/usbmuxd-bridge/config.json")

struct Config: Codable {
    var agent_host: String
    var agent_port: Int
    var token: String
    var bridge_bin: String
    var socket_path: String
    var device_cmd: [String]
    var log_path: String
    var report_cmd: [String]
    var report_dir: String
    var open_report: Bool?
    var auto_start: Bool?
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    var config: Config?
    var bridge: Process?
    var timer: Timer?

    // menu item references we mutate each poll
    var statusTitle: NSMenuItem!
    var deviceItem: NSMenuItem!
    var getInfoItem: NSMenuItem!
    var startItem: NSMenuItem!
    var stopItem: NSMenuItem!
    var logItem: NSMenuItem!

    var tunnelActive = false
    var reportRunning = false
    private var checkInFlight = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        loadConfig()
        buildMenu()
        refresh()
        if config?.auto_start == true {
            startBridge()
        }
        timer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
    }

    func loadConfig() {
        guard let data = try? Data(contentsOf: CONFIG_PATH),
              let cfg = try? JSONDecoder().decode(Config.self, from: data) else {
            return
        }
        config = cfg
    }

    func buildMenu() {
        let m = NSMenu()
        statusTitle = NSMenuItem(title: "Tunnel: …", action: nil, keyEquivalent: "")
        statusTitle.isEnabled = false
        m.addItem(statusTitle)
        deviceItem = NSMenuItem(title: "Device: —", action: nil, keyEquivalent: "")
        deviceItem.isEnabled = false
        m.addItem(deviceItem)
        m.addItem(.separator())
        getInfoItem = NSMenuItem(title: "Get Info…", action: #selector(getInfo), keyEquivalent: "i")
        getInfoItem.target = self
        getInfoItem.isEnabled = false
        m.addItem(getInfoItem)
        m.addItem(.separator())
        startItem = NSMenuItem(title: "Start bridge", action: #selector(startBridge), keyEquivalent: "s")
        startItem.target = self
        m.addItem(startItem)
        stopItem = NSMenuItem(title: "Stop bridge", action: #selector(stopBridge), keyEquivalent: "x")
        stopItem.target = self
        m.addItem(stopItem)
        logItem = NSMenuItem(title: "Open bridge log…", action: #selector(openLog), keyEquivalent: "l")
        logItem.target = self
        m.addItem(logItem)
        m.addItem(.separator())
        let quit = NSMenuItem(title: "Quit", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        m.addItem(quit)
        statusItem.menu = m
    }

    // MARK: - Status

    func refresh() {
        guard let cfg = config else {
            setIcon(color: .systemGray, label: "● NUSB")
            statusTitle.title = "Tunnel: no config"
            return
        }
        let running = bridgeRunning()
        let socket = FileManager.default.fileExists(atPath: cfg.socket_path)

        var color: NSColor
        var status: String
        if running && socket {
            color = .systemOrange
            status = "Tunnel: no device"
        } else if running {
            color = .systemYellow
            status = "Tunnel: connecting…"
        } else {
            color = .systemGray
            status = "Tunnel: stopped"
        }
        setIcon(color: color, label: "● NUSB")
        statusTitle.title = status
        deviceItem.title = "Device: …"
        tunnelActive = false
        getInfoItem.isEnabled = false
        startItem.isHidden = running
        stopItem.isHidden = !running
        logItem.isEnabled = FileManager.default.fileExists(atPath: cfg.log_path)

        // Проверка устройств — только когда туннель поднят, и асинхронно:
        // команда pymobiledevice3 уходит в фон, чтобы не блокировать главный
        // поток (иначе выпадающее меню замирает и не открывается).
        if running && socket && !checkInFlight {
            checkInFlight = true
            let cfgCopy = cfg
            Task.detached { [weak self] in
                let devices = Self.queryDevices(cfgCopy)
                await MainActor.run { [weak self] in
                    guard let self else { return }
                    self.checkInFlight = false
                    guard self.bridgeRunning(),
                          FileManager.default.fileExists(atPath: cfgCopy.socket_path) else { return }
                    self.tunnelActive = !devices.isEmpty
                    self.getInfoItem.isEnabled = self.tunnelActive && !self.reportRunning
                    self.deviceItem.title = self.deviceLabel(devices)
                    self.setIcon(color: devices.isEmpty ? .systemOrange : .systemGreen, label: "● NUSB")
                    self.statusTitle.title = devices.isEmpty ? "Tunnel: no device" : "Tunnel: Connected (\(devices.count))"
                }
            }
        }
    }

    func setIcon(color: NSColor, label: String) {
        let attr = NSAttributedString(
            string: label,
            attributes: [.foregroundColor: color, .font: NSFont.systemFont(ofSize: 13, weight: .semibold)]
        )
        statusItem.button?.attributedTitle = attr
    }

    func bridgeRunning() -> Bool {
        bridge?.isRunning ?? false
    }

    /// Run the configured device list command off the main thread with a hard
    /// timeout so a hung pymobiledevice3 (e.g. when the socket is stale) can
    /// never freeze the menu.
    nonisolated static func queryDevices(_ cfg: Config) -> [[String: Any]] {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: cfg.device_cmd[0])
        p.arguments = Array(cfg.device_cmd.dropFirst())
        var env = ProcessInfo.processInfo.environment
        env["USBMUXD_SOCKET_ADDRESS"] = cfg.socket_path
        p.environment = env
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = Pipe()
        do { try p.run() } catch { return [] }

        let sema = DispatchSemaphore(value: 0)
        p.terminationHandler = { _ in sema.signal() }
        if sema.wait(timeout: .now() + 6) == .timedOut {
            p.terminate()
            return []
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        guard let arr = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else {
            return []
        }
        return arr
    }

    func deviceLabel(_ devices: [[String: Any]]) -> String {
        if devices.isEmpty { return "Device: —" }
        let name = devices[0]["DeviceName"] as? String ?? "iPhone"
        let type = devices[0]["ProductType"] as? String ?? "?"
        return "Device: \(name) · \(type)"
    }

    // MARK: - Actions

    /// Run the iScan report (HTML generation) through the tunnel, then open it.
    @objc func getInfo() {
        guard let cfg = config, tunnelActive, !reportRunning else { return }
        reportRunning = true
        getInfoItem.isEnabled = false

        let p = Process()
        p.executableURL = URL(fileURLWithPath: cfg.report_cmd[0])
        p.arguments = Array(cfg.report_cmd.dropFirst())
        p.currentDirectoryURL = URL(fileURLWithPath: cfg.report_dir)
        var env = ProcessInfo.processInfo.environment
        env["USBMUXD_SOCKET_ADDRESS"] = cfg.socket_path
        p.environment = env
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe

        p.terminationHandler = { [weak self] _ in
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            let out = String(data: data, encoding: .utf8) ?? ""
            let saved = Self.extractReportPath(from: out)
            Task { @MainActor in
                guard let self else { return }
                self.reportRunning = false
                self.refresh()
                if let saved, self.config?.open_report == true {
                    NSWorkspace.shared.open(URL(fileURLWithPath: saved))
                }
            }
        }
        do {
            try p.run()
        } catch {
            reportRunning = false
            refresh()
        }
    }

    /// iscan prints "✓ Report saved: <path>" — return the path from the last line.
    nonisolated static func extractReportPath(from output: String) -> String? {
        for line in output.components(separatedBy: .newlines).reversed() {
            if let range = line.range(of: "Report saved: ") {
                return String(line[range.upperBound...]).trimmingCharacters(in: .whitespaces)
            }
        }
        return nil
    }

    @objc func startBridge() {
        guard let cfg = config, !bridgeRunning() else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: cfg.bridge_bin)
        p.arguments = [
            "--agent-host", cfg.agent_host,
            "--agent-port", String(cfg.agent_port),
            "--token", cfg.token,
            "--log-level", "INFO",
        ]
        if !FileManager.default.fileExists(atPath: cfg.log_path) {
            FileManager.default.createFile(atPath: cfg.log_path, contents: nil)
        }
        if let fh = FileHandle(forWritingAtPath: cfg.log_path) {
            fh.seekToEndOfFile()
            p.standardOutput = fh
            p.standardError = fh
        }
        p.terminationHandler = { [weak self] _ in
            Task { @MainActor in
                self?.bridge = nil
                self?.refresh()
            }
        }
        do {
            try p.run()
            bridge = p
        } catch {
            print("failed to start bridge: \(error)")
        }
        refresh()
    }

    @objc func stopBridge() {
        bridge?.terminate()
        bridge = nil
        refresh()
    }

    @objc func openLog() {
        guard let cfg = config else { return }
        NSWorkspace.shared.openFile(cfg.log_path)
    }

    @objc func quit() {
        NSApp.terminate(nil)
    }
}

@main
struct NetworkUSBMenuApp {
    @MainActor
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.run()
    }
}
