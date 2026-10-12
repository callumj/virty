import Foundation
import Security

@main
enum SigningProbe {
    static func main() {
        #if SECOND_BUILD
        let build = "second"
        #else
        let build = "first"
        #endif

        let args = Array(CommandLine.arguments.dropFirst())
        guard args.count == 2, UUID(uuidString: args[1]) != nil else { exit(2) }
        let store = SecretStore(vmID: args[1], mode: "login")
        do {
            switch args[0] {
            case "create": try store.generate()
            case "read":
                let secret = try store.readForTyping()
                guard secret.utf8.count == 24 else { throw Failure("Unexpected test item format.") }
            case "inspect":
                var status: OSStatus = errSecSuccess
                guard let result = VirtySigningProbePartitions("com.virty.vm.\(args[1])" as CFString, &status) else {
                    throw Failure("Test ACL inspection returned no metadata.")
                }
                let partitions = result.takeRetainedValue()
                guard status == errSecSuccess else { throw Failure("Test ACL inspection failed (\(status)).") }
                emit(["ok": true, "partitions": partitions])
                return
            case "delete":
                let status = SecItemDelete([
                    kSecClass as String: kSecClassGenericPassword,
                    kSecAttrService as String: "com.virty.vm.\(args[1])",
                    kSecAttrAccount as String: "vm-login"
                ] as CFDictionary)
                guard status == errSecSuccess else { throw Failure("Test item cleanup failed (\(status)).") }
            default: exit(2)
            }
            emit(["ok": true, "build": build, "action": args[0]])
        } catch {
            emit(["ok": false, "build": build, "error": error.localizedDescription])
            exit(1)
        }
    }
}
