import Foundation

@main
enum ArchiveTests {
    static func rejected(_ work: () throws -> Void) {
        do { try work(); fatalError("Expected archive operation to be rejected.") } catch { }
    }
    static func main() throws {
        let entries = [SecretEntry(vmID: UUID().uuidString, alias: "vm-login", value: "public-test-value")]
        let passphrase = "public-test-passphrase"
        let archive = try SecretArchive.seal(entries, passphrase: passphrase)
        let recovered = try SecretArchive.open(archive, passphrase: passphrase)
        precondition(recovered.count == 1 && recovered[0].value == entries[0].value)
        precondition(!String(data: archive, encoding: .utf8)!.contains(entries[0].value))
        rejected { _ = try SecretArchive.open(archive, passphrase: "different-public-passphrase") }
        var envelope = try JSONSerialization.jsonObject(with: archive) as! [String: Any]
        var sealed = Data(base64Encoded: envelope["sealed"] as! String)!
        sealed[sealed.count - 1] ^= 1
        envelope["sealed"] = sealed.base64EncodedString()
        let tampered = try JSONSerialization.data(withJSONObject: envelope)
        rejected { _ = try SecretArchive.open(tampered, passphrase: passphrase) }
        rejected { _ = try SecretArchive.seal(entries + entries, passphrase: passphrase) }
        rejected { _ = try SecretArchive.seal(entries, passphrase: "short") }
        rejected { try SecretEntry(vmID: "invalid", alias: "vm-login", value: "public").validate() }
        rejected { try SecretEntry(vmID: UUID().uuidString, alias: "../escape", value: "public").validate() }
        print("Archive checks passed: authenticated round trip, wrong key, tampering, duplicates, and input bounds.")
    }
}
