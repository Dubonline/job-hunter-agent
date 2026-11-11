// Chrome 原生授权辅助进程：读取浏览器原生焦点窗口，跳过网页，按实时AX位置发送单击。
import AppKit
import ApplicationServices

func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
}

func string(_ element: AXUIElement, _ name: String) -> String {
    return attribute(element, name) as? String ?? ""
}

// 从单个AX属性读取窗口对象；类型不匹配或接口不可读时返回nil，不猜坐标。
func elementAttribute(_ element: AXUIElement, _ name: String) -> AXUIElement? {
    guard let value = attribute(element, name), CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
    return (value as! AXUIElement)
}

// 返回Chrome当前原生窗口：焦点窗口可读时只检查焦点，不混入历史窗口对象。
// 全屏授权弹框可能直接成为焦点窗口，因此不以AXWindows列表为空就放弃。
func chromeWindows(_ app: AXUIElement) -> [AXUIElement] {
    if let focused = elementAttribute(app, kAXFocusedWindowAttribute) { return [focused] }
    if let main = elementAttribute(app, kAXMainWindowAttribute) { return [main] }
    return attribute(app, kAXWindowsAttribute) as? [AXUIElement] ?? []
}

// 每个窗口最多读取256个原生节点、12层；不进入网页AXWebArea，防止误点站内按钮。
func nodes(_ root: AXUIElement) -> [AXUIElement] {
    var result: [AXUIElement] = []
    var pending: [(AXUIElement, Int)] = [(root, 0)]
    while let (element, depth) = pending.popLast(), result.count < 256 {
        if string(element, kAXRoleAttribute) == "AXWebArea" { continue }
        result.append(element)
        if depth < 12 {
            let children = attribute(element, kAXChildrenAttribute) as? [AXUIElement] ?? []
            pending += children.prefix(256).map { ($0, depth + 1) }
        }
    }
    return result
}

// 三重文案与唯一按钮共同确认；语言未知/结构不符不点击。
func matches(_ texts: [String], _ buttons: [String]) -> Bool {
    let joined = texts.joined(separator: " ")
    let chinese = texts.contains("要允许远程调试吗？") && joined.contains("外部应用") && joined.contains("Cookie")
    let english = texts.contains("Allow remote debugging?") && joined.contains("Chrome") && joined.lowercased().contains("cookies")
    return (chinese && buttons.filter { $0 == "允许" }.count == 1 && buttons.contains("取消")) ||
        (english && buttons.filter { $0 == "Allow" }.count == 1 && buttons.contains("Cancel"))
}

// 仅返回通过完整原生文案核对的唯一允许按钮；不返回其他窗口按钮。
func allowButton(_ window: AXUIElement) -> AXUIElement? {
    let items = nodes(window)
    let texts = items.flatMap { [string($0, kAXTitleAttribute), string($0, kAXValueAttribute), string($0, kAXDescriptionAttribute)] }
    let buttons = items.filter { string($0, kAXRoleAttribute) == kAXButtonRole }
    guard matches(texts, buttons.map { string($0, kAXTitleAttribute) }) else { return nil }
    return buttons.first { ["允许", "Allow"].contains(string($0, kAXTitleAttribute)) }
}

// 按实时AX位置点击已核验的允许按钮；只在Chrome前台且命中同一按钮时发送一次鼠标点击。
// 使用现有事件权限，不请求新权限、不激活窗口、不使用固定坐标。
func clickAllow(_ button: AXUIElement, _ pid: pid_t) -> Bool {
    // Chromium敏感对话框有短时输入保护；等待显示稳定后再进行唯一点击，不关闭保护或重试。
    RunLoop.current.run(until: Date().addingTimeInterval(2.0))
    guard getppid() == owner, owner != 1,
          NSWorkspace.shared.frontmostApplication?.processIdentifier == pid,
          CGPreflightPostEventAccess(),
          (attribute(button, kAXEnabledAttribute) as? Bool) == true,
          let rawPosition = attribute(button, kAXPositionAttribute),
          let rawSize = attribute(button, kAXSizeAttribute),
          CFGetTypeID(rawPosition) == AXValueGetTypeID(),
          CFGetTypeID(rawSize) == AXValueGetTypeID() else { return false }
    var position = CGPoint.zero
    var size = CGSize.zero
    guard AXValueGetValue(rawPosition as! AXValue, .cgPoint, &position),
          AXValueGetValue(rawSize as! AXValue, .cgSize, &size),
          size.width > 0, size.height > 0 else { return false }
    let center = CGPoint(x: position.x + size.width / 2, y: position.y + size.height / 2)
    var hit: AXUIElement?
    guard AXUIElementCopyElementAtPosition(AXUIElementCreateSystemWide(), Float(center.x), Float(center.y), &hit) == .success,
          let target = hit, CFEqual(target, button),
          let source = CGEventSource(stateID: .hidSystemState),
          let move = CGEvent(mouseEventSource: source, mouseType: .mouseMoved, mouseCursorPosition: center, mouseButton: .left),
          let down = CGEvent(mouseEventSource: source, mouseType: .leftMouseDown, mouseCursorPosition: center, mouseButton: .left),
          let up = CGEvent(mouseEventSource: source, mouseType: .leftMouseUp, mouseCursorPosition: center, mouseButton: .left) else { return false }
    // 经过WindowServer正常路由，先确认鼠标事件确实到达按钮处，避免只向PID发事件却未命中窗口。
    move.post(tap: .cghidEventTap)
    RunLoop.current.run(until: Date().addingTimeInterval(0.05))
    guard let cursor = CGEvent(source: nil)?.location,
          abs(cursor.x - center.x) <= 1, abs(cursor.y - center.y) <= 1,
          getppid() == owner,
          NSWorkspace.shared.frontmostApplication?.processIdentifier == pid,
          let current = chromeWindows(AXUIElementCreateApplication(pid)).first,
          let currentButton = allowButton(current), CFEqual(currentButton, button) else {
        emit("CURSOR_OR_DIALOG_CHANGED")
        return false
    }
    emit("ALLOW_INTENT")
    down.setIntegerValueField(.mouseEventClickState, value: 1)
    up.setIntegerValueField(.mouseEventClickState, value: 1)
    down.setIntegerValueField(.mouseEventNumber, value: 1)
    up.setIntegerValueField(.mouseEventNumber, value: 1)
    down.post(tap: .cghidEventTap)
    RunLoop.current.run(until: Date().addingTimeInterval(0.04))
    up.post(tap: .cghidEventTap)
    return true
}

func emit(_ event: String, _ milliseconds: Double = 0) {
    print("{\"event\":\"\(event)\",\"milliseconds\":\(Int(milliseconds))}")
    fflush(stdout)
}

if CommandLine.arguments.contains("--self-test") {
    assert(matches(["要允许远程调试吗？", "外部应用请求控制Chrome，包括Cookie"], ["允许", "取消"]))
    assert(!matches(["网页权限", "外部应用 Cookie"], ["允许", "取消"]))
    assert(!matches(["要允许远程调试吗？", "外部应用 Cookie"], ["允许", "允许", "取消"]))
    assert(!matches(["要允许远程调试吗？"], ["允许", "取消"]))
    emit("SELF_TEST_OK")
    exit(0)
}

// 仅检查现有权限，不弹系统授权、不修改TCC或Chrome辅助功能状态。
let trusted = AXIsProcessTrusted()
guard trusted else { emit("ACCESSIBILITY_REQUIRED"); exit(3) }
// 单次只读核验：仅输出Chrome窗口数/AX返回码，不输出窗口标题或网页内容。
// 读不到窗口则退出5，不触发连接、不点击，也不请求任何新权限。
if CommandLine.arguments.contains("--probe-windows") {
    let applications = NSRunningApplication.runningApplications(withBundleIdentifier: "com.google.Chrome")
    var count = 0
    for chrome in applications {
        let element = AXUIElementCreateApplication(chrome.processIdentifier)
        AXUIElementSetMessagingTimeout(element, 0.3)
        var value: CFTypeRef?
        let result = AXUIElementCopyAttributeValue(element, kAXWindowsAttribute as CFString, &value)
        let windows = value as? [AXUIElement] ?? []
        let candidates = chromeWindows(element)
        count += candidates.count
        print("{\"event\":\"WINDOW_PROBE\",\"ax_code\":\(result.rawValue),\"windows\":\(windows.count),\"focused\":\(elementAttribute(element, kAXFocusedWindowAttribute) != nil),\"main\":\(elementAttribute(element, kAXMainWindowAttribute) != nil),\"candidates\":\(candidates.count),\"authorization_dialog\":\(candidates.contains { allowButton($0) != nil })}")
        if let window = candidates.first, let button = allowButton(window) {
            print("{\"event\":\"BUTTON_PROBE\",\"enabled\":\((attribute(button, kAXEnabledAttribute) as? Bool) == true),\"position_readable\":\(attribute(button, kAXPositionAttribute) != nil),\"size_readable\":\(attribute(button, kAXSizeAttribute) != nil),\"chrome_frontmost\":\(NSWorkspace.shared.frontmostApplication?.processIdentifier == chrome.processIdentifier),\"event_access\":\(CGPreflightPostEventAccess())}")

        }
    }
    print("{\"event\":\"WINDOW_PROBE_DONE\",\"applications\":\(applications.count),\"windows\":\(count)}")
    exit(count > 0 ? 0 : 5)
}
if CommandLine.arguments.contains("--check") { emit("READY"); exit(0) }
let owner = getppid()
emit("READY")
var attempted: [AXUIElement] = []
var attemptCount = 0 // 每个辅助进程最多128次授权点击；不是投递预算，达到上限立即退出。
while getppid() == owner && owner != 1 {
    let applicationPIDs = NSRunningApplication.runningApplications(withBundleIdentifier: "com.google.Chrome").map { $0.processIdentifier }
    for pid in applicationPIDs {
        let app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, 0.3)
        let windows = chromeWindows(app)
        for window in windows {
            guard let button = allowButton(window) else { continue }
            if attempted.contains(where: { CFEqual($0, button) }) { continue }
            // 先记尝试，AX超时也不重复点击同一按钮。后续新弹框仍可处理。
            attempted.append(button)
            attemptCount += 1
            if attemptCount > 128 { emit("ATTEMPT_LIMIT"); exit(4) }
            let started = Date()
            guard clickAllow(button, pid) else { emit("ALLOW_NOT_CLICKED"); exit(4) }
            emit("ALLOW_CLICK_SENT", Date().timeIntervalSince(started) * 1000)
            // 点击后必须重新定位当前窗口；Chrome旧AX对象可能保留已关闭弹框快照。
            // 当前窗口也不可读不能当作弹框消失，不读取网页或重试点击。
            var disappeared = false
            var replacementDialog = false
            for _ in 0..<20 {
                RunLoop.current.run(until: Date().addingTimeInterval(0.05))
                let current = chromeWindows(AXUIElementCreateApplication(pid))
                if let focused = current.first, !CFEqual(focused, window), allowButton(focused) != nil {
                    replacementDialog = true
                }
                if !current.isEmpty && !current.contains(where: { allowButton($0) != nil }) {
                    disappeared = true
                    break
                }
            }
            if replacementDialog { emit("OTHER_AUTHORIZATION_DIALOG_VISIBLE") }
            emit(disappeared ? "DIALOG_DISMISSED" : "DIALOG_UNVERIFIED", Date().timeIntervalSince(started) * 1000)
            if !disappeared { exit(4) }
            // 已确认消失后释放对象；Chrome复用AX对象时，后续新弹框仍应可处理。
            attempted.removeAll { CFEqual($0, button) }
        }
    }
    RunLoop.current.run(until: Date().addingTimeInterval(0.2)) // 轮询间隔200毫秒，处理本机AX事件，无模型/网络请求。
}
