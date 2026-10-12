import Foundation

private final class MemoryBrokerBackend: BrokerBackend {
    var values: [String: String]
    var mutations = 0
    var beforeAdd: ((SecretEntry) throws -> Void)?
    var readOverride: ((String) throws -> String?)?
    var beforeRemove: ((String) throws -> Void)?

    init(_ entries: [SecretEntry] = []) {
        values = Dictionary(uniqueKeysWithValues: entries.map { ($0.account, $0.value) })
    }
    func accounts() throws -> [String] { Array(values.keys) }
    func add(_ entry: SecretEntry) throws {
        try beforeAdd?(entry)
        guard values[entry.account] == nil else { throw BrokerFailure("Fixture duplicate account.") }
        values[entry.account] = entry.value
        mutations += 1
    }
    func read(account: String) throws -> String {
        if let replacement = try readOverride?(account) { return replacement }
        guard let value = values[account] else { throw BrokerFailure("Fixture missing account.") }
        return value
    }
    func remove(account: String) throws {
        try beforeRemove?(account)
        guard values.removeValue(forKey: account) != nil else { throw BrokerFailure("Fixture missing account.") }
        mutations += 1
    }
}

@main
enum BrokerStoreTests {
    static let source = "00000000-0000-0000-0000-000000000001"
    static let target = "00000000-0000-0000-0000-000000000002"
    static let other = "00000000-0000-0000-0000-000000000003"

    static func entries(_ count: Int, vmID: String = other, prefix: String = "alias") -> [SecretEntry] {
        (0..<count).map { SecretEntry(vmID: vmID, alias: "\(prefix)-\($0)", value: "public-fixture-value-\($0)") }
    }
    static func check(_ value: Bool) { precondition(value) }
    static func rejected(_ expected: String, _ work: () throws -> Void) {
        do { try work(); fatalError("Expected rejection: \(expected)") }
        catch { precondition(error.localizedDescription.contains(expected), "Unexpected fixture rejection: \(error)") }
    }

    static func writesAndEnumeration() throws {
        for count in [999, 1000, 1001] {
            for generate in [false, true] {
                let memory = MemoryBrokerBackend(entries(count))
                let store = BrokerStore(backend: memory)
                let original = memory.values
                let write = {
                    if generate { try store.generate(vmID: source, alias: "vm-login") }
                    else { try store.put(SecretEntry(vmID: source, alias: "vm-login", value: "public-new-value")) }
                }
                if count < SecretArchive.entryLimit {
                    try write()
                    precondition(memory.values.count == 1000 && memory.mutations == 1)
                    check(try store.read(vmID: source, alias: "vm-login").count >= 16)
                } else {
                    rejected("capacity") { try write() }
                    precondition(memory.values == original && memory.mutations == 0)
                }
                check(try store.aliases().count == memory.values.count)
                check(try store.entries().count == memory.values.count)
                try store.removeVM(other)
                check(try store.aliases().count == (count == 999 ? 1 : 0))
            }
        }
    }

    static func batchCapacityAndReplacement() throws {
        for count in [998, 999, 1000, 1001] {
            let memory = MemoryBrokerBackend(entries(2, vmID: source) + entries(count - 2))
            let store = BrokerStore(backend: memory)
            let original = memory.values
            if count == 998 {
                check(try store.copyVM(source: source, target: target, replace: false) == ["alias-0", "alias-1"])
                precondition(memory.values.count == 1000)
            } else {
                rejected("capacity") { _ = try store.copyVM(source: source, target: target, replace: false) }
                precondition(memory.values == original && memory.mutations == 0)
            }
            check(try store.read(vmID: source, alias: "alias-0") == "public-fixture-value-0")
        }

        for previousCount in [1, 2, 3] {
            let memory = MemoryBrokerBackend(entries(2, vmID: source) + entries(previousCount, vmID: target, prefix: "old") + entries(998 - previousCount))
            let store = BrokerStore(backend: memory)
            let original = memory.values
            if previousCount == 1 {
                rejected("capacity") { _ = try store.copyVM(source: source, target: target, replace: true) }
                precondition(memory.values == original && memory.mutations == 0)
            } else {
                rejected("must be empty") { _ = try store.copyVM(source: source, target: target, replace: false) }
                precondition(memory.values == original && memory.mutations == 0)
                _ = try store.copyVM(source: source, target: target, replace: true)
                precondition(memory.values.count == 1002 - previousCount)
                precondition(memory.values[target + "/old-0"] == nil)
                precondition(memory.values[target + "/alias-0"] == "public-fixture-value-0")
            }
        }
    }

    static func copyRollback() throws {
        for failure in ["add", "read", "remove"] {
            let memory = MemoryBrokerBackend(entries(2, vmID: source) + entries(2, vmID: target, prefix: "old") + entries(996))
            let store = BrokerStore(backend: memory)
            let original = memory.values
            var failed = false
            memory.beforeAdd = { entry in
                if failure == "add", !failed, entry.vmID == target, entry.alias == "alias-1" {
                    failed = true
                    throw BrokerFailure("Injected add failure.")
                }
            }
            memory.readOverride = { account in
                if failure == "read", !failed, account == target + "/alias-1" {
                    failed = true
                    return "public-corrupt-verification"
                }
                return nil
            }
            memory.beforeRemove = { account in
                if failure == "remove", !failed, account.hasPrefix(target + "/") {
                    failed = true
                    throw BrokerFailure("Injected remove failure.")
                }
            }
            rejected("previous target restored") { _ = try store.copyVM(source: source, target: target, replace: true) }
            precondition(failed && memory.values == original)
            check(try store.aliases().count == 1000)
        }

        // A replacement can reduce a legacy oversized namespace to the limit;
        // if it fails, restoring its old target must be allowed above the limit.
        let memory = MemoryBrokerBackend(entries(1, vmID: source) + entries(2, vmID: target, prefix: "old") + entries(998))
        let store = BrokerStore(backend: memory)
        let original = memory.values
        var failed = false
        memory.beforeAdd = { entry in
            if !failed, entry.vmID == target, entry.alias == "alias-0" {
                failed = true
                throw BrokerFailure("Injected legacy replacement failure.")
            }
        }
        rejected("previous target restored") { _ = try store.copyVM(source: source, target: target, replace: true) }
        precondition(memory.values == original && memory.values.count == 1001)
        check(try store.aliases().count == 1001)
        try store.removeVM(target)
        check(try store.aliases().count == 999)
    }

    static func archiveRestore() throws {
        for count in [999, 1000, 1001] {
            let memory = MemoryBrokerBackend()
            let store = BrokerStore(backend: memory)
            let incoming = entries(count)
            if count <= SecretArchive.entryLimit {
                try SecretArchive.validate(incoming)
                try store.restore(incoming)
                precondition(memory.values.count == count)
            } else {
                rejected("too many") { try SecretArchive.validate(incoming) }
                rejected("too many") { try store.restore(incoming) }
                precondition(memory.values.isEmpty && memory.mutations == 0)
            }
        }
        let memory = MemoryBrokerBackend()
        let store = BrokerStore(backend: memory)
        var failed = false
        memory.beforeAdd = { entry in
            if !failed, entry.alias == "alias-998" {
                failed = true
                throw BrokerFailure("Injected restore failure.")
            }
        }
        rejected("rolled back") { try store.restore(entries(1000)) }
        precondition(failed && memory.values.isEmpty)
        memory.beforeAdd = nil
        try store.restore(entries(1000))
        let original = memory.values
        rejected("empty broker namespace") { try store.restore(entries(1, vmID: source)) }
        precondition(memory.values == original)
    }

    static func main() throws {
        try writesAndEnumeration()
        try batchCapacityAndReplacement()
        try copyRollback()
        try archiveRestore()
        print("Broker store checks passed: capacity boundaries, batch preflight, replacement, rollback, oversized cleanup, and archive restore.")
    }
}
