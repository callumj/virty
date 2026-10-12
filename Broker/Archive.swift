import Foundation
import CryptoKit

struct BrokerFailure: Error, LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

struct SecretEntry: Codable {
    let vmID: String
    let alias: String
    let value: String
    var account: String { vmID + "/" + alias }

    func validate() throws {
        guard UUID(uuidString: vmID)?.uuidString == vmID,
              !alias.isEmpty, alias.utf8.count <= 64,
              alias.utf8.allSatisfy({ (48...57).contains($0) || (65...90).contains($0) ||
                  (97...122).contains($0) || [45, 46, 95].contains($0) }),
              !value.isEmpty, value.utf8.count <= 4096 else {
            throw BrokerFailure("Invalid secret entry; UUID, alias, or value bounds failed.")
        }
    }
}

enum SecretArchive {
    static let limit = 8 * 1024 * 1024
    static let entryLimit = 1000
    private static let context = Data("com.virty.secret-archive.v1".utf8)
    private struct Contents: Codable { let version: Int; let entries: [SecretEntry] }
    private struct Envelope: Codable { let version: Int; let salt: Data; let sealed: Data }

    private static func key(_ passphrase: String, salt: Data) throws -> SymmetricKey {
        let password = Data(passphrase.utf8)
        guard (12...4096).contains(password.count), salt.count == 16 else {
            throw BrokerFailure("Backup passphrase must contain 12–4096 UTF-8 bytes.")
        }
        var result = Data(count: 32)
        let status = result.withUnsafeMutableBytes { output in
            password.withUnsafeBytes { input in
                salt.withUnsafeBytes { saltBytes in
                    VirtyDeriveBackupKey(input.baseAddress, input.count, saltBytes.baseAddress,
                                         saltBytes.count, output.baseAddress)
                }
            }
        }
        guard status == 0 else { throw BrokerFailure("Backup key derivation failed.") }
        return SymmetricKey(data: result)
    }

    static func validate(_ entries: [SecretEntry]) throws {
        guard entries.count <= entryLimit, Set(entries.map(\.account)).count == entries.count else {
            throw BrokerFailure("Archive contains too many entries or duplicate aliases.")
        }
        for entry in entries { try entry.validate() }
    }

    static func seal(_ entries: [SecretEntry], passphrase: String) throws -> Data {
        try validate(entries)
        var salt = Data(count: 16)
        let status = salt.withUnsafeMutableBytes { SecRandomCopyBytes(kSecRandomDefault, $0.count, $0.baseAddress!) }
        guard status == errSecSuccess else { throw BrokerFailure("Cannot generate archive salt.") }
        let plaintext = try JSONEncoder().encode(Contents(version: 1, entries: entries))
        guard plaintext.count <= limit / 2 else { throw BrokerFailure("Secret archive exceeds its size bound.") }
        let box = try AES.GCM.seal(plaintext, using: key(passphrase, salt: salt), authenticating: context)
        guard let combined = box.combined else { throw BrokerFailure("Cannot encode encrypted archive.") }
        return try JSONEncoder().encode(Envelope(version: 1, salt: salt, sealed: combined))
    }

    static func open(_ data: Data, passphrase: String) throws -> [SecretEntry] {
        guard data.count <= limit else { throw BrokerFailure("Encrypted archive exceeds its size bound.") }
        do {
            let envelope = try JSONDecoder().decode(Envelope.self, from: data)
            guard envelope.version == 1, envelope.salt.count == 16 else {
                throw BrokerFailure("Unsupported archive format.")
            }
            let box = try AES.GCM.SealedBox(combined: envelope.sealed)
            let plaintext = try AES.GCM.open(box, using: key(passphrase, salt: envelope.salt), authenticating: context)
            guard plaintext.count <= limit / 2 else { throw BrokerFailure("Archive contents exceed their size bound.") }
            let contents = try JSONDecoder().decode(Contents.self, from: plaintext)
            guard contents.version == 1 else { throw BrokerFailure("Unsupported archive contents.") }
            try validate(contents.entries)
            return contents.entries
        } catch {
            // Never echo decoded content, passphrases, or cryptographic error details.
            throw BrokerFailure("Archive authentication or validation failed; no items were restored.")
        }
    }
}
