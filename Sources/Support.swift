import Foundation
import Darwin

struct Failure: Error, LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

/// A framework callback arriving after its timeout must not resume twice.
final class OnceReply<Value: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Value, Error>?
    init(_ continuation: CheckedContinuation<Value, Error>) { self.continuation = continuation }
    func finish(_ result: Result<Value, Error>) {
        lock.lock()
        let reply = continuation
        continuation = nil
        lock.unlock()
        reply?.resume(with: result)
    }
}

func emit(_ object: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]),
       let line = String(data: data, encoding: .utf8) { print(line) }
    fflush(stdout)
}

func note(_ message: String) {
    FileHandle.standardError.write(Data((message + "\n").utf8))
}

func privateDirectory(_ url: URL) throws {
    try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true,
                                           attributes: [.posixPermissions: 0o700])
    let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
    guard (attributes[.ownerAccountID] as? NSNumber)?.uint32Value == getuid(),
          (attributes[.posixPermissions] as? NSNumber)?.intValue == 0o700 else {
        throw Failure("Runtime directory must belong to this user and have mode 0700.")
    }
}

final class InstanceLock {
    private var fd: Int32 = -1
    init(home: URL) throws {
        fd = open(home.appendingPathComponent("manager.lock").path, O_CREAT | O_RDWR | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw Failure("Cannot open VM ownership lock.") }
        guard flock(fd, LOCK_EX | LOCK_NB) == 0 else {
            close(fd); fd = -1
            throw Failure("Another Virty process owns this VM.")
        }
    }
    deinit { if fd >= 0 { close(fd) } }
}

struct VMRecord: Codable {
    var id: String
    var username: String
    var keychain: String
    var installed: Bool
    var provisioned: Bool
    var restoreBuild: String
    var cpuCount: Int
    var memoryBytes: UInt64
    var width: Int
    var height: Int
    var macAddress: String
    var managerExecutable: String
    var guestOS: String? = nil
    var installMedia: String? = nil
    // Missing on older records: retain their automatic first-boot provisioning.
    var setupAssistant: Bool? = nil
    var os: String { guestOS ?? "macos" }
    var needsGuestProvisioning: Bool { os == "macos" && setupAssistant != true && !provisioned }
    var accountSetup: String { os == "linux" ? "guest-managed" : (setupAssistant == true ? "setup-assistant" : "automatic") }

    static func load(_ home: URL) throws -> VMRecord {
        try JSONDecoder().decode(VMRecord.self, from: Data(contentsOf: home.appendingPathComponent("vm.json")))
    }
    func save(_ home: URL) throws {
        let encoder = JSONEncoder(); encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try encoder.encode(self).write(to: home.appendingPathComponent("vm.json"), options: .atomic)
    }
}
