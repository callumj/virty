import Foundation
import CryptoKit
import Darwin

/// Each accepted ID is retained indefinitely. A lookup/write touches one indexed
/// record, so normal typing never loads or serializes the complete history.
final class OperationLedger {
    struct Record: Codable, Equatable {
        let requestID: String
        let command: String
        var status: String
        let alias: String?
        let epoch: String?
    }
    private struct LegacyFingerprint: Codable, Equatable {
        let inode: UInt64
        let size: Int64
        let modifiedSeconds: Int64
        let modifiedNanoseconds: Int64
        let changedSeconds: Int64
        let changedNanoseconds: Int64
    }
    private struct Migration: Codable { let version: Int; let legacy: LegacyFingerprint? }
    private let root: URL
    private let epoch = UUID().uuidString
    private var activeIDs: Set<String> = [] // Only current deliveries; finalized IDs remain on disk.

    static func validRequestID(_ id: String) -> Bool {
        !id.isEmpty && id.count <= 128 && id.allSatisfy {
            $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "-")
        }
    }

    init(home: URL) throws {
        root = home.appendingPathComponent("operations", isDirectory: true)
        try Self.directory(root)
        let marker = root.appendingPathComponent("migration.json")
        let legacy = home.appendingPathComponent("operations.json")
        if FileManager.default.fileExists(atPath: marker.path) {
            let migration = try JSONDecoder().decode(Migration.self, from: Self.read(marker))
            guard migration.version == 1 else { throw Failure("Unsupported typing-ledger migration; preserve its records.") }
            guard try Self.fingerprint(legacy) == migration.legacy else {
                throw Failure("Legacy typing ledger changed after migration; controller downgrade is unsupported. Preserve both ledgers.")
            }
            return
        }
        if FileManager.default.fileExists(atPath: legacy.path) {
            // A one-time migration may read the legacy full dictionary. Until the
            // durable marker commits, every restart repeats it before accepting input.
            // The legacy file is deliberately retained for review/recovery.
            let entries = try JSONDecoder().decode([String: [String: String]].self, from: Self.read(legacy, limit: nil, legacy: true))
            for (id, entry) in entries {
                guard Self.validRequestID(id), let command = entry["command"],
                      let status = entry["status"] else { throw Failure("Invalid legacy typing ledger; no input accepted.") }
                let record = Record(requestID: id, command: command,
                                    status: status == "started" ? "uncertain" : status,
                                    alias: entry["alias"], epoch: nil)
                try validate(record)
                if let existing = try rawRecord(id) {
                    guard existing == record else { throw Failure("Incomplete typing-ledger migration conflicts with a record; no input accepted.") }
                } else { try persist(record) }
            }
        }
        try Self.write(try JSONEncoder().encode(Migration(version: 1, legacy: try Self.fingerprint(legacy))), to: marker)
    }

    func lookup(_ id: String) throws -> Record? {
        guard var record = try rawRecord(id) else { return nil }
        // A started record from another process may already have delivered keys.
        // Treat it as uncertain without rewriting/scanning the historical ledger.
        if record.status == "started", record.epoch != epoch || !activeIDs.contains(id) { record.status = "uncertain" }
        return record
    }

    func begin(_ id: String, command: String, alias: String?) throws {
        guard try rawRecord(id) == nil else { throw Failure("Typing request ID already accepted; no input delivered.") }
        try persist(Record(requestID: id, command: command, status: "started", alias: alias, epoch: epoch))
        activeIDs.insert(id)
    }

    func finish(_ id: String, status: String) throws {
        defer { activeIDs.remove(id) }
        guard var record = try rawRecord(id), record.epoch == epoch, record.status == "started",
              status == "typed" || status == "uncertain" else { throw Failure("Invalid typing-ledger transition.") }
        record.status = status
        try persist(record)
    }

    private func path(_ id: String) throws -> URL {
        guard Self.validRequestID(id) else { throw Failure("Invalid typing request ID.") }
        let digest = SHA256.hash(data: Data(id.utf8)).map { String(format: "%02x", $0) }.joined()
        // Hex digests also avoid case-insensitive APFS collisions between distinct IDs.
        return root.appendingPathComponent(String(digest.prefix(2)), isDirectory: true)
            .appendingPathComponent(digest + ".json")
    }

    private func validate(_ record: Record) throws {
        guard Self.validRequestID(record.requestID), ["type-text", "type-secret"].contains(record.command),
              ["started", "typed", "uncertain"].contains(record.status),
              record.alias == nil || (record.command == "type-secret" && !record.alias!.isEmpty && record.alias!.utf8.count <= 64),
              record.epoch == nil || UUID(uuidString: record.epoch!) != nil else {
            throw Failure("Corrupt typing-ledger record; no input accepted.")
        }
    }

    private func rawRecord(_ id: String) throws -> Record? {
        let file = try path(id)
        var info = stat()
        if lstat(file.path, &info) != 0 {
            guard errno == ENOENT else { throw Failure("Cannot inspect typing-ledger record.") }
            // Also check an existing shard: absence through a symlink must not allow
            // input to be mistaken for an unused request ID.
            if FileManager.default.fileExists(atPath: file.deletingLastPathComponent().path) {
                try Self.directory(file.deletingLastPathComponent())
            }
            return nil
        }
        try Self.directory(file.deletingLastPathComponent())
        let record = try JSONDecoder().decode(Record.self, from: Self.read(file))
        try validate(record)
        guard record.requestID == id else { throw Failure("Typing-ledger request identity mismatch.") }
        return record
    }

    private func persist(_ record: Record) throws {
        try validate(record)
        let file = try path(record.requestID)
        try Self.directory(file.deletingLastPathComponent())
        try Self.write(try JSONEncoder().encode(record), to: file)
    }

    private static func fingerprint(_ url: URL) throws -> LegacyFingerprint? {
        var info = stat()
        if lstat(url.path, &info) != 0 {
            guard errno == ENOENT else { throw Failure("Cannot inspect legacy typing ledger.") }
            return nil
        }
        guard info.st_mode & S_IFMT == S_IFREG, info.st_uid == getuid(), info.st_mode & 0o077 == 0 else {
            throw Failure("Retained legacy typing ledger is no longer an owned private regular file.")
        }
        return LegacyFingerprint(inode: UInt64(info.st_ino), size: info.st_size,
                                 modifiedSeconds: Int64(info.st_mtimespec.tv_sec), modifiedNanoseconds: Int64(info.st_mtimespec.tv_nsec),
                                 changedSeconds: Int64(info.st_ctimespec.tv_sec), changedNanoseconds: Int64(info.st_ctimespec.tv_nsec))
    }

    private static func directory(_ url: URL) throws {
        var previous = stat()
        let exists = lstat(url.path, &previous) == 0
        if !exists {
            guard errno == ENOENT else { throw Failure("Cannot inspect typing-ledger directory.") }
            try FileManager.default.createDirectory(at: url, withIntermediateDirectories: false,
                                                   attributes: [.posixPermissions: 0o700])
        }
        var info = stat()
        guard lstat(url.path, &info) == 0, info.st_mode & S_IFMT == S_IFDIR,
              info.st_uid == getuid(), info.st_mode & 0o777 == 0o700 else {
            throw Failure("Typing-ledger directory must be owned, private and not a symlink.")
        }
        // Newly created shard names must survive before their records can commit.
        if !exists { try syncDirectory(url.deletingLastPathComponent()) }
    }

    private static func read(_ url: URL, limit: Int? = 16384, legacy: Bool = false) throws -> Data {
        let fd = open(url.path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard fd >= 0 else { throw Failure("Cannot read typing ledger; no input accepted.") }
        defer { close(fd) }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_mode & S_IFMT == S_IFREG, info.st_uid == getuid(),
              (legacy ? info.st_mode & 0o022 == 0 : info.st_mode & 0o077 == 0), info.st_size >= 0,
              limit == nil || info.st_size <= limit! else {
            throw Failure("Typing ledger must be an owned private regular file within its size limit.")
        }
        // The former JSONEncoder atomic path inherited umask (often 0644).
        // Preserve its contents while making the retained migration source private.
        if legacy, fchmod(fd, 0o600) != 0 { throw Failure("Cannot protect legacy typing ledger.") }
        var data = Data(), bytes = [UInt8](repeating: 0, count: 65536)
        while true {
            let count = Darwin.read(fd, &bytes, bytes.count)
            if count == 0 { return data }
            if count < 0 { if errno == EINTR { continue }; throw Failure("Cannot read typing ledger.") }
            guard limit == nil || data.count + count <= limit! else { throw Failure("Typing ledger exceeds its size limit.") }
            data.append(contentsOf: bytes.prefix(count))
        }
    }

    private static func write(_ data: Data, to url: URL) throws {
        let temporary = url.deletingLastPathComponent().appendingPathComponent(".pending-" + UUID().uuidString)
        let fd = open(temporary.path, O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw Failure("Cannot create durable typing-ledger record.") }
        defer { close(fd); unlink(temporary.path) }
        try data.withUnsafeBytes { bytes in
            var offset = 0
            while offset < bytes.count {
                let count = Darwin.write(fd, bytes.baseAddress!.advanced(by: offset), bytes.count - offset)
                if count < 0 { if errno == EINTR { continue }; throw Failure("Cannot write typing-ledger record.") }
                guard count > 0 else { throw Failure("Incomplete typing-ledger write.") }
                offset += count
            }
        }
        guard fsync(fd) == 0, rename(temporary.path, url.path) == 0 else {
            throw Failure("Cannot commit durable typing-ledger record.")
        }
        try syncDirectory(url.deletingLastPathComponent())
    }

    private static func syncDirectory(_ url: URL) throws {
        let fd = open(url.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
        guard fd >= 0 else { throw Failure("Cannot open typing-ledger directory for durability.") }
        defer { close(fd) }
        guard fsync(fd) == 0 else { throw Failure("Cannot sync typing-ledger directory.") }
    }
}
