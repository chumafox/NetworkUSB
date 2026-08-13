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
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let mainMenu = NSMenu()
    var config: Config?
    var bridge: Process?
    var timer: Timer?

    var tunnelActive = false
    var reportRunning = false
    var isScanning = false
    var scanStatusMessage: String?

    private var checkInFlight = false
    private var currentStores: [StoreConfig] = []
    private var currentDevices: [[String: Any]] = []

    func applicationDidFinishLaunching(_ notification: Notification) {
        mainMenu.delegate = self
        statusItem.menu = mainMenu

        loadConfig()
        ensureDefaultStores()
        updateIcon()

        // Background status polling loop every 4 seconds
        timer = Timer.scheduledTimer(withTimeInterval: 4.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.pollStatus() }
        }

        // Auto discover stores in background silently
        Task {
            await self.discoverStores()
        }
    }

    func loadConfig() {
        if let data = try? Data(contentsOf: CONFIG_PATH),
           let cfg = try? JSONDecoder().decode(Config.self, from: data) {
            config = cfg
            return
        }
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let fallback = Config(
            agent_host: "",
            agent_port: 8721,
            token: "",
            bridge_bin: "\(home)/Projects/NetworkUSB/.venv/bin/usbmuxd-bridge",
            socket_path: "/tmp/usbmuxd.sock",
            device_cmd: ["\(home)/Projects/iScan/.venv/bin/pymobiledevice3", "usbmux", "list"],
            log_path: "\(home)/Library/Logs/networkusb-bridge.log",
            report_cmd: ["\(home)/Projects/iScan/.venv/bin/iscan", "report"],
            report_dir: "\(home)/Projects/iScan",
            open_report: true,
            auto_start: false,
            stores: [],
            active_store_id: nil
        )
        config = fallback
        saveConfig()
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
        guard let cfg = config else { return }
        self.currentStores = cfg.stores ?? []
    }

    // MARK: - NSMenuDelegate (Triggered instantly when user clicks the menu icon)

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()

        guard let cfg = config else {
            let item = NSMenuItem(title: "Tunnel: no config", action: nil, keyEquivalent: "")
            item.isEnabled = false
            menu.addItem(item)
            return
        }

        let running = bridgeRunning()
        let socket = FileManager.default.fileExists(atPath: cfg.socket_path)

        var statusText: String
        if isScanning {
            statusText = "Tunnel: scanning bridge…"
        } else if cfg.agent_host.isEmpty {
            statusText = "Tunnel: no server selected"
        } else if running && socket {
            statusText = currentDevices.isEmpty ? "Tunnel: no device" : "Tunnel: Connected (\(currentDevices.count))"
        } else if running {
            statusText = "Tunnel: connecting…"
        } else {
            statusText = "Tunnel: stopped"
        }

        let statusItem = NSMenuItem(title: statusText, action: nil, keyEquivalent: "")
        statusItem.isEnabled = false
        menu.addItem(statusItem)

        menu.addItem(.separator())

        // SERVERS Section Header
        let storesHeader = NSMenuItem(title: "SERVERS:", action: nil, keyEquivalent: "")
        storesHeader.isEnabled = false
        menu.addItem(storesHeader)

        if currentStores.isEmpty {
            let emptyItem = NSMenuItem(title: "   (No servers discovered)", action: nil, keyEquivalent: "")
            emptyItem.isEnabled = false
            menu.addItem(emptyItem)
        } else {
            for (index, store) in currentStores.enumerated() {
                let isSelected = (store.host == cfg.agent_host)
                let title = "   \(isSelected ? "✓ " : "  ")\(store.host):\(store.effectivePort)"
                let item = NSMenuItem(title: title, action: #selector(selectStoreItem(_:)), keyEquivalent: "")
                item.target = self
                item.tag = index
                menu.addItem(item)
            }
        }

        menu.addItem(.separator())

        if isScanning {
            let scanningItem = NSMenuItem(title: "Scanning bridge…", action: nil, keyEquivalent: "")
            scanningItem.isEnabled = false
            menu.addItem(scanningItem)
        } else {
            let scanItem = NSMenuItem(title: "Scan Bridge", action: #selector(triggerStoreDiscovery), keyEquivalent: "r")
            scanItem.target = self
            menu.addItem(scanItem)
        }


        if !currentStores.isEmpty {
            let clearItem = NSMenuItem(title: "Clear Servers", action: #selector(clearStoresList), keyEquivalent: "")
            clearItem.target = self
            menu.addItem(clearItem)
        }

        menu.addItem(.separator())

        let devLabel = deviceLabel(currentDevices)
        let deviceMenuItem = NSMenuItem(title: devLabel, action: nil, keyEquivalent: "")
        deviceMenuItem.isEnabled = false
        menu.addItem(deviceMenuItem)

        let getInfoItem = NSMenuItem(title: "Get Info…", action: #selector(getInfo), keyEquivalent: "i")
        getInfoItem.target = self
        getInfoItem.isEnabled = tunnelActive && !reportRunning
        menu.addItem(getInfoItem)

        menu.addItem(.separator())

        if !running && !cfg.agent_host.isEmpty {
            let startItem = NSMenuItem(title: "Start bridge", action: #selector(startBridge), keyEquivalent: "s")
            startItem.target = self
            menu.addItem(startItem)
        } else if running {
            let stopItem = NSMenuItem(title: "Stop bridge", action: #selector(stopBridge), keyEquivalent: "x")
            stopItem.target = self
            menu.addItem(stopItem)
        }

        let logItem = NSMenuItem(title: "Open bridge log…", action: #selector(openLog), keyEquivalent: "l")
        logItem.target = self
        logItem.isEnabled = FileManager.default.fileExists(atPath: cfg.log_path)
        menu.addItem(logItem)

        menu.addItem(.separator())

        let quitItem = NSMenuItem(title: "Quit", action: #selector(quit), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)
    }

    // MARK: - Store / Server Actions

    @objc func selectStoreItem(_ sender: NSMenuItem) {
        let index = sender.tag
        guard index >= 0 && index < currentStores.count else { return }
        let selectedStore = currentStores[index]
        switchToStore(selectedStore)
    }

    func switchToStore(_ store: StoreConfig) {
        guard var cfg = config else { return }

        stopBridge()

        cfg.agent_host = store.host
        cfg.agent_port = store.effectivePort
        if let t = store.token, !t.isEmpty {
            cfg.token = t
        }
        cfg.active_store_id = store.id ?? store.host
        self.config = cfg
        saveConfig()

        updateIcon()
        startBridge()
    }

    @objc func clearStoresList() {
        guard var cfg = config else { return }
        currentStores = []
        cfg.stores = []
        cfg.agent_host = ""
        cfg.active_store_id = nil
        self.config = cfg
        saveConfig()
        stopBridge()
        updateIcon()
    }

    @objc func triggerStoreDiscovery() {
        Task {
            await discoverStores()
        }
    }

    func discoverStores() async {
        isScanning = true
        scanStatusMessage = "Scanning…"
        updateIcon()

        let (foundStores, _) = await Task.detached { () -> ([StoreConfig], String) in
            var discovered: [StoreConfig] = []
            var statusNote = ""

            // 1. Check local Tart VM / Port 8721 readiness
            let localhost = "127.0.0.1"
            let socket = socket(AF_INET, SOCK_STREAM, 0)
            if socket >= 0 {
                var addr = sockaddr_in()
                addr.sin_family = sa_family_t(AF_INET)
                addr.sin_port = in_port_t(8721).bigEndian
                inet_pton(AF_INET, localhost, &addr.sin_addr)
                let connected = withUnsafePointer(to: &addr) {
                    $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                        connect(socket, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) == 0
                    }
                }
                close(socket)
                if connected {
                    discovered.append(StoreConfig(
                        id: "tart_vm_auto",
                        name: "Local Tart VM",
                        host: localhost,
                        port: 8721,
                        token: "0f1cead0241a2580faa848c351a82a5f1cef945573e8a059e3d5ceba6f6c22cb"
                    ))
                }
            }

            // 2. Discover Tailscale peers
            let paths = [
                "/opt/homebrew/bin/tailscale",
                "/usr/local/bin/tailscale",
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

            if let bin = binPath {
                let proc = Process()
                proc.executableURL = URL(fileURLWithPath: bin)
                proc.arguments = ["status", "--json"]
                let pipe = Pipe()
                proc.standardOutput = pipe
                proc.standardError = Pipe()
                if (try? proc.run()) != nil {
                    let data = pipe.fileHandleForReading.readDataToEndOfFile()
                    if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                       let peerMap = json["Peer"] as? [String: [String: Any]] {
                        for (_, peer) in peerMap {
                            let name = (peer["HostName"] as? String) ?? (peer["DNSName"] as? String) ?? "Unknown Server"
                            let online = (peer["Online"] as? Bool) ?? false
                            guard online else { continue }
                            if let ips = peer["TailscaleIPs"] as? [String], let ip = ips.first {
                                let cleanName = name.replacingOccurrences(of: ".tailnet.net.", with: "")
                                discovered.append(StoreConfig(
                                    id: "ts_\(ip)",
                                    name: "Server (\(cleanName))",
                                    host: ip,
                                    port: 8721,
                                    token: nil
                                ))
                            }
                        }
                    } else {
                        statusNote = "Tailscale not authenticated."
                    }
                }
            } else {
                statusNote = "Tailscale CLI not found."
            }

            return (discovered, statusNote)
        }.value

        isScanning = false

        if !foundStores.isEmpty {
            var updated = currentStores
            for s in foundStores {
                if !updated.contains(where: { $0.host == s.host }) {
                    updated.append(s)
                }
            }
            self.currentStores = updated
            if var cfg = config {
                cfg.stores = updated
                // Auto activate first discovered store if none currently active
                if cfg.agent_host.isEmpty, let first = foundStores.first {
                    cfg.agent_host = first.host
                    cfg.agent_port = first.effectivePort
                    if let t = first.token { cfg.token = t }
                    cfg.active_store_id = first.id
                }
                self.config = cfg
                saveConfig()
            }
            if let activeStore = foundStores.first, config?.agent_host == activeStore.host {
                switchToStore(activeStore)
            }
        }

        let summaryText: String
        if !foundStores.isEmpty {
            summaryText = "Bridge: \(foundStores.count)"
        } else {
            summaryText = "Bridge: 0"
        }
        self.scanStatusMessage = summaryText
        updateIcon()
    }

    // MARK: - Polling & Icon Status

    func pollStatus() {
        guard let cfg = config else {
            setIcon(color: .systemGray, label: "● NUSB")
            return
        }

        updateIcon()

        let running = bridgeRunning()
        let socket = FileManager.default.fileExists(atPath: cfg.socket_path)

        if running && socket && !checkInFlight {
            checkInFlight = true
            let cfgCopy = cfg
            Task.detached { [weak self] in
                let devices = Self.queryDevices(cfgCopy)
                await MainActor.run { [weak self] in
                    guard let self else { return }
                    self.checkInFlight = false
                    self.currentDevices = devices
                    self.tunnelActive = !devices.isEmpty
                    self.updateIcon()
                }
            }
        }
    }

    func updateIcon() {
        guard let cfg = config else {
            setIcon(color: .systemGray, label: "● NUSB")
            return
        }
        let running = bridgeRunning()
        let socket = FileManager.default.fileExists(atPath: cfg.socket_path)

        var color: NSColor
        var label = "● NUSB"
        if isScanning {
            color = .systemBlue
            label = "● NUSB (scanning…)"
        } else if cfg.agent_host.isEmpty {
            color = .systemGray
        } else if running && socket {
            color = currentDevices.isEmpty ? .systemOrange : .systemGreen
        } else if running {
            color = .systemYellow
        } else {
            color = .systemGray
        }
        setIcon(color: color, label: label)
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
        guard !cfg.socket_path.isEmpty else { return [] }
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
                self.updateIcon()
                if let saved, self.config?.open_report == true {
                    NSWorkspace.shared.open(URL(fileURLWithPath: saved))
                }
            }
        }
        do {
            try p.run()
        } catch {
            reportRunning = false
            updateIcon()
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
        guard let cfg = config, !cfg.agent_host.isEmpty, !bridgeRunning() else { return }
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
                self?.updateIcon()
            }
        }
        do {
            try p.run()
            bridge = p
        } catch {
            print("failed to start bridge: \(error)")
        }
        updateIcon()
    }

    @objc func stopBridge() {
        bridge?.terminate()
        bridge = nil
        updateIcon()
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
