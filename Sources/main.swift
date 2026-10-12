import AppKit
import Virtualization

umask(0o077)
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let args = Array(CommandLine.arguments.dropFirst())
let home = URL(fileURLWithPath: ProcessInfo.processInfo.environment["VIRTY_HOME"] ?? FileManager.default.currentDirectoryPath + "/runtime/vm", isDirectory: true).standardizedFileURL
var ownership: InstanceLock?
var controller: Controller?

Task { @MainActor in
    do {
        guard VZVirtualMachine.isSupported else { throw Failure("Host virtualization is unavailable in this execution context.") }
        guard let command = args.first else { throw Failure("Use latest, create IMAGE KEYCHAIN, install IMAGE, serve, or secret-probe.") }
        try privateDirectory(home)
        ownership = try InstanceLock(home: home)
        switch command {
        case "latest":
            let image = try await VZMacOSRestoreImage.latestSupported
            emit(["url": image.url.absoluteString, "build": image.buildVersion, "majorVersion": image.operatingSystemVersion.majorVersion])
        case "capabilities":
            emit(["ok": true, "supportsSnapshots": true, "supportsSnapshotPolicyRecovery": true, "supportsSetupAssistant": true])
        case "create":
            guard args.count == 3 || (args.count == 4 && args[3] == "--setup-assistant") else {
                throw Failure("Use create IMAGE KEYCHAIN [--setup-assistant] (broker, login, or data-protection).")
            }
            try await createGuest(home: home, imageURL: URL(fileURLWithPath: args[1]), keychain: args[2], setupAssistant: args.count == 4)
        case "install":
            guard args.count == 2 else { throw Failure("Use install IMAGE.") }
            try await installGuest(home: home, imageURL: URL(fileURLWithPath: args[1]))
        case "create-linux":
            guard args.count == 6, let cpus = Int(args[3]), let memory = Int(args[4]), let disk = Int(args[5]) else {
                throw Failure("Use create-linux ISO_OR_DASH RAW_DISK_OR_DASH CPUS MEMORY_GIB DISK_GIB.")
            }
            try createLinuxGuest(home: home, media: args[1], sourceDisk: args[2], cpus: cpus, memoryGiB: memory, diskGiB: disk)
        case "secret-probe":
            let record = try VMRecord.load(home)
            let store = SecretStore(vmID: record.id, mode: record.keychain)
            _ = try store.readForTyping(); _ = try store.readForTyping()
            emit(["readable": true, "keychain": record.keychain, "reads": 2])
        case "serve":
            guard !FileManager.default.fileExists(atPath: home.appendingPathComponent("maintenance.json").path) else {
                throw Failure("VM snapshot maintenance is pending; use virty snapshot recover.")
            }
            controller = try Controller(home: home)
            try controller!.listen()
            emit(["managerReady": true, "socket": home.appendingPathComponent("control.sock").path])
            return
        case "clone-identity":
            try refreshCloneIdentity(home: home)
        default: throw Failure("Unknown manager command.")
        }
        exit(0)
    } catch {
        note(error.localizedDescription)
        exit(1)
    }
}
app.run()
