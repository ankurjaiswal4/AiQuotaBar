// AiQuotaBar: native menu bar panel. The Python engine (aiquotabar.py) owns all logic;
// this app only runs it, decodes its JSON, draws the panel, posts the alerts the engine
// decided on and asks it to change settings.

import AppKit
import SwiftUI
import UserNotifications

// MARK: - Data model (decoded leniently: missing or odd fields never throw)

extension KeyedDecodingContainer {
    func lenient<T: Decodable>(_ key: Key, _ fallback: T) -> T {
        (try? decodeIfPresent(T.self, forKey: key)) ?? fallback
    }

    func lenientOptional<T: Decodable>(_ key: Key) -> T? {
        try? decodeIfPresent(T.self, forKey: key)
    }
}

enum UsageState: String {
    case ok, warn, hot, stale

    init(raw: String) {
        self = UsageState(rawValue: raw) ?? .stale
    }
}

struct EngineMessage: Decodable {
    var title: String
    var body: String

    enum CodingKeys: String, CodingKey { case title, body }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        title = c.lenient(.title, "")
        body = c.lenient(.body, "")
    }
}

// "At this pace the weekly limit runs out today 23:54, about 4 days before it resets."
struct Pace: Decodable {
    var subject: String
    var when: String
    var lead: String

    enum CodingKeys: String, CodingKey { case subject, when, lead }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        subject = c.lenient(.subject, "limit")
        when = c.lenient(.when, "")
        lead = c.lenient(.lead, "")
    }
}

struct Ring: Decodable {
    var title: String
    var used: Int
    var left: Int
    var state: UsageState
    var reset: String
    var countdown: String
    var pace: Pace?

    enum CodingKeys: String, CodingKey { case title, used, left, state, reset, countdown, pace }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        title = c.lenient(.title, "")
        used = c.lenient(.used, 0)
        left = c.lenient(.left, 100 - used)
        state = UsageState(raw: c.lenient(.state, "stale"))
        reset = c.lenient(.reset, "")
        countdown = c.lenient(.countdown, "")
        pace = c.lenientOptional(.pace)
    }
}

struct LimitRow: Decodable {
    var name: String
    var used: Int
    var left: Int
    var state: UsageState
    var reset: String
    var pace: Pace?

    enum CodingKeys: String, CodingKey { case name, used, left, state, reset, pace }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = c.lenient(.name, "")
        used = c.lenient(.used, 0)
        left = c.lenient(.left, 100 - used)
        state = UsageState(raw: c.lenient(.state, "stale"))
        reset = c.lenient(.reset, "")
        pace = c.lenientOptional(.pace)
    }
}

struct ActivityRow: Decodable {
    var project: String
    var effort: String
    var minutes: Int
    var due: Bool
    var sessions: Int

    enum CodingKeys: String, CodingKey { case project, effort, minutes, due, sessions }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        project = c.lenient(.project, "")
        effort = c.lenient(.effort, "")
        minutes = c.lenient(.minutes, 0)
        due = c.lenient(.due, false)
        sessions = c.lenient(.sessions, 1)
    }

    // Only long-running high effort is worth showing a duration for.
    var showsMinutes: Bool { ["high", "xhigh", "max"].contains(effort) }
}

// "Where your week went": each surface's share of this week's usage.
struct Breakdown: Decodable {
    struct Row: Decodable {
        var name: String
        var percent: Int

        enum CodingKeys: String, CodingKey { case name, percent }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            name = c.lenient(.name, "")
            percent = c.lenient(.percent, 0)
        }
    }

    var since: String?
    var rows: [Row]

    enum CodingKeys: String, CodingKey { case since, rows }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        since = c.lenientOptional(.since)
        rows = c.lenient(.rows, [])
    }
}

// One `Wk 88%` piece of the menu bar; the app colours each by its own state.
struct MenubarPart: Decodable {
    var label: String
    var value: String
    var kind: String
    var state: UsageState

    enum CodingKeys: String, CodingKey { case label, value, kind, state }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        label = c.lenient(.label, "")
        value = c.lenient(.value, "")
        kind = c.lenient(.kind, "")
        state = UsageState(raw: c.lenient(.state, "stale"))
    }
}

struct EngineSettings: Decodable {
    var launchAtLogin: Bool
    var notifications: Bool
    var menubar: String
    var alertFrom: Int
    var alertChoices: [Int]

    enum CodingKeys: String, CodingKey {
        case launchAtLogin = "launch_at_login"
        case notifications, menubar
        case alertFrom = "alert_from"
        case alertChoices = "alert_choices"
    }

    init() {
        launchAtLogin = false
        notifications = false
        menubar = "both"
        alertFrom = 25
        alertChoices = []
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        launchAtLogin = c.lenient(.launchAtLogin, false)
        notifications = c.lenient(.notifications, false)
        menubar = c.lenient(.menubar, "both")
        alertFrom = c.lenient(.alertFrom, 25)
        alertChoices = c.lenient(.alertChoices, [])
    }
}

struct EngineState: Decodable {
    var schema: Int
    var version: String
    var mode: String
    var updated: String?
    var offlineSince: String?
    var message: EngineMessage?
    var menubarText: String
    var menubarParts: [MenubarPart]
    var menubarSuffix: String
    var rings: [Ring]
    var limits: [LimitRow]
    var breakdown: Breakdown?
    var activity: [ActivityRow]
    var settings: EngineSettings
    var notificationHint: String
    // Alert texts the engine decided are due on this run (each one is posted once).
    var alerts: [String]

    enum CodingKeys: String, CodingKey {
        case schema, version, mode, updated
        case offlineSince = "offline_since"
        case message, menubar, rings, limits, breakdown, activity, settings
        case notificationHint = "notification_hint"
        case alerts
    }

    // The menubar block's overall state is ignored: each part carries its own.
    private struct MenubarBlock: Decodable {
        var text: String
        var parts: [MenubarPart]
        var suffix: String

        enum CodingKeys: String, CodingKey { case text, parts, suffix }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            text = c.lenient(.text, "◌ —")
            parts = c.lenient(.parts, [])
            suffix = c.lenient(.suffix, "")
        }
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        schema = c.lenient(.schema, 0)
        version = c.lenient(.version, "")
        mode = c.lenient(.mode, "error")
        updated = c.lenientOptional(.updated)
        offlineSince = c.lenientOptional(.offlineSince)
        message = c.lenientOptional(.message)
        let menubarBlock: MenubarBlock? = c.lenientOptional(.menubar)
        menubarText = menubarBlock?.text ?? "◌ —"
        menubarParts = menubarBlock?.parts ?? []
        menubarSuffix = menubarBlock?.suffix ?? ""
        rings = c.lenient(.rings, [])
        limits = c.lenient(.limits, [])
        breakdown = c.lenientOptional(.breakdown)
        activity = c.lenient(.activity, [])
        settings = c.lenient(.settings, EngineSettings())
        notificationHint = c.lenient(.notificationHint, "")
        alerts = c.lenient(.alerts, [])
    }
}

// MARK: - Colours

enum Palette {
    private static func dynamic(light: UInt32, dark: UInt32) -> NSColor {
        NSColor(name: nil) { appearance in
            let isDark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            return nsColor(hex: isDark ? dark : light)
        }
    }

    private static func color(light: UInt32, dark: UInt32) -> Color {
        Color(nsColor: dynamic(light: light, dark: dark))
    }

    private static func nsColor(hex: UInt32) -> NSColor {
        NSColor(
            srgbRed: CGFloat((hex >> 16) & 0xFF) / 255,
            green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255,
            alpha: 1
        )
    }

    // A limit that's fine is drawn in neutral ink (like the Mac battery icon);
    // amber and red appear only when something needs attention.
    static let ok = color(light: 0x5f6066, dark: 0xd4d5da)
    static let warn = color(light: 0xb25e00, dark: 0xffb340)
    static let hot = color(light: 0xc8323f, dark: 0xff6170)
    static let stale = color(light: 0x8a8c94, dark: 0x8f94a3)
    static let live = color(light: 0x30b75f, dark: 0x34c77b)
    // The app's own mark, and the "on" colour of the settings switches.
    static let clay = color(light: 0xd97a55, dark: 0xd97a55)

    // Menu bar only: each window keeps its own colour while it's fine, so the two
    // numbers are told apart at a glance; amber and red still take over.
    private static let menuWeek = dynamic(light: 0x0a63c9, dark: 0x6cb4ff)
    private static let menuSession = dynamic(light: 0x00796b, dark: 0x4fd6c8)
    private static let menuWarn = dynamic(light: 0xa35400, dark: 0xffb340)
    private static let menuHot = dynamic(light: 0xc21f30, dark: 0xff6170)

    static func menuBarColor(for part: MenubarPart) -> NSColor {
        switch part.state {
        case .warn: return menuWarn
        case .hot: return menuHot
        case .stale: return .secondaryLabelColor
        case .ok:
            switch part.kind {
            case "week": return menuWeek
            case "session": return menuSession
            default: return .labelColor
            }
        }
    }

    static func color(for state: UsageState) -> Color {
        switch state {
        case .ok: return ok
        case .warn: return warn
        case .hot: return hot
        case .stale: return stale
        }
    }

    static let track = Color.primary.opacity(0.12)
    static let sectionFill = Color.primary.opacity(0.06)
}

// MARK: - Engine runner (blocking; always called off the main thread)

struct EngineFailure: Error {
    let reason: String

    init(_ reason: String) {
        // Keep messages short and never echo engine output.
        self.reason = String(reason.prefix(120))
    }
}

enum EngineRunner {
    static let interpreter = "/usr/bin/python3"
    static let timeoutSeconds: Double = 20

    static func fetchState() -> Result<EngineState, EngineFailure> {
        switch launch(arguments: ["--json"]) {
        case .failure(let failure):
            return .failure(failure)
        case .success(let data):
            guard let state = try? JSONDecoder().decode(EngineState.self, from: data) else {
                return .failure(EngineFailure("The engine returned unreadable data."))
            }
            return .success(state)
        }
    }

    // A failed --set is fine: the refresh that follows shows what the engine really has.
    static func applySetting(token: String) {
        _ = launch(arguments: ["--set", token])
    }

    private static func launch(arguments: [String]) -> Result<Data, EngineFailure> {
        guard FileManager.default.isExecutableFile(atPath: interpreter) else {
            return .failure(EngineFailure("python3 was not found on this Mac."))
        }
        guard let resources = Bundle.main.resourceURL else {
            return .failure(EngineFailure("The app bundle has no resources."))
        }
        let scriptPath = resources.appendingPathComponent("aiquotabar.py").path
        guard FileManager.default.fileExists(atPath: scriptPath) else {
            return .failure(EngineFailure("aiquotabar.py is missing from the app."))
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: interpreter)
        process.arguments = [scriptPath] + arguments
        let output = Pipe()
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        process.standardInput = FileHandle.nullDevice

        do {
            try process.run()
        } catch {
            return .failure(EngineFailure("The engine could not be started."))
        }

        let killer = DispatchWorkItem {
            if process.isRunning { process.terminate() }
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + timeoutSeconds, execute: killer)

        // Reading to the end also unblocks when the killer ends the process.
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        killer.cancel()

        if process.terminationReason == .uncaughtSignal {
            return .failure(EngineFailure("The engine took too long to answer."))
        }
        if process.terminationStatus != 0 {
            return .failure(EngineFailure("The engine exited with code \(process.terminationStatus)."))
        }
        return .success(data)
    }
}

// MARK: - Settings changes (tokens are built only from fixed values)

// Weekly comes first everywhere, matching the order of the panel's rings.
enum MenuBarChoice: String, CaseIterable {
    case weekly
    case session
    case both

    var title: String { rawValue.capitalized }
}

enum SettingChange {
    case launchAtLogin(Bool)
    case notifications(Bool)
    case menubar(MenuBarChoice)
    case minThreshold(Int)

    var token: String {
        switch self {
        case .launchAtLogin(let on): return "login=" + (on ? "on" : "off")
        case .notifications(let on): return "notifications=" + (on ? "on" : "off")
        case .menubar(let choice): return "menubar=" + choice.rawValue
        case .minThreshold(let percent): return "min_threshold=\(percent)"
        }
    }
}

// MARK: - Notifications

// The engine keeps the fire-once bookkeeping; the app only posts what it is handed,
// so the alert comes from AiQuotaBar (with its icon) instead of a helper process.
enum Notifier {
    static func requestPermission() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    static func post(_ bodies: [String]) {
        let center = UNUserNotificationCenter.current()
        for body in bodies {
            let content = UNMutableNotificationContent()
            content.title = "AiQuotaBar"
            content.body = body
            let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
            center.add(request) { _ in }
        }
    }
}

// MARK: - Observable model

@MainActor
final class UsageModel: ObservableObject {
    @Published private(set) var state: EngineState?
    @Published private(set) var lastError: String?
    @Published private(set) var runsInFlight = 0
    // Drives the menu bar plate: shown while closed, hidden while open.
    @Published private(set) var isPanelOpen = false

    var isRefreshing: Bool { runsInFlight > 0 }

    // Setting changes the engine hasn't saved yet. While any are pending, a run
    // that finishes keeps the settings on screen, so a refresh queued before the
    // click can't flip a switch back for a moment.
    private var settingChangesPending = 0

    // One serial queue keeps engine runs from ever overlapping.
    private let engineQueue = DispatchQueue(label: "aiquotabar.engine")
    private var timer: Timer?
    private var windowObservers: [NSObjectProtocol] = []

    init() {
        timer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        watchPanelWindow()
        refresh()
    }

    // The panel is the app's only window besides the status item, and it becomes
    // key on every open and resigns on every close. SwiftUI's onAppear isn't
    // reliable here: the panel view stays alive between clicks.
    private func watchPanelWindow() {
        let center = NotificationCenter.default
        let pairs: [(Notification.Name, Bool)] = [
            (NSWindow.didBecomeKeyNotification, true),
            (NSWindow.didResignKeyNotification, false),
        ]
        for (name, isOpen) in pairs {
            let observer = center.addObserver(forName: name, object: nil, queue: .main) { [weak self] note in
                guard let window = note.object as? NSWindow,
                      !String(describing: type(of: window)).contains("StatusBar") else { return }
                Task { @MainActor in
                    guard let self else { return }
                    self.isPanelOpen = isOpen
                    self.pushMenuBarImage()
                }
            }
            windowObservers.append(observer)
        }
    }

    // While the panel is open macOS draws its own highlight behind the item, so
    // our plate steps aside. SwiftUI doesn't refresh the label during that
    // highlight, so the image is also set on the status bar button directly.
    private func pushMenuBarImage() {
        guard let state, !state.menubarParts.isEmpty, let button = Self.statusButton() else { return }
        button.image = MenuBarImage.make(parts: state.menubarParts, suffix: state.menubarSuffix,
                                         showsPlate: !isPanelOpen)
    }

    private static func statusButton() -> NSStatusBarButton? {
        for window in NSApp.windows where String(describing: type(of: window)).contains("StatusBar") {
            if let button = findButton(in: window.contentView) {
                return button
            }
        }
        return nil
    }

    private static func findButton(in view: NSView?) -> NSStatusBarButton? {
        guard let view else { return nil }
        if let button = view as? NSStatusBarButton { return button }
        for subview in view.subviews {
            if let button = findButton(in: subview) { return button }
        }
        return nil
    }

    func refresh() {
        guard !isRefreshing else { return }
        run(settingToken: nil)
    }

    func change(_ change: SettingChange) {
        applyOptimistically(change)
        settingChangesPending += 1
        run(settingToken: change.token)
    }

    private func run(settingToken: String?) {
        runsInFlight += 1
        engineQueue.async {
            if let token = settingToken {
                EngineRunner.applySetting(token: token)
            }
            let result = EngineRunner.fetchState()
            Task { @MainActor in self.finish(result, savedSetting: settingToken != nil) }
        }
    }

    private func finish(_ result: Result<EngineState, EngineFailure>, savedSetting: Bool) {
        runsInFlight -= 1
        if savedSetting { settingChangesPending -= 1 }
        switch result {
        case .success(var newState):
            if settingChangesPending > 0, let shown = state?.settings {
                newState.settings = shown
            }
            state = newState
            lastError = nil
            pushMenuBarImage()
            Notifier.post(newState.alerts)
        case .failure(let failure):
            lastError = failure.reason
        }
    }

    private func applyOptimistically(_ change: SettingChange) {
        switch change {
        case .launchAtLogin(let on): state?.settings.launchAtLogin = on
        case .notifications(let on): state?.settings.notifications = on
        case .menubar(let choice): state?.settings.menubar = choice.rawValue
        case .minThreshold(let percent): state?.settings.alertFrom = percent
        }
    }
}

// MARK: - Small shared views

struct SectionCard<Content: View>: View {
    @ViewBuilder var content: Content

    var body: some View {
        content
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 10).fill(Palette.sectionFill))
    }
}

struct RingGauge: View {
    let used: Int
    let left: Int
    let color: Color

    private var fraction: Double { min(max(Double(used) / 100, 0), 1) }

    var body: some View {
        ZStack {
            Circle().stroke(Palette.track, lineWidth: 8)
            Circle()
                .trim(from: 0, to: fraction)
                .stroke(color, style: StrokeStyle(lineWidth: 8, lineCap: .round))
                .rotationEffect(.degrees(-90))
            VStack(spacing: 2) {
                Text("\(used)%")
                    .font(.system(size: 22, weight: .semibold))
                    .monospacedDigit()
                Text(used >= 100 ? "Limit reached" : "\(left)% left")
                    .font(.system(size: 10))
                    .monospacedDigit()
                    .foregroundStyle(.tertiary)
            }
        }
        .frame(width: 96, height: 96)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(used >= 100 ? "Limit reached" : "\(used) percent used, \(left) percent left")
    }
}

struct UsageBar: View {
    let used: Int
    let color: Color

    private var fraction: Double { min(max(Double(used) / 100, 0), 1) }

    var body: some View {
        Capsule()
            .fill(Palette.track)
            .frame(height: 5)
            .overlay(alignment: .leading) {
                GeometryReader { proxy in
                    Capsule()
                        .fill(color)
                        .frame(width: proxy.size.width * fraction)
                }
            }
    }
}

// MARK: - Panel sections

struct HeaderView: View {
    let state: EngineState?
    let hasStaleWarning: Bool
    let isRefreshing: Bool
    let onRefresh: () -> Void

    private var statusText: String {
        guard let state else { return "Checking…" }
        switch state.mode {
        case "live": return "Live · updated \(state.updated ?? "just now")"
        case "degraded": return "Offline · last known from \(state.offlineSince ?? "earlier")"
        case "signin": return "Not signed in"
        default: return "Can't reach Anthropic"
        }
    }

    private var dotColor: Color {
        guard let state else { return Palette.stale }
        switch state.mode {
        case "live": return Palette.live
        case "degraded": return Palette.stale
        case "signin": return Palette.warn
        default: return Palette.hot
        }
    }

    var body: some View {
        HStack(spacing: 10) {
            ZStack {
                RoundedRectangle(cornerRadius: 8).fill(Color.black.opacity(0.85))
                Image(systemName: "gauge.with.needle")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(Palette.clay)
            }
            .frame(width: 28, height: 28)
            .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 2) {
                Text("Claude Code limits")
                    .font(.system(size: 13, weight: .semibold))
                HStack(spacing: 5) {
                    Circle().fill(dotColor).frame(width: 6, height: 6)
                    Text(statusText)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                }
                if hasStaleWarning {
                    Text("Couldn't update, showing last values")
                        .font(.system(size: 10.5))
                        .foregroundColor(Palette.warn)
                }
            }

            Spacer(minLength: 0)

            Button(action: onRefresh) {
                if isRefreshing {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: "arrow.clockwise")
                }
            }
            .buttonStyle(.borderless)
            .frame(width: 26, height: 26)
            .background(RoundedRectangle(cornerRadius: 6).fill(Palette.sectionFill))
            .disabled(isRefreshing)
            .help("Refresh now")
            .accessibilityLabel("Refresh")
        }
    }
}

struct MessageCard: View {
    let title: String
    let text: String

    var body: some View {
        SectionCard {
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.system(size: 12, weight: .semibold))
                Text(text).font(.system(size: 11)).foregroundStyle(.secondary)
            }
        }
    }
}

// One big donut and one short line, centred. Two of these sit side by side.
struct RingCard: View {
    let ring: Ring

    var body: some View {
        VStack(spacing: 8) {
            Text(ring.title)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.secondary)
                .lineLimit(1)
            RingGauge(used: ring.used, left: ring.left, color: Palette.color(for: ring.state))
            VStack(spacing: 1) {
                Text(ring.reset)
                    .font(.system(size: 12))
                    .foregroundStyle(.primary.opacity(0.85))
                if !ring.countdown.isEmpty {
                    Text(ring.countdown)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                }
            }
            .monospacedDigit()
            .lineLimit(1)
            .minimumScaleFactor(0.85)
        }
        .padding(.vertical, 10)
        .padding(.horizontal, 6)
        .frame(maxWidth: .infinity)
        .background(RoundedRectangle(cornerRadius: 10).fill(Palette.sectionFill))
    }
}

// Amber-tinted, in plain words: when the limit runs out and how early.
struct PaceCard: View {
    let pace: Pace

    // Built from Text pieces so only the time is bold.
    private var sentence: Text {
        Text("At this pace the \(pace.subject) runs out ")
            + Text(pace.when).bold().foregroundColor(.primary)
            + Text(", \(pace.lead) before it resets.")
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Image(systemName: "exclamationmark.circle")
                .foregroundColor(Palette.warn)
            sentence
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .font(.system(size: 11.5))
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10).fill(Palette.warn.opacity(0.12)))
    }
}

// "Where your week went": one split bar and a legend, in shades of neutral ink.
struct BreakdownCard: View {
    let breakdown: Breakdown

    private func shade(_ index: Int) -> Color {
        let opacities = [0.62, 0.38, 0.24, 0.14]
        return Color.primary.opacity(opacities[min(index, opacities.count - 1)])
    }

    private var summary: String {
        breakdown.rows.map { "\($0.name) \($0.percent) percent" }.joined(separator: ", ")
    }

    var body: some View {
        SectionCard {
            VStack(alignment: .leading, spacing: 7) {
                HStack {
                    Text("Where your week went")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.secondary)
                    Spacer()
                    if let since = breakdown.since {
                        Text("since \(since)")
                            .font(.system(size: 10.5))
                            .monospacedDigit()
                            .foregroundStyle(.tertiary)
                    }
                }
                GeometryReader { proxy in
                    HStack(spacing: 2) {
                        ForEach(Array(breakdown.rows.enumerated()), id: \.offset) { index, row in
                            Rectangle()
                                .fill(shade(index))
                                .frame(width: max(2, proxy.size.width * Double(row.percent) / 100))
                        }
                    }
                }
                .frame(height: 6)
                .background(Palette.track)
                .clipShape(Capsule())
                HStack(spacing: 12) {
                    ForEach(Array(breakdown.rows.enumerated()), id: \.offset) { index, row in
                        HStack(spacing: 5) {
                            RoundedRectangle(cornerRadius: 2)
                                .fill(shade(index))
                                .frame(width: 7, height: 7)
                            Text("\(row.name) \(row.percent)%")
                                .lineLimit(1)
                        }
                    }
                }
                .font(.system(size: 11))
                .monospacedDigit()
                .foregroundStyle(.secondary)
                .minimumScaleFactor(0.8)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Where your week went: \(summary)")
    }
}

struct LimitRowView: View {
    let limit: LimitRow

    private var color: Color { Palette.color(for: limit.state) }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(limit.name)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                Spacer()
                Text("\(limit.used)%")
                    .font(.system(size: 12, weight: .semibold))
                    .monospacedDigit()
            }
            UsageBar(used: limit.used, color: color)
            HStack {
                Text("\(limit.left)% left")
                Spacer()
                Text(limit.reset)
            }
            .font(.system(size: 10.5))
            .monospacedDigit()
            .foregroundStyle(.tertiary)
            if let pace = limit.pace {
                PaceCard(pace: pace)
            }
        }
    }
}

struct LimitCard: View {
    let limit: LimitRow

    var body: some View {
        SectionCard {
            LimitRowView(limit: limit)
        }
    }
}

// Sits above the rings so you see the effort level first. Amber once a
// high-effort run is long enough to be worth changing.
struct ActivityStrip: View {
    let row: ActivityRow

    // Built from Text pieces: markdown inside an interpolated String isn't parsed.
    private var detail: Text {
        let minutes = row.showsMinutes ? " for \(row.minutes)m" : ""
        return Text("\(row.effort.capitalized) effort").bold()
            + Text("\(minutes) · \(row.project)")
    }

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "bolt.fill")
                .foregroundColor(row.due ? Palette.warn : Color.primary.opacity(0.3))
            detail
                .font(.system(size: 11.5))
                .lineLimit(1)
                .layoutPriority(1)
            Spacer(minLength: 4)
            if row.due {
                Text("try /effort medium")
                    .font(.system(size: 10.5))
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
                    .fixedSize()
            } else if row.sessions > 1 {
                Text("\(row.sessions) sessions")
                    .font(.system(size: 10.5))
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
                    .fixedSize()
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .background(
            RoundedRectangle(cornerRadius: 10)
                .fill(row.due ? Palette.warn.opacity(0.12) : Palette.sectionFill)
        )
    }
}

struct SettingsCard: View {
    let settings: EngineSettings
    let hint: String
    let onChange: (SettingChange) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            SectionCard {
                VStack(alignment: .leading, spacing: 10) {
                    menubarRow
                    if !settings.alertChoices.isEmpty {
                        alertRow
                    }
                    toggleRow("Notifications", icon: "bell", isOn: settings.notifications) {
                        onChange(.notifications($0))
                    }
                    toggleRow("Launch at login", icon: "power", isOn: settings.launchAtLogin) {
                        onChange(.launchAtLogin($0))
                    }
                }
            }
            if !hint.isEmpty {
                Text(hint)
                    .font(.system(size: 10.5))
                    .foregroundStyle(.tertiary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 2)
            }
        }
    }

    private func rowLabel(_ title: String, icon: String) -> some View {
        HStack(spacing: 8) {
            Image(systemName: icon)
                .foregroundStyle(.secondary)
                .frame(width: 18)
            Text(title).font(.system(size: 13))
        }
    }

    private func toggleRow(
        _ title: String, icon: String, isOn: Bool, onSet: @escaping (Bool) -> Void
    ) -> some View {
        HStack {
            // The label side is a click target too, like Control Center. It stops
            // short of the switch so one click can't toggle twice; VoiceOver still
            // sees a single switch.
            HStack {
                rowLabel(title, icon: icon)
                Spacer()
            }
            .contentShape(Rectangle())
            .onTapGesture { onSet(!isOn) }
            .accessibilityHidden(true)
            Toggle(title, isOn: Binding(get: { isOn }, set: onSet))
                .labelsHidden()
                .toggleStyle(.switch)
                .controlSize(.small)
                .tint(Palette.clay)
        }
    }

    private var menubarRow: some View {
        let selection = Binding<MenuBarChoice>(
            get: { MenuBarChoice(rawValue: settings.menubar) ?? .both },
            set: { onChange(.menubar($0)) }
        )
        return HStack {
            rowLabel("Menu bar shows", icon: "menubar.rectangle")
            Spacer()
            Picker("Menu bar shows", selection: selection) {
                ForEach(MenuBarChoice.allCases, id: \.self) { choice in
                    Text(choice.title).tag(choice)
                }
            }
            .labelsHidden()
            .pickerStyle(.segmented)
            .fixedSize()
        }
    }

    private var alertRow: some View {
        let selection = Binding<Int>(
            get: { settings.alertFrom },
            set: { onChange(.minThreshold($0)) }
        )
        return HStack {
            rowLabel("Alert from", icon: "gauge.medium")
            Spacer()
            Picker("Alert from", selection: selection) {
                ForEach(settings.alertChoices, id: \.self) { percent in
                    Text("\(percent)%").tag(percent)
                }
            }
            .labelsHidden()
            .pickerStyle(.segmented)
            .fixedSize()
        }
    }
}

struct FooterView: View {
    var body: some View {
        HStack {
            Text("Unofficial · not affiliated with Anthropic")
                .font(.system(size: 10.5))
                .foregroundStyle(.tertiary)
            Spacer()
            Button {
                NSApplication.shared.terminate(nil)
            } label: {
                HStack(spacing: 4) {
                    Text("Quit")
                    Text("⌘Q").foregroundStyle(.tertiary)
                }
                .font(.system(size: 12))
            }
            .buttonStyle(.plain)
            .keyboardShortcut("q")
        }
        .padding(.horizontal, 2)
    }
}

// MARK: - Panel

struct PanelView: View {
    @ObservedObject var model: UsageModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HeaderView(
                state: model.state,
                hasStaleWarning: model.state != nil && model.lastError != nil,
                isRefreshing: model.isRefreshing,
                onRefresh: { model.refresh() }
            )
            content
            FooterView()
        }
        .padding(14)
        .frame(width: 340)
        .onAppear { model.refresh() }
    }

    @ViewBuilder
    private var content: some View {
        if let state = model.state {
            ForEach(Array(state.activity.enumerated()), id: \.offset) { _, row in
                ActivityStrip(row: row)
            }
            if !state.rings.isEmpty {
                HStack(spacing: 8) {
                    ForEach(Array(state.rings.prefix(2).enumerated()), id: \.offset) { _, ring in
                        RingCard(ring: ring)
                    }
                }
                ForEach(Array(state.rings.prefix(2).enumerated()), id: \.offset) { _, ring in
                    if let pace = ring.pace {
                        PaceCard(pace: pace)
                    }
                }
            } else if let message = state.message {
                MessageCard(title: message.title, text: message.body)
            }
            ForEach(Array(state.limits.enumerated()), id: \.offset) { _, limit in
                LimitCard(limit: limit)
            }
            if let breakdown = state.breakdown, !breakdown.rows.isEmpty {
                BreakdownCard(breakdown: breakdown)
            }
            SettingsCard(
                settings: state.settings,
                hint: state.notificationHint,
                onChange: { model.change($0) }
            )
        } else if let reason = model.lastError {
            MessageCard(title: "AiQuotaBar couldn't start its engine", text: reason)
        } else {
            MessageCard(title: "Loading", text: "Reading your usage…")
        }
    }
}

// MARK: - Menu bar label

// `Wk 88% · 5h 13%` with each number in its own colour, on a faint rounded
// plate. A menu bar label is drawn as a one-colour template, so coloured text
// has to go in as a non-template image. Its colours resolve when it draws,
// which follows the menu bar's light or dark look.
struct MenuBarLabel: View {
    let state: EngineState?
    let showsPlate: Bool

    var body: some View {
        if let state, !state.menubarParts.isEmpty {
            Image(nsImage: MenuBarImage.make(parts: state.menubarParts, suffix: state.menubarSuffix,
                                             showsPlate: showsPlate))
                .accessibilityLabel(state.menubarText)
        } else {
            Text(state?.menubarText ?? "◌ —")
        }
    }
}

enum MenuBarImage {
    static func make(parts: [MenubarPart], suffix: String, showsPlate: Bool) -> NSImage {
        let font = NSFont.monospacedDigitSystemFont(ofSize: NSFont.systemFontSize, weight: .regular)
        let text = NSMutableAttributedString()
        func append(_ string: String, _ color: NSColor) {
            text.append(NSAttributedString(string: string, attributes: [.font: font, .foregroundColor: color]))
        }
        for (index, part) in parts.enumerated() {
            if index > 0 { append(" · ", .labelColor) }
            append(part.label + " ", .labelColor)
            append(part.value, Palette.menuBarColor(for: part))
        }
        append(suffix, .labelColor)

        let finished = NSAttributedString(attributedString: text)
        let textSize = finished.size()
        // 3pt above and below makes the plate the full 22pt menu bar item height.
        let padding = NSSize(width: 6, height: 3)
        let size = NSSize(width: ceil(textSize.width) + padding.width * 2,
                          height: ceil(textSize.height) + padding.height * 2)
        let image = NSImage(size: size, flipped: false) { rect in
            if showsPlate {
                NSColor.labelColor.withAlphaComponent(0.12).setFill()
                NSBezierPath(roundedRect: rect, xRadius: 5, yRadius: 5).fill()
            }
            finished.draw(at: NSPoint(x: padding.width, y: padding.height))
            return true
        }
        image.isTemplate = false
        return image
    }
}

// MARK: - App entry

// MenuBarExtra has no right-click menu of its own, so a right click on the
// menu bar item is caught here and answered with a small Quit menu.
final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    private var rightClickMonitor: Any?

    func applicationDidFinishLaunching(_ notification: Notification) {
        UNUserNotificationCenter.current().delegate = self
        Notifier.requestPermission()
        rightClickMonitor = NSEvent.addLocalMonitorForEvents(matching: .rightMouseDown) { event in
            guard let window = event.window,
                  String(describing: type(of: window)).contains("StatusBar"),
                  let view = window.contentView else {
                return event
            }
            let menu = NSMenu()
            menu.addItem(withTitle: "Quit AiQuotaBar",
                         action: #selector(NSApplication.terminate(_:)),
                         keyEquivalent: "q")
            menu.popUp(positioning: nil, at: NSPoint(x: 0, y: view.bounds.minY - 4), in: view)
            return nil
        }
    }

    // macOS hides banners from the frontmost app by default, and the app counts as
    // frontmost while its panel is open. Show them anyway.
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list])
    }
}

struct AiQuotaBarApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var model = UsageModel()

    var body: some Scene {
        MenuBarExtra {
            PanelView(model: model)
        } label: {
            MenuBarLabel(state: model.state, showsPlate: !model.isPanelOpen)
        }
        .menuBarExtraStyle(.window)
    }
}

@main
struct AiQuotaBarLauncher {
    @MainActor
    static func main() {
        let arguments = CommandLine.arguments
        if let flagIndex = arguments.firstIndex(of: "--selftest") {
            let pathIndex = flagIndex + 1
            guard pathIndex < arguments.count else {
                print("selftest failed: missing path")
                exit(1)
            }
            exit(runSelfTest(path: arguments[pathIndex]))
        }
        AiQuotaBarApp.main()
    }

    // Decodes a saved engine reply with the app's own decoder, without starting the UI.
    private static func runSelfTest(path: String) -> Int32 {
        guard let data = FileManager.default.contents(atPath: path) else {
            print("selftest failed: cannot read file")
            return 1
        }
        guard let state = try? JSONDecoder().decode(EngineState.self, from: data) else {
            print("selftest failed: cannot decode JSON")
            return 1
        }
        print("selftest ok: mode=\(state.mode) rings=\(state.rings.count) limits=\(state.limits.count) "
              + "parts=\(state.menubarParts.count) breakdown=\(state.breakdown?.rows.count ?? 0) "
              + "alerts=\(state.alerts.count)")
        return 0
    }
}
