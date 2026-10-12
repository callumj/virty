import AppKit
import Virtualization

@MainActor
final class Controller: NSObject, VZVirtualMachineDelegate {
    let home: URL
    var record: VMRecord
    let vm: VZVirtualMachine
    let view: GuestView
    let window: NSWindow
    private var socket: ControlSocket?
    private var busy = false
    private var cancelRequested = false
    private var secretCancellation: BrokerCancellation?
    private let operations: OperationLedger
    private var generation = UUID().uuidString
    private var framebuffer: ConsoleFramebuffer?

    init(home: URL) throws {
        self.home = home
        record = try VMRecord.load(home)
        operations = try OperationLedger(home: home)
        guard record.installed else { throw Failure("Install the guest before starting the manager.") }
        vm = VZVirtualMachine(configuration: try configuration(home: home, record: record))
        view = GuestView(frame: NSRect(x: 0, y: 0, width: record.width, height: record.height))
        window = NSWindow(contentRect: view.frame, styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        super.init()
        let name = ProcessInfo.processInfo.environment["VIRTY_VM_NAME"] ?? record.id
        window.title = "Virty — \(name) (\(record.os))"
        window.isReleasedWhenClosed = false
        window.contentView = view
        view.virtualMachine = vm
        view.automaticallyReconfiguresDisplay = false
        view.capturesSystemKeys = false
        window.makeFirstResponder(view)
        vm.delegate = self
    }

    func listen() throws {
        socket = ControlSocket(path: home.appendingPathComponent("control.sock").path)
        try socket!.start { [weak self] request in
            guard let self else { return ["ok": false, "error": "Manager unavailable."] }
            do { return try await self.handle(request) }
            catch { return ["ok": false, "error": error.localizedDescription] }
        }
    }

    func handle(_ request: [String: Any]) async throws -> [String: Any] {
        if let expected = request["vmID"] {
            guard expected as? String == record.id else {
                throw Failure("VM identity mismatch; no command was delivered.")
            }
        }
        guard let command = request["command"] as? String else { throw Failure("Missing command.") }
        if command == "status" {
            return ["ok": true, "vmID": record.id, "guestOS": record.os, "username": record.username, "state": stateName,
                    "inputBusy": busy, "keychain": record.keychain, "provisioned": record.provisioned, "accountSetup": record.accountSetup,
                    "width": record.width, "height": record.height, "generation": generation,
                    "consoleVisible": window.isVisible, "installerAttached": record.installMedia != nil,
                    "captureBackend": "private-console-loopback", "supportsSnapshots": true, "supportsSnapshotPolicyRecovery": true]
        }
        if command == "cancel" { cancelRequested = true; secretCancellation?.cancel(); return ["ok": true, "cancelRequested": busy] }
        if command == "operation" {
            guard let id = request["requestId"] as? String, let previous = try operations.lookup(id) else { throw Failure("Unknown operation.") }
            return ["ok": true, "requestId": id, "status": previous.status]
        }
        guard !busy else { throw Failure("Guest control is busy; screenshot/input requests are blocked during typing.") }
        switch command {
        case "start":
            guard !["maintenance.json", "snapshot-policy.json"].contains(where: { FileManager.default.fileExists(atPath: home.appendingPathComponent($0).path) }) else {
                throw Failure("VM snapshot maintenance is pending; recover it before starting.")
            }
            guard vm.state == .stopped else { throw Failure("Guest must be stopped before start.") }
            busy = true; cancelRequested = false
            defer { busy = false; secretCancellation = nil }
            framebuffer?.stop(); framebuffer = nil
            if record.os == "macos" {
                let options = VZMacOSVirtualMachineStartOptions()
                let provisionGuest = record.needsGuestProvisioning
                if provisionGuest {
                    let provisioning = VZMacGuestProvisioningOptions()
                    provisioning.username = record.username
                    provisioning.fullName = "Virty Test"
                    let cancellation = BrokerCancellation(); secretCancellation = cancellation
                    let expectedID = record.id, expectedGeneration = generation
                    provisioning.password = try await SecretStore(vmID: record.id, mode: record.keychain)
                        .readForTypingAsync(cancellation: cancellation)
                    guard !cancelRequested, !cancellation.isCancelled, record.id == expectedID,
                          generation == expectedGeneration, vm.state == .stopped else {
                        throw Failure("Guest start cancelled or state changed during secret access.")
                    }
                    provisioning.logsInAutomatically = false
                    provisioning.enablesRemoteLogin = false
                    try options.setGuestProvisioning(provisioning)
                }
                try await withCheckedThrowingContinuation { (c: CheckedContinuation<Void, Error>) in
                    vm.start(options: options) { error in
                        if let error { c.resume(throwing: error) } else { c.resume() }
                    }
                }
                if provisionGuest { record.provisioned = true; try record.save(home) }
            } else {
                try await vm.start()
            }
            generation = UUID().uuidString
            return ["ok": true, "state": stateName]
        case "stop":
            if request["force"] as? Bool == true {
                guard vm.canStop else { throw Failure("Guest cannot stop in its current state.") }
                busy = true; defer { busy = false }
                framebuffer?.stop(); framebuffer = nil
                try await vm.stop()
                return ["ok": true, "state": stateName, "forced": true]
            }
            guard vm.canRequestStop else { throw Failure("Guest cannot request shutdown in its current state.") }
            try vm.requestStop()
            return ["ok": true, "shutdownRequested": true]
        case "quit":
            guard vm.state == .stopped || vm.state == .error else { throw Failure("Stop the guest before quitting the manager.") }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { NSApplication.shared.terminate(nil) }
            return ["ok": true]
        case "show":
            window.makeKeyAndOrderFront(nil)
            NSApplication.shared.activate()
            return ["ok": true]
        case "hide": window.orderOut(nil); return ["ok": true]
        default: break
        }
        guard vm.state == .running else { throw Failure("Guest is not running.") }
        if let expected = request["generation"] as? String, expected != generation { throw Failure("Stale display generation.") }
        switch command {
        case "screenshot":
            busy = true; defer { busy = false }
            // Open the console on demand after the guest has started.
            if framebuffer == nil {
                let console = try VirtyVNCConsole(virtualMachine: vm)
                let candidate = ConsoleFramebuffer(console: console)
                try await candidate.validate()
                framebuffer = candidate
            }
            let frame = try await framebuffer!.capture(width: record.width, height: record.height)
            return ["ok": true, "png": frame.png.base64EncodedString(), "width": frame.width,
                    "height": frame.height, "generation": generation, "backend": "private-console-loopback",
                    "cached": frame.cached, "updatedAt": ISO8601DateFormatter().string(from: frame.updatedAt)]
        case "click":
            guard let x = request["x"] as? Int, let y = request["y"] as? Int,
                  x >= 0, y >= 0, x < record.width, y < record.height else { throw Failure("Coordinates outside the guest display.") }
            try view.injectClick(x: x, y: y, width: record.width, height: record.height)
            return ["ok": true]
        case "key":
            guard let chord = request["chord"] as? String else { throw Failure("Missing key chord.") }
            try key(chord)
            return ["ok": true]
        case "scroll":
            guard let expected = request["generation"] as? String, !expected.isEmpty else {
                throw Failure("Scroll requires a recent display generation.")
            }
            guard let x = request["x"] as? Int, let y = request["y"] as? Int,
                  x >= 0, y >= 0, x < record.width, y < record.height else { throw Failure("Coordinates outside the guest display.") }
            guard request["deltaX"] == nil, let deltaY = request["deltaY"] as? Int else {
                throw Failure("Supply integer deltaY only; horizontal scrolling is not supported.")
            }
            try view.injectScroll(x: x, y: y, width: record.width, height: record.height, deltaY: deltaY)
            return ["ok": true]
        case "type-text", "type-secret":
            guard let id = request["requestId"] as? String, OperationLedger.validRequestID(id) else {
                throw Failure("Typing needs an opaque alphanumeric requestId (hyphens allowed).")
            }
            if let previous = try operations.lookup(id) {
                guard previous.command == command else { throw Failure("Request ID was used for another operation.") }
                if command == "type-secret" {
                    guard request["alias"] as? String == (previous.alias ?? "vm-login") else {
                        throw Failure("Request ID was used for another secret alias.")
                    }
                }
                return ["ok": previous.status == "typed", "requestId": id, "status": previous.status, "replayed": true]
            }
            let alias: String?
            let publicText: String?
            if command == "type-secret" {
                guard let value = request["alias"] as? String, !value.isEmpty, value.utf8.count <= 64 else {
                    throw Failure("Missing or invalid VM-scoped secret alias.")
                }
                alias = value; publicText = nil
            } else {
                guard let value = request["text"] as? String, value.count <= 4096 else { throw Failure("Text missing or too long.") }
                publicText = value; alias = nil
            }
            let delay = request["delayMs"] as? Int ?? 80
            guard (20...1000).contains(delay) else { throw Failure("Delay must be 20–1000 ms.") }
            // Reserve ownership before suspending for secret access. Cancel and status
            // remain available; all other controls stay excluded throughout lookup.
            let expectedID = record.id, expectedGeneration = generation
            let cancellation = BrokerCancellation()
            busy = true; cancelRequested = false; secretCancellation = cancellation
            view.automationOwnsInput = true
            var beganInjection = false
            defer {
                busy = false; secretCancellation = nil; view.automationOwnsInput = false
                if beganInjection { try? view.injectModifier(code: 56, flags: []) }
            }
            try operations.begin(id, command: command, alias: alias)
            do {
                let text: String
                if let alias {
                    text = try await SecretStore(vmID: expectedID, mode: record.keychain)
                        .readForTypingAsync(alias: alias, cancellation: cancellation)
                } else { text = publicText! }
                guard !cancelRequested, !cancellation.isCancelled, record.id == expectedID,
                      generation == expectedGeneration, vm.state == .running else {
                    throw Failure("Guest state changed or typing was cancelled during secret access.")
                }
                let strokes = try USKeyboard.strokes(text, allowControl: command != "type-secret")
                guard strokes.count * delay <= 60000 else { throw Failure("Typing would exceed the 60-second request limit.") }
                for stroke in strokes {
                    guard !cancelRequested, !cancellation.isCancelled, record.id == expectedID,
                          generation == expectedGeneration, vm.state == .running else { throw Failure("Typing interrupted; delivery may be partial.") }
                    beganInjection = true
                    if stroke.shift { try view.injectModifier(code: 56, flags: [.shift]) }
                    do {
                        try view.injectKey(code: stroke.code, down: true, flags: stroke.shift ? [.shift] : [])
                        try view.injectKey(code: stroke.code, down: false, flags: stroke.shift ? [.shift] : [])
                    } catch { try? view.injectKey(code: stroke.code, down: false); throw error }
                    if stroke.shift { try view.injectModifier(code: 56, flags: []) }
                    try await Task.sleep(nanoseconds: UInt64(delay) * 1_000_000)
                }
                try operations.finish(id, status: "typed")
                return ["ok": true, "requestId": id, "status": "typed"]
            } catch {
                try? operations.finish(id, status: "uncertain")
                throw Failure("Typing interrupted; request marked uncertain and will not be automatically repeated.")
            }
        default: throw Failure("Unknown control command.")
        }
    }

    private func key(_ chord: String) throws {
        let parts = chord.lowercased().split(separator: "+").map(String.init)
        guard let last = parts.last else { throw Failure("Empty key chord.") }
        let code: UInt16
        if let found = USKeyboard.special[last] { code = found }
        else if last.count == 1, let char = last.first, let found = USKeyboard.plain[char] { code = found }
        else { throw Failure("Unsupported key.") }
        let modifiers: [String: (UInt16, NSEvent.ModifierFlags)] = ["cmd":(55,.command),"shift":(56,.shift),"alt":(58,.option),"ctrl":(59,.control)]
        var flags = NSEvent.ModifierFlags()
        var held: [UInt16] = []
        let requested = try parts.dropLast().map { part -> (UInt16, NSEvent.ModifierFlags) in
            guard let modifier = modifiers[part] else { throw Failure("Unsupported modifier.") }; return modifier
        }
        defer { for code in held.reversed() { try? view.injectModifier(code: code, flags: []) } }
        for (modifierCode, flag) in requested {
            flags.insert(flag); try view.injectModifier(code: modifierCode, flags: flags); held.append(modifierCode)
        }
        do { try view.injectKey(code: code, down: true, flags: flags); try view.injectKey(code: code, down: false, flags: flags) }
        catch { try? view.injectKey(code: code, down: false); throw error }
    }

    var stateName: String {
        switch vm.state {
        case .stopped: return "stopped"
        case .running: return "running"
        case .paused: return "paused"
        case .error: return "error"
        case .starting: return "starting"
        case .stopping: return "stopping"
        case .pausing: return "pausing"
        case .resuming: return "resuming"
        default: return "transitioning"
        }
    }
    nonisolated func guestDidStop(_ virtualMachine: VZVirtualMachine) { note("Guest stopped.") }
    nonisolated func virtualMachine(_ virtualMachine: VZVirtualMachine, didStopWithError error: Error) {
        let code = (error as NSError).code
        note("Guest stopped with a virtualization error (code \(code)).")
    }
}
