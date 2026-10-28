import AppKit
import ApplicationServices
import CoreGraphics
import Darwin
import Foundation
import ScreenCaptureKit

struct CuaFailure: Error {
    let message: String
    let text: String?
    init(_ message: String, text: String? = nil) {
        self.message = message
        self.text = text
    }
}

var sessionPid: pid_t = 0
var sessionApp = ""
var elements: [AXUIElement] = []
var windowRect = CGRect.zero
var imageWidth = 0
var imageHeight = 0

let keyCodes: [String: CGKeyCode] = [
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13,
    "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38,
    "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50,
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51, "escape": 53, "esc": 53,
    "forwarddelete": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109,
    "f11": 103, "f12": 111]
let modifiers: [String: CGEventFlags] = ["cmd": .maskCommand, "command": .maskCommand, "shift": .maskShift,
                                         "alt": .maskAlternate, "option": .maskAlternate, "ctrl": .maskControl,
                                         "control": .maskControl, "fn": .maskSecondaryFn]

func emit(_ object: [String: Any]) {
    guard JSONSerialization.isValidJSONObject(object),
          let data = try? JSONSerialization.data(withJSONObject: object) else { return }
    var line = data
    line.append(0x0A)
    line.withUnsafeBytes { raw in
        if let base = raw.baseAddress, raw.count > 0 {
            _ = Darwin.write(STDOUT_FILENO, base, raw.count)
        }
    }
}

func jsonDouble(_ object: [String: Any], _ key: String) -> Double? {
    guard let value = object[key], !(value is NSNull) else { return nil }
    return (value as? NSNumber)?.doubleValue
}

func jsonInt(_ object: [String: Any], _ key: String) -> Int? {
    guard let value = jsonDouble(object, key) else { return nil }
    return Int(value)
}

func jsonString(_ object: [String: Any], _ key: String) -> String? {
    object[key] as? String
}

func permissionError(_ kind: String, _ pane: String) -> String {
    let binary = Bundle.main.executablePath ?? "TesseraCUA"
    return "TesseraCUA: \(kind) is not granted to \(binary). System Settings > Privacy & Security > \(pane), enable TesseraCUA."
}

func requireAccessibility() throws {
    if !AXIsProcessTrusted() {
        throw CuaFailure(permissionError("Accessibility", "Accessibility"))
    }
}

func oneLine(_ text: String, limit: Int = 200) -> String {
    let flat = text.replacingOccurrences(of: "\n", with: " ").replacingOccurrences(of: "\t", with: " ")
    if flat.count <= limit { return flat.isEmpty ? "-" : flat }
    return String(flat.prefix(limit))
}

func axCopy(_ element: AXUIElement, _ attribute: String) -> CFTypeRef? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success else { return nil }
    return value
}

func axString(_ element: AXUIElement, _ attribute: String) -> String {
    axCopy(element, attribute) as? String ?? ""
}

func axChildren(_ element: AXUIElement) -> [AXUIElement] {
    axCopy(element, kAXChildrenAttribute as String) as? [AXUIElement] ?? []
}

func axFrame(_ element: AXUIElement) -> CGRect {
    var point = CGPoint.zero
    var size = CGSize.zero
    if let position = axCopy(element, kAXPositionAttribute as String), CFGetTypeID(position) == AXValueGetTypeID() {
        AXValueGetValue(position as! AXValue, .cgPoint, &point)
    }
    if let box = axCopy(element, kAXSizeAttribute as String), CFGetTypeID(box) == AXValueGetTypeID() {
        AXValueGetValue(box as! AXValue, .cgSize, &size)
    }
    return CGRect(origin: point, size: size)
}

func axName(_ element: AXUIElement) -> String {
    for attribute in [kAXTitleAttribute, kAXDescriptionAttribute, kAXHelpAttribute] {
        let text = axString(element, attribute as String)
        if !text.isEmpty { return text }
    }
    return ""
}

func axActions(_ element: AXUIElement) -> [String] {
    var names: CFArray?
    guard AXUIElementCopyActionNames(element, &names) == .success, let names else { return [] }
    return names as? [String] ?? []
}

func buildTree(_ root: AXUIElement) -> (text: String, truncated: Bool) {
    elements = []
    var lines: [String] = []
    var truncated = false
    func walk(_ element: AXUIElement, _ depth: Int) {
        if elements.count >= 400 || depth > 12 {
            truncated = true
            return
        }
        let frame = axFrame(element)
        let index = elements.count
        elements.append(element)
        let actions = axActions(element).joined(separator: ",")
        let box = "\(Int(frame.origin.x)),\(Int(frame.origin.y)),\(Int(frame.width)),\(Int(frame.height))"
        lines.append("\(index) \(oneLine(axString(element, kAXRoleAttribute as String), limit: 80)) "
            + "\(oneLine(axString(element, kAXSubroleAttribute as String))) "
            + "\(oneLine(axName(element))) "
            + "\(oneLine(axString(element, kAXValueAttribute as String))) \(box) \(actions.isEmpty ? "-" : actions)")
        if depth == 12 {
            if !axChildren(element).isEmpty { truncated = true }
            return
        }
        for child in axChildren(element) {
            walk(child, depth + 1)
        }
    }
    walk(root, 0)
    if truncated { lines.append("truncated") }
    return (lines.joined(separator: "\n"), truncated)
}

func runningRegular() -> [NSRunningApplication] {
    NSWorkspace.shared.runningApplications.filter { $0.activationPolicy == .regular }
}

func matchApp(_ name: String) -> NSRunningApplication? {
    let wanted = name.lowercased()
    return runningRegular().first { app in
        (app.localizedName ?? "").lowercased() == wanted || (app.bundleIdentifier ?? "").lowercased() == wanted
    }
}

func windowBounds(_ item: [String: Any]) -> CGRect? {
    guard let bounds = item[kCGWindowBounds as String] as? NSDictionary else { return nil }
    let x = (bounds["X"] as? NSNumber)?.doubleValue ?? 0
    let y = (bounds["Y"] as? NSNumber)?.doubleValue ?? 0
    let width = (bounds["Width"] as? NSNumber)?.doubleValue ?? 0
    let height = (bounds["Height"] as? NSNumber)?.doubleValue ?? 0
    if width < 1 || height < 1 { return nil }
    return CGRect(x: x, y: y, width: width, height: height)
}

func frontWindow(_ pid: pid_t) -> (id: CGWindowID, bounds: CGRect)? {
    guard let info = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else {
        return nil
    }
    var best: (CGWindowID, CGRect, CGFloat)?
    for item in info {
        let owner = (item[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value ?? -1
        let layer = (item[kCGWindowLayer as String] as? NSNumber)?.intValue ?? 1
        if owner != pid || layer != 0 { continue }
        guard let bounds = windowBounds(item) else { continue }
        let number = (item[kCGWindowNumber as String] as? NSNumber)?.uint32Value ?? 0
        let area = bounds.width * bounds.height
        if best == nil || area > best!.2 {
            best = (number, bounds, area)
        }
    }
    guard let best else { return nil }
    return (best.0, best.1)
}

final class CaptureWaiter: @unchecked Sendable {
    private let lock = NSLock()
    private let semaphore = DispatchSemaphore(value: 0)
    private var image: CGImage?
    private var error: Error?

    func finish(image: CGImage?, error: Error?) {
        lock.lock()
        self.image = image
        self.error = error
        lock.unlock()
        semaphore.signal()
    }

    func wait() throws -> CGImage {
        semaphore.wait()
        lock.lock()
        defer { lock.unlock() }
        if let error { throw error }
        if let image { return image }
        throw CuaFailure("TesseraCUA: could not capture the window")
    }
}

func screenScale(_ frame: CGRect) -> CGFloat {
    let screen = NSScreen.screens.first { $0.frame.intersects(frame) } ?? NSScreen.main
    let scale = screen?.backingScaleFactor ?? 1
    return scale > 0 ? scale : 1
}

func shareableWindow(_ windowId: CGWindowID) async throws -> SCWindow {
    for onScreenOnly in [true, false] {
        let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: onScreenOnly)
        if let window = content.windows.first(where: { $0.windowID == windowId }) {
            return window
        }
    }
    throw CuaFailure("TesseraCUA: the window is no longer available to capture")
}

func screenshot(_ windowId: CGWindowID) async throws -> CGImage {
    let window = try await shareableWindow(windowId)
    let filter = SCContentFilter(desktopIndependentWindow: window)
    let configuration = SCStreamConfiguration()
    configuration.showsCursor = false
    let scale = screenScale(window.frame)
    configuration.width = max(1, Int((window.frame.width * scale).rounded()))
    configuration.height = max(1, Int((window.frame.height * scale).rounded()))
    return try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: configuration)
}

func isScreenRecordingDenial(_ error: Error) -> Bool {
    if !CGPreflightScreenCaptureAccess() { return true }
    let ns = error as NSError
    let text = ns.localizedDescription.lowercased()
    if text.contains("screen recording") || text.contains("not authorized") || text.contains("declined") {
        return true
    }
    return ns.domain.contains("ScreenCaptureKit") && ns.code == -3801
}

func captureFailure(_ error: Error) -> CuaFailure {
    if let failure = error as? CuaFailure { return failure }
    if isScreenRecordingDenial(error) {
        return CuaFailure(permissionError("Screen Recording", "Screen & System Audio Recording"))
    }
    return CuaFailure("TesseraCUA: could not capture the window (\((error as NSError).localizedDescription))")
}

func captureWindow(_ windowId: CGWindowID) throws -> CGImage {
    if !CGPreflightScreenCaptureAccess() {
        throw CuaFailure(permissionError("Screen Recording", "Screen & System Audio Recording"))
    }
    let waiter = CaptureWaiter()
    Task.detached {
        do {
            waiter.finish(image: try await screenshot(windowId), error: nil)
        } catch {
            waiter.finish(image: nil, error: error)
        }
    }
    do {
        return try waiter.wait()
    } catch {
        throw captureFailure(error)
    }
}

func screenshotDesktop(_ displayId: CGDirectDisplayID) async throws -> CGImage {
    let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: true)
    guard let display = content.displays.first(where: { $0.displayID == displayId }) ?? content.displays.first else {
        throw CuaFailure("TesseraCUA: no display is available to capture")
    }
    // A whole-display filter shows every window on that screen, which is what a
    // person means by "a screenshot of the desktop". The per-app capture stays in
    // get_app_state, where it keeps the cursor where it was.
    let filter = SCContentFilter(display: display, excludingWindows: [])
    let configuration = SCStreamConfiguration()
    configuration.showsCursor = false
    configuration.width = max(1, Int(CGDisplayPixelsWide(display.displayID)))
    configuration.height = max(1, Int(CGDisplayPixelsHigh(display.displayID)))
    return try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: configuration)
}

func captureDesktop(_ displayId: CGDirectDisplayID) throws -> CGImage {
    if !CGPreflightScreenCaptureAccess() {
        throw CuaFailure(permissionError("Screen Recording", "Screen & System Audio Recording"))
    }
    let waiter = CaptureWaiter()
    Task.detached {
        do {
            waiter.finish(image: try await screenshotDesktop(displayId), error: nil)
        } catch {
            waiter.finish(image: nil, error: error)
        }
    }
    do {
        return try waiter.wait()
    } catch {
        throw captureFailure(error)
    }
}

func scaledPNG(_ image: CGImage, maxPx: Int) throws -> (data: Data, width: Int, height: Int) {
    var cg = image
    if maxPx > 0 {
        let longest = max(cg.width, cg.height)
        if longest > maxPx {
            let scale = Double(maxPx) / Double(longest)
            let width = max(1, Int((Double(cg.width) * scale).rounded()))
            let height = max(1, Int((Double(cg.height) * scale).rounded()))
            let color = CGColorSpaceCreateDeviceRGB()
            let info = CGImageAlphaInfo.premultipliedLast.rawValue
            if let context = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8,
                                       bytesPerRow: 0, space: color, bitmapInfo: info) {
                context.interpolationQuality = .high
                context.draw(cg, in: CGRect(x: 0, y: 0, width: width, height: height))
                if let scaled = context.makeImage() { cg = scaled }
            }
        }
    }
    guard let data = NSBitmapImageRep(cgImage: cg).representation(using: .png, properties: [:]) else {
        throw CuaFailure("TesseraCUA: could not encode the window screenshot")
    }
    return (data, cg.width, cg.height)
}

func eventPoint(topX: Double, topY: Double) -> CGPoint {
    let screen = CGDisplayBounds(CGMainDisplayID())
    return CGPoint(x: topX, y: screen.height - topY)
}

func eventPoint(imageX: Double, imageY: Double) throws -> CGPoint {
    if imageWidth <= 0 || imageHeight <= 0 || windowRect.width <= 0 || windowRect.height <= 0 {
        throw CuaFailure("TesseraCUA: no window bounds; call get_app_state first")
    }
    let pointX = imageX * Double(windowRect.width) / Double(imageWidth)
    let pointY = imageY * Double(windowRect.height) / Double(imageHeight)
    return eventPoint(topX: Double(windowRect.origin.x) + pointX, topY: Double(windowRect.origin.y) + pointY)
}

func eventPoint(element: AXUIElement) throws -> CGPoint {
    let frame = axFrame(element)
    if frame.width <= 0 || frame.height <= 0 {
        throw CuaFailure("TesseraCUA: element has no frame; pass x and y from the screenshot")
    }
    return eventPoint(topX: Double(frame.midX), topY: Double(frame.midY))
}

func elementIfPresent(_ request: [String: Any], _ key: String) -> AXUIElement? {
    guard let index = jsonInt(request, key), elements.indices.contains(index) else { return nil }
    return elements[index]
}

func elementAt(_ request: [String: Any], _ key: String) throws -> AXUIElement? {
    guard jsonInt(request, key) != nil else { return nil }
    if let element = elementIfPresent(request, key) { return element }
    if elements.isEmpty {
        throw CuaFailure("TesseraCUA: no accessibility tree; call get_app_state first")
    }
    throw CuaFailure("TesseraCUA: element index is not in the last accessibility tree; call get_app_state again")
}

func useApp(_ name: String?) throws {
    guard let name, !name.isEmpty else {
        if sessionPid == 0 {
            throw CuaFailure("TesseraCUA: no target app; call get_app_state first")
        }
        return
    }
    guard let app = matchApp(name) else {
        throw CuaFailure("TesseraCUA: no running app named \(name)")
    }
    let pid = app.processIdentifier
    if pid != sessionPid {
        sessionPid = pid
        sessionApp = app.localizedName ?? name
        elements = []
        windowRect = .zero
        imageWidth = 0
        imageHeight = 0
    }
}

func postToPid(_ event: CGEvent?) throws {
    guard let event else { throw CuaFailure("TesseraCUA: could not build an input event") }
    guard sessionPid != 0 else { throw CuaFailure("TesseraCUA: no target app; call get_app_state first") }
    event.postToPid(sessionPid)
    Thread.sleep(forTimeInterval: 0.02)
}

func pointForAction(_ request: [String: Any], elementKey: String, xKey: String, yKey: String) throws -> CGPoint? {
    if let element = elementIfPresent(request, elementKey), axFrame(element).width > 0, axFrame(element).height > 0 {
        return try eventPoint(element: element)
    }
    if let x = jsonDouble(request, xKey), let y = jsonDouble(request, yKey) {
        return try eventPoint(imageX: x, imageY: y)
    }
    return nil
}

func mouse(down: CGEventType, up: CGEventType, button: CGMouseButton, at point: CGPoint, dragged: CGEventType? = nil, to: CGPoint? = nil) throws {
    let source: CGEventSource? = nil
    try postToPid(CGEvent(mouseEventSource: source, mouseType: down, mouseCursorPosition: point, mouseButton: button))
    if let dragged, let to {
        try postToPid(CGEvent(mouseEventSource: source, mouseType: dragged, mouseCursorPosition: to, mouseButton: button))
        try postToPid(CGEvent(mouseEventSource: source, mouseType: up, mouseCursorPosition: to, mouseButton: button))
    } else {
        try postToPid(CGEvent(mouseEventSource: source, mouseType: up, mouseCursorPosition: point, mouseButton: button))
    }
}

func listApps() -> [String: Any] {
    let apps: [[String: Any]] = runningRegular().map { app in
        ["name": app.localizedName ?? "", "pid": Int(app.processIdentifier), "bundle_id": app.bundleIdentifier ?? ""]
    }
    return ["apps": apps, "text": "list_apps ok"]
}

func getAppState(_ request: [String: Any]) throws -> [String: Any] {
    guard let name = jsonString(request, "app"), !name.isEmpty else {
        throw CuaFailure("TesseraCUA: get_app_state needs an app name")
    }
    guard let app = matchApp(name) else {
        throw CuaFailure("TesseraCUA: no running app named \(name)")
    }
    try requireAccessibility()
    sessionPid = app.processIdentifier
    sessionApp = app.localizedName ?? name
    let tree = buildTree(AXUIElementCreateApplication(sessionPid))
    var response: [String: Any] = ["text": tree.text, "pid": Int(sessionPid)]
    guard let window = frontWindow(sessionPid) else {
        windowRect = .zero
        imageWidth = 0
        imageHeight = 0
        response["text"] = tree.text + "\nno on-screen window"
        return response
    }
    windowRect = window.bounds
    let image: CGImage
    do {
        image = try captureWindow(window.id)
    } catch let failure as CuaFailure {
        throw CuaFailure(failure.message, text: failure.text ?? tree.text)
    }
    let encoded = try scaledPNG(image, maxPx: jsonInt(request, "max_px") ?? 1600)
    imageWidth = encoded.width
    imageHeight = encoded.height
    response["image"] = ["mimeType": "image/png", "data": encoded.data.base64EncodedString()]
    return response
}

func screenshot(_ request: [String: Any]) throws -> [String: Any] {
    // Both capture shapes exist. One-app capture stays in get_app_state, and a
    // whole-desktop shot is this one. Screen Recording is already granted to this
    // helper; it only needs re-granting if the capture path changes, so a refusal
    // says that plainly instead of asking again.
    var displayId = CGMainDisplayID()
    if let asked = jsonInt(request, "display"), asked > 0 {
        displayId = CGDirectDisplayID(asked)
    }
    let image = try captureDesktop(displayId)
    let encoded = try scaledPNG(image, maxPx: jsonInt(request, "max_px") ?? 1600)
    let bounds = CGDisplayBounds(displayId)
    return ["text": "screenshot ok (\(encoded.width)x\(encoded.height))",
            "display": Int(displayId),
            "bounds": ["x": Double(bounds.origin.x), "y": Double(bounds.origin.y),
                       "width": Double(bounds.width), "height": Double(bounds.height)],
            "image": ["mimeType": "image/png", "data": encoded.data.base64EncodedString()]]
}

func click(_ request: [String: Any]) throws -> [String: Any] {
    try requireAccessibility()
    try useApp(jsonString(request, "app"))
    if let element = elementIfPresent(request, "element"),
       AXUIElementPerformAction(element, kAXPressAction as CFString) == .success {
        return ["text": "click ok"]
    }
    guard let x = jsonDouble(request, "x"), let y = jsonDouble(request, "y") else {
        if jsonInt(request, "element") != nil {
            throw CuaFailure("TesseraCUA: AXPress failed")
        }
        throw CuaFailure("TesseraCUA: click needs an element index or x,y")
    }
    let at = try eventPoint(imageX: x, imageY: y)
    try mouse(down: .leftMouseDown, up: .leftMouseUp, button: .left, at: at)
    return ["text": "click ok"]
}

func secondary(_ request: [String: Any]) throws -> [String: Any] {
    try requireAccessibility()
    try useApp(jsonString(request, "app"))
    if let element = elementIfPresent(request, "element"),
       AXUIElementPerformAction(element, kAXShowMenuAction as CFString) == .success {
        return ["text": "perform_secondary_action ok"]
    }
    guard let at = try pointForAction(request, elementKey: "element", xKey: "x", yKey: "y") else {
        throw CuaFailure("TesseraCUA: secondary action needs an element index or x,y")
    }
    try mouse(down: .rightMouseDown, up: .rightMouseUp, button: .right, at: at)
    return ["text": "perform_secondary_action ok"]
}

func setValue(_ request: [String: Any]) throws -> [String: Any] {
    try requireAccessibility()
    try useApp(jsonString(request, "app"))
    guard let element = try elementAt(request, "element") else {
        throw CuaFailure("TesseraCUA: set_value needs an element index")
    }
    let value = jsonString(request, "value") ?? ""
    if AXUIElementSetAttributeValue(element, kAXValueAttribute as CFString, value as CFString) != .success {
        throw CuaFailure("TesseraCUA: AXValue failed")
    }
    return ["text": "set_value ok"]
}

func selectText(_ request: [String: Any]) throws -> [String: Any] {
    try requireAccessibility()
    try useApp(jsonString(request, "app"))
    guard let element = try elementAt(request, "element") else {
        throw CuaFailure("TesseraCUA: select_text needs an element index")
    }
    let length = (axString(element, kAXValueAttribute as String) as NSString).length
    var range = CFRange(location: 0, length: length)
    guard let axRange = AXValueCreate(.cfRange, &range) else {
        throw CuaFailure("TesseraCUA: could not build a text range")
    }
    if AXUIElementSetAttributeValue(element, kAXSelectedTextRangeAttribute as CFString, axRange) != .success {
        throw CuaFailure("TesseraCUA: could not select text")
    }
    return ["text": "select_text ok"]
}

func adjustScrollBar(_ bar: AXUIElement, dy: Double) -> Bool {
    guard let current = axCopy(bar, kAXValueAttribute as String) as? NSNumber else { return false }
    let next = min(1, max(0, current.doubleValue - dy * 0.1))
    return AXUIElementSetAttributeValue(bar, kAXValueAttribute as CFString, NSNumber(value: next)) == .success
}

func adjustScroll(_ element: AXUIElement, dy: Double, depth: Int = 0) -> Bool {
    let role = axString(element, kAXRoleAttribute as String)
    if role == (kAXScrollBarRole as String) {
        return adjustScrollBar(element, dy: dy)
    }
    if role == (kAXScrollAreaRole as String), let bar = verticalBar(element) {
        return adjustScrollBar(bar, dy: dy)
    }
    if depth >= 3 { return false }
    for child in axChildren(element) where adjustScroll(child, dy: dy, depth: depth + 1) {
        return true
    }
    return false
}

func verticalBar(_ element: AXUIElement) -> AXUIElement? {
    guard let value = axCopy(element, kAXVerticalScrollBarAttribute as String),
          CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
    return (value as! AXUIElement)
}

func wheel(_ value: Double) -> Int32 {
    Int32(max(-10000, min(10000, Int(value.rounded()))))
}

func scroll(_ request: [String: Any]) throws -> [String: Any] {
    try requireAccessibility()
    try useApp(jsonString(request, "app"))
    let dy = jsonDouble(request, "dy") ?? 0
    let dx = jsonDouble(request, "dx") ?? 0
    if let element = elementIfPresent(request, "element"), adjustScroll(element, dy: dy) {
        return ["text": "scroll ok"]
    }
    guard let event = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 2,
                              wheel1: wheel(-dy), wheel2: wheel(-dx), wheel3: 0) else {
        throw CuaFailure("TesseraCUA: could not build a scroll event")
    }
    if let x = jsonDouble(request, "x"), let y = jsonDouble(request, "y") {
        event.location = try eventPoint(imageX: x, imageY: y)
    } else if windowRect.width > 0 {
        event.location = eventPoint(topX: Double(windowRect.midX), topY: Double(windowRect.midY))
    }
    try postToPid(event)
    return ["text": "scroll ok"]
}

func drag(_ request: [String: Any]) throws -> [String: Any] {
    try requireAccessibility()
    try useApp(jsonString(request, "app"))
    guard let from = try pointForAction(request, elementKey: "element", xKey: "x", yKey: "y"),
          let to = try pointForAction(request, elementKey: "to_element", xKey: "to_x", yKey: "to_y") else {
        throw CuaFailure("TesseraCUA: drag needs a start and end, as element indexes or x,y")
    }
    let right = (jsonString(request, "button") ?? "left") == "right"
    if right {
        try mouse(down: .rightMouseDown, up: .rightMouseUp, button: .right, at: from, dragged: .rightMouseDragged, to: to)
    } else {
        try mouse(down: .leftMouseDown, up: .leftMouseUp, button: .left, at: from, dragged: .leftMouseDragged, to: to)
    }
    return ["text": "drag ok"]
}

func pressKey(_ request: [String: Any]) throws -> [String: Any] {
    try requireAccessibility()
    try useApp(jsonString(request, "app"))
    var flags: CGEventFlags = []
    var code: CGKeyCode?
    for part in (jsonString(request, "key") ?? "").lowercased().split(separator: "+").map(String.init) {
        if let flag = modifiers[part] { flags.insert(flag) } else { code = keyCodes[part] }
    }
    guard let key = code else {
        throw CuaFailure("TesseraCUA: unknown key \(jsonString(request, "key") ?? "")")
    }
    for isDown in [true, false] {
        let event = CGEvent(keyboardEventSource: nil, virtualKey: key, keyDown: isDown)
        event?.flags = flags
        try postToPid(event)
    }
    return ["text": "press_key ok"]
}

func typeText(_ request: [String: Any]) throws -> [String: Any] {
    try requireAccessibility()
    try useApp(jsonString(request, "app"))
    let text = jsonString(request, "text") ?? ""
    for scalar in text.utf16 {
        var unit = scalar
        for isDown in [true, false] {
            let event = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: isDown)
            event?.keyboardSetUnicodeString(stringLength: 1, unicodeString: &unit)
            try postToPid(event)
        }
    }
    return ["text": "type_text ok"]
}

func handle(_ request: [String: Any]) throws -> [String: Any] {
    switch jsonString(request, "op") ?? "" {
    case "list_apps": return listApps()
    case "get_app_state": return try getAppState(request)
    case "screenshot": return try screenshot(request)
    case "click": return try click(request)
    case "perform_secondary_action": return try secondary(request)
    case "set_value": return try setValue(request)
    case "select_text": return try selectText(request)
    case "scroll": return try scroll(request)
    case "drag": return try drag(request)
    case "press_key": return try pressKey(request)
    case "type_text": return try typeText(request)
    default: throw CuaFailure("TesseraCUA: unknown op")
    }
}

func serve() {
    while let line = readLine(strippingNewline: true) {
        if line.isEmpty { continue }
        guard let data = line.data(using: .utf8),
              let request = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }
        let id = request["id"] ?? NSNull()
        do {
            var response = try handle(request)
            response["id"] = id
            response["ok"] = true
            emit(response)
        } catch let failure as CuaFailure {
            var response: [String: Any] = ["id": id, "ok": false, "error": failure.message]
            if let text = failure.text { response["text"] = text }
            emit(response)
        } catch {
            emit(["id": id, "ok": false, "error": "TesseraCUA: \(error.localizedDescription)"])
        }
    }
}

DispatchQueue.global(qos: .userInitiated).async {
    serve()
    exit(0)
}
dispatchMain()
