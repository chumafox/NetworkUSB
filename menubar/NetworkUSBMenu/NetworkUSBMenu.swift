import Cocoa

let CONFIG_PATH = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent(".config/usbmuxd-bridge/config.json")

struct StoreConfig: Codable, Equatable {
    var id: String?
    var name: String
    var host: String
    var port: Int?
    var token: String?

    var effectivePort: Int { port ?? 8721 }
}

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

    var stores: [StoreConfig]?
    var active_store_id: String?
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    var config: Config?
    var bridge: Process?
    var timer: Timer?

    // Submenu references
    var statusTitle: NSMenuItem!
    var storeSubmenuItem: NSMenuItem!
    var storeSubmenu: NSMenu!
    var deviceItem: NSMenuItem!
    var getInfoItem: NSMenuItem!
    var startItem: NSMenuItem!
    var stopItem: NSMenuItem!
    var logItem: NSMenuItem!

    var tunnelActive = false
    var reportRunning = false
    private var checkInFlight = false
    private var currentStores: [StoreConfig] = []

    func applicationDidFinishLaunching(_ notification: Notification) {
        loadConfig()
        ensureDefaultStores()
        buildMenu()
        refresh()

        if config?.auto_start == true {
            startBridge()
        }

        // Poll status every 4 seconds
        timer = Timer.scheduledTimer(withTimeInterval: 4.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }

        // Run initial Tailscale store discovery in background
        Task {
            await self.discoverTailscaleStores()
        }
    }

    func loadConfig() {
        guard let data = try? Data(contentsOf: CONFIG_PATH),
              let cfg = try? JSONDecoder().decode(Config.self, from: data) else {
            return
        }
        config = cfg
    }

    func saveConfig() {
        guard let cfg = config else { return }
        let encoder = JSONEncoder()
        encoder.outputFormatting = .prettyPrinted
        if let data = try? encoder.encode(cfg) {
            try? data.write(to: CONFIG_PATH)
        }
    }

    func ensureDefaultStores() {
        guard var cfg = config else { return }
        var list = cfg.stores ?? []

        // If list is empty, seed with current host & local VM defaults
        if list.isEmpty {
            let currentStore = StoreConfig(
                id: "active_current",
                name: "Текущий сервер (\(cfg.agent_host))",
                host: cfg.agent_host,
                port: cfg.agent_port,
                token: cfg.token
            )
            let tartStore = StoreConfig(
                id: "tart_vm",
                name: "Локальная ВМ Tart (127.0.0.1)",
                host: "127.0.0.1",
                port: 8721,
                token: cfg.token
            )
            list = [currentStore, tartStore]
            cfg.stores = list
            cfg.active_store_id = currentStore.id
            self.config = cfg
            saveConfig()
        }
        self.currentStores = list
    }

    func buildMenu() {
        let m = NSMenu()
        statusTitle = NSMenuItem(title: "Tunnel: …", action: nil, keyEquivalent: "")
        statusTitle.isEnabled = false
        m.addItem(statusTitle)

        // Store selector dropdown item
        storeSubmenuItem = NSMenuItem(title: "📍 Выбор магазина…", action: nil, keyEquivalent: "")
        storeSubmenu = NSMenu()
        storeSubmenuItem.submenu = storeSubmenu
        m.addItem(storeSubmenuItem)

        m.addItem(.separator())

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

    func rebuildStoreSubmenu() {
        guard let cfg = config else { return }
        storeSubmenu.removeAllItems()

        let activeHost = cfg.agent_host
        var activeName = activeHost

        for (index, store) in currentStores.enumerated() {
            let isSelected = (store.host == activeHost)
            if isSelected {
                activeName = store.name
            }

            let title = "\(isSelected ? "✓ " : "   ")\(store.name) (\(store.host):\(store.effectivePort))"
            let item = NSMenuItem(title: title, action: #selector(selectStoreItem(_:)), keyEquivalent: "")
            item.target = self
            item.tag = index
            storeSubmenu.addItem(item)
        }

        storeSubmenu.addItem(.separator())

        let refreshItem = NSMenuItem(title: "🔄 Сканировать сеть Tailscale…", action: #selector(triggerTailscaleDiscovery), keyEquivalent: "r")
        refreshItem.target = self
        storeSubmenu.addItem(refreshItem)

        storeSubmenuItem.title = "📍 Магазин: \(activeName)"
    }

    @objc func selectStoreItem(_ sender: NSMenuItem) {
        let index = sender.tag
        guard index >= 0 && index < currentStores.count else { return }
        let selectedStore = currentStores[index]
        switchToStore(selectedStore)
    }

    func switchToStore(_ store: StoreConfig) {
        guard var cfg = config else { return }

        // Stop current bridge if running
        stopBridge()

        cfg.agent_host = store.host
        cfg.agent_port = store.effectivePort
        if let t = store.token, !t.isEmpty {
            cfg.token = t
        }
        cfg.active_store_id = store.id ?? store.host
        self.config = cfg
        saveConfig()

        rebuildStoreSubmenu()

        // Auto restart bridge for newly selected store
        startBridge()
    }

    @objc func triggerTailscaleDiscovery() {
        Task {
            await discoverTailscaleStores()
        }
    }

    func discoverTailscaleStores() async {
        let peers = await Task.detached { () -> [StoreConfig] in
            let paths = [
                "/usr/local/bin/tailscale",
                "/opt/homebrew/bin/tailscale",
                "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                "/usr/bin/tailscale"
            ]
            var binPath: String?
            for p in paths {
                if FileManager.default.fileExists(atPath: p) {
                    binPath = p
                    break
                }
            }
            guard let bin = binPath else { return [] }

            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: bin)
            proc.arguments = ["status", "--json"]
            let pipe = Pipe()
            proc.standardOutput = pipe
            proc.standardError = Pipe()
            do { try proc.run() } catch { return [] }

            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let peerMap = json["Peer"] as? [String: [String: Any]] else {
                return []
            }

            var result: [StoreConfig] = []
            for (_, peer) in peerMap {
                let name = (peer["HostName"] as? String) ?? (peer["DNSName"] as? String) ?? "Unknown Shop"
                let online = (peer["Online"] as? Bool) ?? false
                guard online else { continue }
                if let ips = peer["TailscaleIPs"] as? [String], let ip = ips.first {
                    let cleanName = name.replacingOccurrences(of: ".tailnet.net.", with: "")
                    result.append(StoreConfig(
                        id: "ts_\(ip)",
                        name: "🛍 \(cleanName)",
                        host: ip,
                        port: 8721,
                        token: nil
                    ))
                }
            }
            return result
        }.value

        guard !peers.isEmpty else { return }

        // Merge discovered peers into current stores
        var updated = currentStores
        for p in peers {
            if !updated.contains(where: { $0.host == p.host }) {
                updated.append(p)
            }
        }
        self.currentStores = updated
        if var cfg = config {
            cfg.stores = updated
            self.config = cfg
            saveConfig()
        }
        rebuildStoreSubmenu()
    }

    // MARK: - Status & Refresh

    func refresh() {
        guard let cfg = config else {
            setIcon(color: .systemGray, label: "● NUSB")
            statusTitle.title = "Tunnel: no config"
            return
        }
        rebuildStoreSubmenu()

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
        NSWorkspace.shared.open(URL(fileURLWithPath: cfg.log_path))
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
