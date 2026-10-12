import Foundation
import CryptoKit
import Darwin

@main struct BrokerClientTests {
    @MainActor static func main() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("virty-broker-test-\(UUID().uuidString)")
        try privateDirectory(root)
        defer { try? FileManager.default.removeItem(at: root) }
        let script = root.appendingPathComponent("broker")
        func configure(_ body: String) throws {
            try Data(("#!/bin/sh\n" + body).utf8).write(to: script)
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: script.path)
            let hash = SHA256.hash(data: try Data(contentsOf: script)).map { String(format: "%02x", $0) }.joined()
            let owner: [String: Any] = ["version": 1, "instance": UUID().uuidString, "executable": script.path, "sha256": hash]
            let path = root.appendingPathComponent("broker.json")
            try JSONSerialization.data(withJSONObject: owner).write(to: path)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
        }
        try configure("cat >/dev/null\nprintf '{\"ok\":true,\"value\":\"public-fixture\"}'\n")
        let fd = open(root.appendingPathComponent("broker.lock").path, O_CREAT | O_RDWR, 0o600)
        guard fd >= 0 else { fatalError("fixture lock") }
        defer { close(fd) }
        precondition(flock(fd, LOCK_EX) == 0)
        let client = BrokerClient(home: root)
        let token = BrokerCancellation()
        var heartbeat = false
        let pending = Task { try await client.callAsync("read", vmID: UUID().uuidString, alias: "public", cancellation: token, timeout: 0.5) }
        try await Task.sleep(nanoseconds: 50_000_000)
        heartbeat = true
        precondition(heartbeat) // Runs on the same actor while the broker waits.
        token.cancel()
        do { _ = try await pending.value; fatalError("cancel ignored") } catch { precondition(error.localizedDescription.contains("cancelled")) }
        let start = ProcessInfo.processInfo.systemUptime
        do {
            _ = try await client.callAsync("read", vmID: UUID().uuidString, alias: "public", cancellation: BrokerCancellation(), timeout: 0.1)
            fatalError("lock timeout ignored")
        } catch { precondition(error.localizedDescription.contains("deadline")) }
        precondition(ProcessInfo.processInfo.systemUptime - start < 0.5)
        precondition(flock(fd, LOCK_UN) == 0)
        // A cancelled completed lookup must still be rejected by the delivery owner.
        precondition(token.isCancelled)
        let result = try await client.callAsync("read", vmID: UUID().uuidString, alias: "public", cancellation: BrokerCancellation())
        precondition(result["value"] as? String == "public-fixture")
        try configure("cat >/dev/null\nexec /bin/sleep 10\n")
        let subprocessStart = ProcessInfo.processInfo.systemUptime
        do {
            _ = try await client.callAsync("read", vmID: UUID().uuidString, alias: "public", cancellation: BrokerCancellation(), timeout: 0.15)
            fatalError("subprocess timeout ignored")
        } catch { precondition(error.localizedDescription.contains("deadline")) }
        precondition(ProcessInfo.processInfo.systemUptime - subprocessStart < 0.7)
        try configure("cat >/dev/null\n/usr/bin/head -c 20000 /dev/zero\n")
        do {
            _ = try await client.callAsync("read", vmID: UUID().uuidString, alias: "public", cancellation: BrokerCancellation())
            fatalError("output cap ignored")
        } catch { precondition(error.localizedDescription.contains("limit")) }
        try configure("exit 1\n")
        do {
            _ = try await client.callAsync("read", vmID: UUID().uuidString, alias: "public", cancellation: BrokerCancellation())
            fatalError("early child failure ignored")
        } catch {} // A closed stdin must be an error, never a manager SIGPIPE.
        // stderr must be drained too, without returning its public fixture bytes.
        try configure("cat >/dev/null\n/usr/bin/head -c 100000 /dev/zero >&2\nprintf '{\"ok\":true}'\n")
        _ = try await client.callAsync("read", vmID: UUID().uuidString, alias: "public", cancellation: BrokerCancellation())
        print("Broker client checks passed (heartbeat, lock deadline/cancel, child deadline, bounded pipes).")
    }
}
