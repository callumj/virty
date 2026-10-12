import Foundation
import CryptoKit
import Darwin

@main struct OperationLedgerTests {
    static func main() throws {
        if CommandLine.arguments.count == 3, CommandLine.arguments[1] == "--validate-request-id" {
            print(OperationLedger.validRequestID(CommandLine.arguments[2]) ? "true" : "false")
            return
        }
        struct Case: Decodable { let value: String; let valid: Bool }
        let cases = try JSONDecoder().decode([Case].self, from: Data(contentsOf: URL(fileURLWithPath: "Tests/request-id-cases.json")))
        for item in cases { precondition(OperationLedger.validRequestID(item.value) == item.valid) }
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("virty-ledger-test-\(UUID().uuidString)")
        try privateDirectory(root)
        defer { try? FileManager.default.removeItem(at: root) }
        func expect(_ condition: @autoclosure () throws -> Bool) throws { let value = try condition(); precondition(value) }
        func rejected(_ body: () throws -> Void) { do { try body(); fatalError("expected refusal") } catch {} }
        let ledger = try OperationLedger(home: root)
        // Find two IDs differing only in case with the same shard: raw filenames
        // would collide on default APFS even though manager IDs are case-sensitive.
        var casePair: (String, String)?
        for index in 0..<10000 {
            let upper = "CASE-\(index)", lower = upper.lowercased()
            if Array(SHA256.hash(data: Data(upper.utf8))).first == Array(SHA256.hash(data: Data(lower.utf8))).first {
                casePair = (upper, lower); break
            }
        }
        guard let (upper, lower) = casePair else { fatalError("case collision fixture") }
        try ledger.begin(upper, command: "type-text", alias: nil)
        try ledger.begin(lower, command: "type-text", alias: nil)
        try ledger.finish(upper, status: "typed")
        try expect(ledger.lookup(upper)?.status == "typed")
        try expect(ledger.lookup(lower)?.status == "started")
        try ledger.begin("old-ID", command: "type-secret", alias: "public-alias")
        try expect(ledger.lookup("old-ID")?.status == "started")
        let restarted = try OperationLedger(home: root)
        try expect(restarted.lookup("old-ID")?.status == "uncertain")
        rejected { try restarted.begin("old-ID", command: "type-secret", alias: "public-alias") }
        try ledger.begin("response-lost", command: "type-text", alias: nil)
        try ledger.finish("response-lost", status: "typed")
        try expect(restarted.lookup("response-lost")?.status == "typed")
        rejected { try restarted.begin("response-lost", command: "type-text", alias: nil) }
        func recordBytes() throws -> Int {
            let iterator = FileManager.default.enumerator(at: root.appendingPathComponent("operations"), includingPropertiesForKeys: [.fileSizeKey])!
            var count = 0
            for case let url as URL in iterator where url.pathExtension == "json" {
                count += try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
            }
            return count
        }
        for index in 0..<100 { try ledger.begin("key-\(index)", command: "type-text", alias: nil); try ledger.finish("key-\(index)", status: "typed") }
        let first = try recordBytes()
        for index in 100..<200 { try ledger.begin("key-\(index)", command: "type-text", alias: nil); try ledger.finish("key-\(index)", status: "typed") }
        let second = try recordBytes()
        precondition(second > first && second < first * 21 / 10)
        try expect(OperationLedger(home: root).lookup("old-ID")?.status == "uncertain")
        // Guest-state replacement leaves the external operation index untouched.
        try Data("restored-public-metadata".utf8).write(to: root.appendingPathComponent("vm.json"))
        try expect(OperationLedger(home: root).lookup("response-lost")?.status == "typed")
        let legacyHome = root.appendingPathComponent("legacy")
        try privateDirectory(legacyHome)
        let legacy = legacyHome.appendingPathComponent("operations.json")
        let data = try JSONSerialization.data(withJSONObject: [
            "legacy-complete": ["command": "type-text", "status": "typed"],
            "legacy-partial": ["command": "type-secret", "status": "started", "alias": "public-alias"]])
        try data.write(to: legacy)
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: legacy.path)
        let migrated = try OperationLedger(home: legacyHome)
        try expect(migrated.lookup("legacy-complete")?.status == "typed")
        try expect(migrated.lookup("legacy-partial")?.status == "uncertain")
        try expect(Data(contentsOf: legacy) == data)
        // Simulate death after an arbitrary migrated record but before the commit marker.
        let marker = legacyHome.appendingPathComponent("operations/migration.json")
        try FileManager.default.removeItem(at: marker)
        let iterator = FileManager.default.enumerator(at: legacyHome.appendingPathComponent("operations"), includingPropertiesForKeys: nil)!
        let records = iterator.compactMap { $0 as? URL }.filter { $0.pathExtension == "json" }
        try FileManager.default.removeItem(at: records[0])
        let recovered = try OperationLedger(home: legacyHome)
        try expect(recovered.lookup("legacy-complete")?.status == "typed")
        try expect(recovered.lookup("legacy-partial")?.status == "uncertain")
        rejected { try recovered.begin("legacy-partial", command: "type-secret", alias: "public-alias") }
        // Older controllers atomically rewrite the retained source. Re-upgrade must
        // fail closed rather than silently ignoring those newer accepted IDs.
        try data.write(to: legacy, options: .atomic)
        rejected { _ = try OperationLedger(home: legacyHome) }
        // Corrupt marker/records fail closed instead of treating accepted IDs as new.
        try Data("broken".utf8).write(to: marker)
        rejected { _ = try OperationLedger(home: legacyHome) }
        let corrupt = records.first { FileManager.default.fileExists(atPath: $0.path) }!
        let corruptID = try JSONDecoder().decode(OperationLedger.Record.self, from: Data(contentsOf: corrupt)).requestID
        try Data("broken".utf8).write(to: corrupt)
        rejected { _ = try recovered.lookup(corruptID) }
        rejected { _ = try recovered.lookup("../invalid") }
        print("Operation ledger checks passed (permanent IDs, restart uncertainty, linear storage, repeatable migration, corruption, shared ID contract).")
    }
}
