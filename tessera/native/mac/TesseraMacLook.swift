// Legacy helper for mac_look / mac_act. Launched through LaunchServices as its own app so
// Screen Recording and Accessibility grants attach to this bundle. This posts global events.
// Background computer use is TesseraBackground.swift, built by scripts/build-cua-helper.sh.
// Usage: TesseraMacLook <output.json> status|request|screenshot <png>|act <json>
import AppKit
import ApplicationServices
import CoreGraphics
import Foundation

func fullDiskAccess() -> Bool {
    // The system TCC database is readable only with Full Disk Access.
    let path = "/Library/Application Support/com.apple.TCC/TCC.db"
    return FileManager.default.isReadableFile(atPath: path) && (try? FileHandle(forReadingFrom: URL(fileURLWithPath: path))) != nil
}

func status() -> [String: Any] {
    let bounds = CGDisplayBounds(CGMainDisplayID())
    return ["screen_recording": CGPreflightScreenCaptureAccess(), "accessibility": AXIsProcessTrusted(),
            "full_disk_access": fullDiskAccess(), "bundle_id": Bundle.main.bundleIdentifier ?? "",
            "executable": Bundle.main.executablePath ?? "", "screen_points": [bounds.width, bounds.height],
            "idle_seconds": CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: CGEventType(rawValue: ~0)!)]
}

func screenshot(_ path: String) throws -> [String: Any] {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    process.arguments = ["-x", "-t", "png", path]
    try process.run()
    process.waitUntilExit()
    var result = status()
    result["exit_code"] = Int(process.terminationStatus)
    result["path"] = path
    return result
}

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

func post(_ event: CGEvent?) { event?.post(tap: .cghidEventTap); usleep(20_000) }

func act(_ request: [String: Any]) throws -> [String: Any] {
    guard AXIsProcessTrusted() else { throw NSError(domain: "cua", code: 1, userInfo: [NSLocalizedDescriptionKey: "Accessibility permission is not granted to TesseraCUA"]) }
    let bounds = CGDisplayBounds(CGMainDisplayID())
    let width = (request["width"] as? Double) ?? Double(bounds.width)
    let height = (request["height"] as? Double) ?? Double(bounds.height)
    func point(_ action: [String: Any], _ xKey: String = "x", _ yKey: String = "y") -> CGPoint {
        CGPoint(x: (action[xKey] as? Double ?? 0) * Double(bounds.width) / width,
                y: (action[yKey] as? Double ?? 0) * Double(bounds.height) / height)
    }
    var done: [String] = []
    for action in (request["actions"] as? [[String: Any]]) ?? [] {
        let kind = action["type"] as? String ?? ""
        let button = (action["button"] as? String) == "right" ? CGMouseButton.right : CGMouseButton.left
        let down: CGEventType = button == .right ? .rightMouseDown : .leftMouseDown
        let up: CGEventType = button == .right ? .rightMouseUp : .leftMouseUp
        switch kind {
        case "move":
            post(CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: point(action), mouseButton: .left))
        case "click", "double_click":
            let at = action["x"] != nil ? point(action) : CGEvent(source: nil)!.location
            post(CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: at, mouseButton: button))
            for count in 1...(kind == "double_click" ? 2 : 1) {
                for type in [down, up] {
                    let event = CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: at, mouseButton: button)
                    event?.setIntegerValueField(.mouseEventClickState, value: Int64(count))
                    post(event)
                }
            }
        case "drag":
            let from = point(action), to = point(action, "to_x", "to_y")
            post(CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: from, mouseButton: .left))
            post(CGEvent(mouseEventSource: nil, mouseType: .leftMouseDragged, mouseCursorPosition: to, mouseButton: .left))
            post(CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: to, mouseButton: .left))
        case "scroll":
            if action["x"] != nil {
                post(CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: point(action), mouseButton: .left))
            }
            post(CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 2,
                         wheel1: Int32(-(action["dy"] as? Double ?? 0)), wheel2: Int32(-(action["dx"] as? Double ?? 0)), wheel3: 0))
        case "key":
            var flags: CGEventFlags = []
            var code: CGKeyCode? = nil
            for part in ((action["keys"] as? String) ?? "").lowercased().split(separator: "+").map(String.init) {
                if let flag = modifiers[part] { flags.insert(flag) } else { code = keyCodes[part] }
            }
            guard let key = code else { throw NSError(domain: "cua", code: 2, userInfo: [NSLocalizedDescriptionKey: "Unknown key in \(action["keys"] ?? "")"]) }
            for isDown in [true, false] {
                let event = CGEvent(keyboardEventSource: nil, virtualKey: key, keyDown: isDown)
                event?.flags = flags
                post(event)
            }
        case "type":
            for scalar in ((action["text"] as? String) ?? "").utf16 {
                var unit = scalar
                for isDown in [true, false] {
                    let event = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: isDown)
                    event?.keyboardSetUnicodeString(stringLength: 1, unicodeString: &unit)
                    post(event)
                }
            }
        case "wait":
            usleep(UInt32(min(10.0, action["seconds"] as? Double ?? 0.5) * 1_000_000))
        default:
            throw NSError(domain: "cua", code: 3, userInfo: [NSLocalizedDescriptionKey: "Unknown action \(kind)"])
        }
        done.append(kind)
    }
    return ["done": done]
}

let arguments = CommandLine.arguments
guard arguments.count >= 3 else { exit(64) }
let output = URL(fileURLWithPath: arguments[1])
var result: [String: Any]
do {
    switch arguments[2] {
    case "status": result = status()
    case "request":
        _ = CGRequestScreenCaptureAccess()
        _ = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
        result = status()
    case "screenshot": result = try screenshot(arguments[3])
    case "act":
        let request = try JSONSerialization.jsonObject(with: Data(arguments[3].utf8)) as? [String: Any] ?? [:]
        result = try act(request)
    default: result = ["error": "unknown command"]
    }
} catch {
    result = ["error": error.localizedDescription]
}
try JSONSerialization.data(withJSONObject: result).write(to: output)
