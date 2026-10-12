import Foundation
import Security

/// Injectable persistence boundary; tests never initialize or access Keychain.
protocol BrokerBackend {
    func accounts() throws -> [String]
    func add(_ entry: SecretEntry) throws
    func read(account: String) throws -> String
    func remove(account: String) throws
}

private struct KeychainBrokerBackend: BrokerBackend {
    let instance: String
    init(instance: String) throws {
        guard UUID(uuidString: instance)?.uuidString == instance else { throw BrokerFailure("Invalid broker instance UUID.") }
        guard VirtyDisableLegacyAuthenticationUI() == errSecSuccess else { throw BrokerFailure("Cannot disable Keychain authentication UI.") }
        self.instance = instance
    }
    private var query: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: "com.virty.broker." + instance,
         kSecUseAuthenticationUI as String: VirtyLegacyAuthenticationUIFail()!.takeUnretainedValue()]
    }
    private func check(_ status: OSStatus) throws {
        guard status == errSecSuccess else { throw BrokerFailure("Broker Keychain access unavailable (OSStatus \(status)); no UI requested.") }
    }

    func add(_ entry: SecretEntry) throws {
        try entry.validate()
        var item = query
        item.removeValue(forKey: kSecUseAuthenticationUI as String)
        item[kSecAttrAccount as String] = entry.account
        item[kSecAttrLabel as String] = "Virty secret (\(entry.vmID)/\(entry.alias))"
        item[kSecValueData as String] = Data(entry.value.utf8)
        try check(SecItemAdd(item as CFDictionary, nil))
    }

    func read(account: String) throws -> String {
        var item = query
        item[kSecAttrAccount as String] = account
        item[kSecReturnData as String] = true
        item[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        try check(SecItemCopyMatching(item as CFDictionary, &result))
        guard let data = result as? Data, let value = String(data: data, encoding: .utf8) else {
            throw BrokerFailure("Stored secret has an unsupported encoding.")
        }
        return value
    }

    func accounts() throws -> [String] {
        var item = query
        item[kSecReturnAttributes as String] = true
        item[kSecMatchLimit as String] = kSecMatchLimitAll
        var result: CFTypeRef?
        let status = SecItemCopyMatching(item as CFDictionary, &result)
        if status == errSecItemNotFound { return [] }
        try check(status)
        guard let rows = result as? [[String: Any]] else { throw BrokerFailure("Unsupported broker catalog.") }
        return try rows.map { row in
            guard let account = row[kSecAttrAccount as String] as? String else { throw BrokerFailure("Unsupported broker account.") }
            return account
        }
    }

    func remove(account: String) throws {
        var item = query
        item[kSecAttrAccount as String] = account
        try check(SecItemDelete(item as CFDictionary))
    }
}

struct BrokerStore {
    private let backend: any BrokerBackend

    init(instance: String) throws {
        backend = try KeychainBrokerBackend(instance: instance)
    }

    init(backend: any BrokerBackend) {
        self.backend = backend
    }

    private func capacity(_ count: Int) throws {
        guard count <= SecretArchive.entryLimit else {
            throw BrokerFailure("Broker catalog capacity reached; remove unused aliases or checkpoints before adding secrets.")
        }
    }

    func put(_ entry: SecretEntry) throws {
        try entry.validate()
        try capacity(aliases().count + 1)
        try backend.add(entry)
    }

    func read(vmID: String, alias: String) throws -> String {
        let key = SecretEntry(vmID: vmID, alias: alias, value: "validation")
        try key.validate()
        return try backend.read(account: key.account)
    }

    // Legacy catalogs may exceed the write limit. Enumeration and cleanup must
    // remain available so one extra item cannot disable the whole namespace.
    func aliases() throws -> [SecretEntry] {
        let accounts = try backend.accounts()
        guard Set(accounts).count == accounts.count else { throw BrokerFailure("Duplicate broker accounts.") }
        return try accounts.map { account in
            let parts = account.split(separator: "/", omittingEmptySubsequences: false)
            guard parts.count == 2 else { throw BrokerFailure("Unsupported broker account.") }
            let entry = SecretEntry(vmID: String(parts[0]), alias: String(parts[1]), value: "validation")
            try entry.validate()
            return entry
        }.sorted { $0.account < $1.account }
    }

    func entries() throws -> [SecretEntry] {
        try aliases().map { SecretEntry(vmID: $0.vmID, alias: $0.alias, value: try read(vmID: $0.vmID, alias: $0.alias)) }
    }

    func remove(vmID: String, alias: String) throws {
        let key = SecretEntry(vmID: vmID, alias: alias, value: "validation")
        try key.validate()
        try backend.remove(account: key.account)
    }

    func generate(vmID: String, alias: String) throws {
        try SecretEntry(vmID: vmID, alias: alias, value: "validation").validate()
        try capacity(aliases().count + 1)
        let alphabet = Array("abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789".utf8)
        let ceiling = 256 - 256 % alphabet.count
        var value: [UInt8] = []
        while value.count < 24 {
            var random = [UInt8](repeating: 0, count: 64)
            guard SecRandomCopyBytes(kSecRandomDefault, random.count, &random) == errSecSuccess else {
                throw BrokerFailure("Cannot generate a random secret.")
            }
            for byte in random where Int(byte) < ceiling {
                value.append(alphabet[Int(byte) % alphabet.count])
                if value.count == 24 { break }
            }
        }
        try put(SecretEntry(vmID: vmID, alias: alias, value: String(bytes: value, encoding: .utf8)!))
    }

    private func scopedEntries(_ vmID: String, catalog: [SecretEntry]) throws -> [SecretEntry] {
        try SecretEntry(vmID: vmID, alias: "vm-login", value: "validation").validate()
        return try catalog.filter { $0.vmID == vmID }.map {
            SecretEntry(vmID: vmID, alias: $0.alias, value: try read(vmID: vmID, alias: $0.alias))
        }
    }

    /// Values stay inside the signed broker. Only alias metadata leaves this operation.
    func copyVM(source: String, target: String, replace: Bool) throws -> [String] {
        guard source != target else { throw BrokerFailure("Secret copy requires distinct VM UUIDs.") }
        let catalog = try aliases()
        let sourceEntries = try scopedEntries(source, catalog: catalog)
        let previous = try scopedEntries(target, catalog: catalog)
        guard replace || previous.isEmpty else { throw BrokerFailure("Secret copy target must be empty.") }
        let desired = sourceEntries.map { SecretEntry(vmID: target, alias: $0.alias, value: $0.value) }
        // Check the entire final catalog before deleting a single target item.
        try capacity(catalog.count - previous.count + desired.count)
        for entry in desired + previous { try entry.validate() }
        do {
            try removeVM(target)
            for entry in desired { try backend.add(entry) }
            for entry in desired {
                guard try read(vmID: target, alias: entry.alias) == entry.value else {
                    throw BrokerFailure("Secret copy verification failed.")
                }
            }
        } catch {
            do {
                try removeVM(target)
                // Rollback must also work when a legacy catalog was oversized.
                for entry in previous { try backend.add(entry) }
            } catch { throw BrokerFailure("Secret copy failed; target needs recovery from its retained checkpoint.") }
            throw BrokerFailure("Secret copy failed; previous target restored.")
        }
        return desired.map { $0.alias }.sorted()
    }

    func removeVM(_ vmID: String) throws {
        try SecretEntry(vmID: vmID, alias: "vm-login", value: "validation").validate()
        for entry in try aliases().filter({ $0.vmID == vmID }) {
            try remove(vmID: vmID, alias: entry.alias)
        }
    }

    func restore(_ entries: [SecretEntry]) throws {
        try SecretArchive.validate(entries)
        guard try aliases().isEmpty else { throw BrokerFailure("Restore requires a fresh, empty broker namespace.") }
        var written: [SecretEntry] = []
        do {
            for entry in entries { try backend.add(entry); written.append(entry) }
            for entry in entries {
                guard try read(vmID: entry.vmID, alias: entry.alias) == entry.value else { throw BrokerFailure("Restore verification failed.") }
            }
        } catch {
            var cleanupFailed = false
            for entry in written {
                do { try remove(vmID: entry.vmID, alias: entry.alias) } catch { cleanupFailed = true }
            }
            throw BrokerFailure(cleanupFailed ? "Restore failed; some target items need cleanup." : "Restore failed; target changes were rolled back.")
        }
    }
}
