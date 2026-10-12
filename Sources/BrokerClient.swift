import Foundation
import CryptoKit
import Darwin

/// Only anonymous subprocess pipes carry secret values; the control protocol never does.
struct BrokerClient {
    private struct Owner: Decodable {
        let version: Int
        let instance: String
        let executable: String
        let sha256: String
    }
    let home: URL
    init(home: URL? = nil) {
        self.home = home ?? URL(fileURLWithPath: ProcessInfo.processInfo.environment["VIRTY_BROKER_HOME"] ??
                   FileManager.default.currentDirectoryPath + "/runtime/secrets", isDirectory: true)
    }

    /// The deadline starts before dispatch, so queue and lock contention are included.
    func callAsync(_ operation: String, vmID: String, alias: String,
                   cancellation: BrokerCancellation, timeout: TimeInterval = 15) async throws -> [String: Any] {
        let deadline = ProcessInfo.processInfo.systemUptime + timeout
        return try await withTaskCancellationHandler(operation: {
            try await withCheckedThrowingContinuation { continuation in
                DispatchQueue.global(qos: .utility).async {
                    do { continuation.resume(returning: try self.call(operation, vmID: vmID, alias: alias,
                                                                      cancellation: cancellation, deadline: deadline)) }
                    catch { continuation.resume(throwing: error) }
                }
            }
        }, onCancel: { cancellation.cancel() })
    }

    func call(_ operation: String, vmID: String, alias: String,
              cancellation: BrokerCancellation = BrokerCancellation(),
              deadline: TimeInterval = ProcessInfo.processInfo.systemUptime + 15) throws -> [String: Any] {
        func checkDeadline() throws {
            guard !cancellation.isCancelled else { throw Failure("Secret access cancelled; no guest input delivered.") }
            guard ProcessInfo.processInfo.systemUptime < deadline else {
                throw Failure("Secret-broker deadline exceeded; no guest input delivered.")
            }
        }
        try checkDeadline()
        try privateDirectory(home)
        let descriptor = open(home.appendingPathComponent("broker.lock").path, O_CREAT | O_RDWR | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK, 0o600)
        guard descriptor >= 0 else { throw Failure("Cannot open secret-broker administration lock.") }
        defer { close(descriptor) }
        var lockInfo = stat()
        guard fstat(descriptor, &lockInfo) == 0, lockInfo.st_mode & S_IFMT == S_IFREG,
              lockInfo.st_uid == getuid(), lockInfo.st_mode & 0o777 == 0o600 else {
            throw Failure("Secret-broker lock must be an owned private regular file.")
        }
        while flock(descriptor, (operation == "generate" ? LOCK_EX : LOCK_SH) | LOCK_NB) != 0 {
            guard errno == EWOULDBLOCK || errno == EINTR else {
                throw Failure("Cannot acquire secret-broker administration lock.")
            }
            try checkDeadline()
            usleep(10_000)
        }
        try checkDeadline()
        let path = home.appendingPathComponent("broker.json")
        let attributes = try FileManager.default.attributesOfItem(atPath: path.path)
        guard attributes[.type] as? FileAttributeType == .typeRegular,
              (attributes[.ownerAccountID] as? NSNumber)?.uint32Value == getuid(),
              (attributes[.posixPermissions] as? NSNumber)?.intValue == 0o600 else {
            throw Failure("Broker owner record must be an owned regular file with mode 0600.")
        }
        func readOwnedFile(_ file: URL, limit: Int) throws -> Data {
            let fd = open(file.path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
            guard fd >= 0 else { throw Failure("Cannot open pinned broker file.") }
            defer { close(fd) }
            var info = stat()
            guard fstat(fd, &info) == 0, info.st_mode & S_IFMT == S_IFREG,
                  info.st_uid == getuid(), info.st_size >= 0, info.st_size <= limit else {
                throw Failure("Pinned broker file is not an owned bounded regular file.")
            }
            var value = Data(), bytes = [UInt8](repeating: 0, count: 65536)
            while true {
                try checkDeadline()
                let count = read(fd, &bytes, bytes.count)
                if count == 0 { return value }
                if count < 0 {
                    if errno == EINTR { continue }
                    throw Failure("Cannot read pinned broker file.")
                }
                guard value.count + count <= limit else { throw Failure("Pinned broker file exceeds its limit.") }
                value.append(contentsOf: bytes.prefix(count))
            }
        }
        let owner = try JSONDecoder().decode(Owner.self, from: readOwnedFile(path, limit: 16384))
        let executable = URL(fileURLWithPath: owner.executable)
        let executableAttributes = try FileManager.default.attributesOfItem(atPath: owner.executable)
        guard owner.version == 1, UUID(uuidString: owner.instance)?.uuidString == owner.instance,
              owner.executable.hasPrefix("/"), executableAttributes[.type] as? FileAttributeType == .typeRegular,
              (executableAttributes[.ownerAccountID] as? NSNumber)?.uint32Value == getuid(),
              ((executableAttributes[.posixPermissions] as? NSNumber)?.intValue ?? 0o777) & 0o022 == 0,
              SHA256.hash(data: try readOwnedFile(executable, limit: 64 * 1024 * 1024)).map({ String(format: "%02x", $0) }).joined() == owner.sha256 else {
            throw Failure("Pinned secret broker changed or its owner record is invalid; preserve its enrolled build.")
        }
        let child = Process()
        child.executableURL = executable
        let input = Pipe(), output = Pipe(), errors = Pipe()
        child.standardInput = input; child.standardOutput = output; child.standardError = errors
        try checkDeadline()
        try child.run()
        // Drain both pipes under the deadline with a strict stdout cap;
        // stderr is discarded without exposing secrets.
        input.fileHandleForReading.closeFile()
        output.fileHandleForWriting.closeFile()
        errors.fileHandleForWriting.closeFile()
        defer {
            input.fileHandleForWriting.closeFile()
            output.fileHandleForReading.closeFile()
            errors.fileHandleForReading.closeFile()
            if child.isRunning {
                // Kill and reap a timed-out/cancelled broker off-main. An OS-level
                // uninterruptible child state can still delay reaping.
                kill(child.processIdentifier, SIGKILL)
                child.waitUntilExit()
            }
        }
        guard fcntl(input.fileHandleForWriting.fileDescriptor, F_SETNOSIGPIPE, 1) == 0 else {
            throw Failure("Cannot protect secret-broker request pipe.")
        }
        let request: [String: Any] = ["operation": operation, "instance": owner.instance, "vmID": vmID, "alias": alias]
        try input.fileHandleForWriting.write(contentsOf: JSONSerialization.data(withJSONObject: request))
        try input.fileHandleForWriting.close()
        let stdout = output.fileHandleForReading.fileDescriptor
        let stderr = errors.fileHandleForReading.fileDescriptor
        guard fcntl(stdout, F_SETFL, O_NONBLOCK) == 0, fcntl(stderr, F_SETFL, O_NONBLOCK) == 0 else {
            throw Failure("Cannot configure bounded secret-broker pipes.")
        }
        var response = Data(), ended: Set<Int32> = []
        var buffer = [UInt8](repeating: 0, count: 4096)
        while child.isRunning || ended.count != 2 {
            try checkDeadline()
            for fd in [stdout, stderr] where !ended.contains(fd) {
                // One chunk per iteration also bounds a noisy child's CPU occupancy.
                let count = read(fd, &buffer, buffer.count)
                if count > 0, fd == stdout {
                    guard response.count + count <= 16384 else { throw Failure("Secret broker response exceeds its limit.") }
                    response.append(contentsOf: buffer.prefix(count))
                } else if count == 0 { ended.insert(fd) }
                else if count < 0, errno != EAGAIN && errno != EINTR {
                    throw Failure("Cannot read secret-broker response.")
                }
            }
            usleep(10_000)
        }
        child.waitUntilExit()
        try checkDeadline()
        guard child.terminationStatus == 0, response.count <= 16384,
              let value = try JSONSerialization.jsonObject(with: response) as? [String: Any],
              value["ok"] as? Bool == true else {
            throw Failure("Secret broker could not access the requested alias; no authentication UI requested.")
        }
        return value
    }
}

/// Shared by the main-actor owner and the background broker worker.
final class BrokerCancellation: @unchecked Sendable {
    private let lock = NSLock()
    private var cancelled = false
    var isCancelled: Bool { lock.lock(); defer { lock.unlock() }; return cancelled }
    func cancel() { lock.lock(); cancelled = true; lock.unlock() }
}
