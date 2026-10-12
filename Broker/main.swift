import Foundation
import Darwin

umask(0o077)
func reply(_ object: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) {
        FileHandle.standardOutput.write(data + Data([10]))
    }
}
func string(_ request: [String: Any], _ name: String) throws -> String {
    guard let value = request[name] as? String else { throw BrokerFailure("Missing broker request field.") }
    return value
}
do {
    guard CommandLine.arguments.count == 1 else { throw BrokerFailure("Broker accepts its private request on stdin only.") }
    var data = Data()
    while let chunk = try FileHandle.standardInput.read(upToCount: 65536), !chunk.isEmpty {
        data.append(chunk)
        guard data.count <= SecretArchive.limit * 2 else { throw BrokerFailure("Broker request exceeds its size bound.") }
    }
    guard let request = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw BrokerFailure("Invalid broker request.") }
    let store = try BrokerStore(instance: string(request, "instance"))
    switch try string(request, "operation") {
    case "generate":
        try store.generate(vmID: string(request, "vmID"), alias: string(request, "alias"))
        reply(["ok": true])
    case "put":
        try store.put(SecretEntry(vmID: string(request, "vmID"), alias: string(request, "alias"), value: string(request, "value")))
        reply(["ok": true])
    case "read":
        // This response goes only to the controller's anonymous pipe, never its control socket.
        reply(["ok": true, "value": try store.read(vmID: string(request, "vmID"), alias: string(request, "alias"))])
    case "list", "verify":
        let entries = try string(request, "operation") == "verify" ? store.entries() : store.aliases()
        reply(["ok": true, "entries": entries.map { ["vmID": $0.vmID, "alias": $0.alias] }])
    case "remove":
        try store.remove(vmID: string(request, "vmID"), alias: string(request, "alias"))
        reply(["ok": true])
    case "copy-vm":
        guard let replace = request["replace"] as? Bool else { throw BrokerFailure("Missing secret copy policy.") }
        let aliases = try store.copyVM(source: string(request, "sourceVM"), target: string(request, "targetVM"), replace: replace)
        reply(["ok": true, "aliases": aliases])
    case "remove-vm":
        try store.removeVM(string(request, "vmID"))
        reply(["ok": true])
    case "export":
        let archive = try SecretArchive.seal(store.entries(), passphrase: string(request, "passphrase"))
        reply(["ok": true, "archive": archive.base64EncodedString()])
    case "restore":
        guard let archive = Data(base64Encoded: try string(request, "archive")) else { throw BrokerFailure("Invalid encrypted archive.") }
        let entries = try SecretArchive.open(archive, passphrase: string(request, "passphrase"))
        try store.restore(entries)
        reply(["ok": true, "restored": entries.count])
    default: throw BrokerFailure("Unknown broker operation.")
    }
} catch {
    reply(["ok": false, "error": (error as? BrokerFailure)?.message ?? "Broker request failed; no secret details returned."])
    exit(1)
}
