import AppKit
import Virtualization

struct KeyStroke {
    let code: UInt16
    let shift: Bool
}

enum USKeyboard {
    static let special: [String: UInt16] = ["return":36,"enter":36,"tab":48,"escape":53,"backspace":51,"delete":117,
                                          "left":123,"right":124,"down":125,"up":126,"space":49,
                                          "home":115,"end":119,"pageup":116,"pagedown":121]
    // NSEvent modifier changes carry both aggregate and device-side bits.
    // These left-key masks are defined in IOKit hidsystem/IOLLEvent.h.
    static func eventFlags(_ flags: NSEvent.ModifierFlags) -> NSEvent.ModifierFlags {
        var raw = flags.rawValue
        for (flag, device): (NSEvent.ModifierFlags, UInt) in [(.control, 0x1), (.shift, 0x2), (.command, 0x8), (.option, 0x20)] {
            if flags.contains(flag) { raw |= device }
        }
        return NSEvent.ModifierFlags(rawValue: raw)
    }
    // Physical Apple keyboard codes; Virty assumes an ANSI US guest layout.
    static let plain: [Character: UInt16] = [
        "a":0,"s":1,"d":2,"f":3,"h":4,"g":5,"z":6,"x":7,"c":8,"v":9,
        "b":11,"q":12,"w":13,"e":14,"r":15,"y":16,"t":17,"1":18,"2":19,
        "3":20,"4":21,"6":22,"5":23,"=":24,"9":25,"7":26,"-":27,"8":28,
        "0":29,"]":30,"o":31,"u":32,"[":33,"i":34,"p":35,"l":37,"j":38,
        "'":39,"k":40,";":41,"\\":42,",":43,"/":44,"n":45,"m":46,".":47,
        "`":50," ":49,"\n":36,"\t":48
    ]
    static let shifted: [Character: Character] = [
        "!":"1","@":"2","#":"3","$":"4","%":"5","^":"6","&":"7",
        "*":"8","(":"9",")":"0","_":"-","+":"=","{":"[","}":"]",
        "|":"\\",":":";","\"":"'","<":",",">":".","?":"/","~":"`"
    ]
    static func strokes(_ text: String, allowControl: Bool = true) throws -> [KeyStroke] {
        try text.map { char in
            if !allowControl, char == "\n" || char == "\t" { throw Failure("Unsupported secret character; no keys sent.") }
            if let code = plain[char] { return KeyStroke(code: code, shift: false) }
            if let lower = shifted[char], let code = plain[lower] { return KeyStroke(code: code, shift: true) }
            if char.isASCII, char.isUppercase, let lower = char.lowercased().first, let code = plain[lower] {
                return KeyStroke(code: code, shift: true)
            }
            throw Failure("Unsupported character for the ANSI US layout; no keys sent.")
        }
    }
}

@MainActor
final class GuestView: VZVirtualMachineView {
    var automationOwnsInput = false
    override func keyDown(with event: NSEvent) { if !automationOwnsInput { super.keyDown(with: event) } }
    override func keyUp(with event: NSEvent) { if !automationOwnsInput { super.keyUp(with: event) } }
    override func flagsChanged(with event: NSEvent) { if !automationOwnsInput { super.flagsChanged(with: event) } }
    override func mouseDown(with event: NSEvent) { if !automationOwnsInput { super.mouseDown(with: event) } }
    override func mouseUp(with event: NSEvent) { if !automationOwnsInput { super.mouseUp(with: event) } }
    override func mouseDragged(with event: NSEvent) { if !automationOwnsInput { super.mouseDragged(with: event) } }
    override func mouseMoved(with event: NSEvent) { if !automationOwnsInput { super.mouseMoved(with: event) } }
    override func rightMouseDown(with event: NSEvent) { if !automationOwnsInput { super.rightMouseDown(with: event) } }
    override func rightMouseUp(with event: NSEvent) { if !automationOwnsInput { super.rightMouseUp(with: event) } }
    override func rightMouseDragged(with event: NSEvent) { if !automationOwnsInput { super.rightMouseDragged(with: event) } }
    override func otherMouseDown(with event: NSEvent) { if !automationOwnsInput { super.otherMouseDown(with: event) } }
    override func otherMouseUp(with event: NSEvent) { if !automationOwnsInput { super.otherMouseUp(with: event) } }
    override func otherMouseDragged(with event: NSEvent) { if !automationOwnsInput { super.otherMouseDragged(with: event) } }
    override func scrollWheel(with event: NSEvent) { if !automationOwnsInput { super.scrollWheel(with: event) } }

    func injectKey(code: UInt16, down: Bool, flags: NSEvent.ModifierFlags = []) throws {
        guard let event = NSEvent.keyEvent(with: down ? .keyDown : .keyUp, location: .zero,
                                         modifierFlags: USKeyboard.eventFlags(flags), timestamp: ProcessInfo.processInfo.systemUptime,
                                         windowNumber: window?.windowNumber ?? 0, context: nil,
                                         characters: "", charactersIgnoringModifiers: "", isARepeat: false, keyCode: code) else {
            throw Failure("Cannot construct guest key event.")
        }
        if down { super.keyDown(with: event) } else { super.keyUp(with: event) }
    }
    func injectModifier(code: UInt16, flags: NSEvent.ModifierFlags) throws {
        guard let event = NSEvent.keyEvent(with: .flagsChanged, location: .zero, modifierFlags: USKeyboard.eventFlags(flags),
                                         timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: window?.windowNumber ?? 0,
                                         context: nil, characters: "", charactersIgnoringModifiers: "", isARepeat: false, keyCode: code) else {
            throw Failure("Cannot construct guest modifier event.")
        }
        super.flagsChanged(with: event)
    }
    func injectClick(x: Int, y: Int, width: Int, height: Int) throws {
        // Screenshot coordinates have a top-left origin; AppKit view coordinates are bottom-left.
        let location = convert(NSPoint(x: CGFloat(x) * bounds.width / CGFloat(width),
                                       y: bounds.height - CGFloat(y) * bounds.height / CGFloat(height)), to: nil)
        for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
            guard let event = NSEvent.mouseEvent(with: type, location: location, modifierFlags: [],
                                               timestamp: ProcessInfo.processInfo.systemUptime,
                                               windowNumber: window?.windowNumber ?? 0, context: nil,
                                               eventNumber: 0, clickCount: 1, pressure: type == .leftMouseDown ? 1 : 0) else {
                throw Failure("Cannot construct guest pointer event.")
            }
            if type == .leftMouseDown { super.mouseDown(with: event) } else { super.mouseUp(with: event) }
        }
    }

    func injectScroll(x: Int, y: Int, width: Int, height: Int, deltaY: Int) throws {
        let location = convert(NSPoint(x: CGFloat(x) * bounds.width / CGFloat(width),
                                       y: bounds.height - CGFloat(y) * bounds.height / CGFloat(height)), to: nil)
        let event = try GuestScroll.event(deltaY: deltaY)
        // Move only the guest pointer; scroll events are routed at its current position.
        guard let pointer = NSEvent.mouseEvent(with: .mouseMoved, location: location, modifierFlags: [],
                                               timestamp: ProcessInfo.processInfo.systemUptime,
                                               windowNumber: window?.windowNumber ?? 0, context: nil,
                                               eventNumber: 0, clickCount: 0, pressure: 0) else {
            throw Failure("Cannot construct guest pointer event.")
        }
        super.mouseMoved(with: pointer)
        super.scrollWheel(with: event)
    }
}

enum GuestScroll {
    static func event(deltaY: Int) throws -> NSEvent {
        guard (-10...10).contains(deltaY), deltaY != 0 else {
            throw Failure("Scroll deltaY must be nonzero, from -10 to 10 wheel steps.")
        }
        guard let cg = CGEvent(scrollWheelEvent2Source: CGEventSource(stateID: .privateState), units: .line,
                               wheelCount: 1, wheel1: Int32(-deltaY), wheel2: 0, wheel3: 0) else {
            throw Failure("Cannot construct guest scroll event.")
        }
        // Quartz-created NSEvents have no window. VZ consumes these deltas at the
        // guest pointer position set by injectScroll, independent of host screens.
        cg.flags = []
        guard let event = NSEvent(cgEvent: cg) else { throw Failure("Cannot convert guest scroll event.") }
        return event
    }
}
