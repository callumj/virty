import Foundation
import Security
import LocalAuthentication

/// No value-returning control request exists. New VMs delegate storage to the broker.
struct SecretStore {
    let vmID: String
    let mode: String
    private var query: [String: Any] {
        var value: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: "com.virty.vm.\(vmID)",
            kSecAttrAccount as String: "vm-login"
        ]
        if mode == "data-protection" { value[kSecUseDataProtectionKeychain as String] = true }
        return value
    }

    func generate() throws {
        if mode == "broker" {
            _ = try BrokerClient().call("generate", vmID: vmID, alias: "vm-login")
            return
        }
        if mode == "login" { try check(VirtyDisableLegacyAuthenticationUI()) }
        let alphabet = Array("abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789".utf8)
        var random = [UInt8](repeating: 0, count: 24)
        guard SecRandomCopyBytes(kSecRandomDefault, random.count, &random) == errSecSuccess else {
            throw Failure("Cannot generate disposable VM password.")
        }
        let secret = Data(random.map { alphabet[Int($0) % alphabet.count] })
        var item = query
        item[kSecValueData as String] = secret
        item[kSecAttrLabel as String] = "Virty disposable guest login (\(vmID))"
        if mode == "data-protection" {
            item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        }
        try check(SecItemAdd(item as CFDictionary, nil))
    }

    func readForTyping(alias: String = "vm-login") throws -> String {
        if mode == "broker" {
            guard let value = try BrokerClient().call("read", vmID: vmID, alias: alias)["value"] as? String else {
                throw Failure("Secret broker returned an unsupported value format.")
            }
            return value
        }
        guard alias == "vm-login" else { throw Failure("Legacy manager supports only the vm-login alias.") }
        if mode == "login" { try check(VirtyDisableLegacyAuthenticationUI()) }
        var item = query
        if mode == "data-protection" {
            let context = LAContext(); context.interactionNotAllowed = true
            item[kSecUseAuthenticationContext as String] = context
        } else {
            // File-based SecItem shim: authentication must fail instead of prompting.
            // Its UI policy differs from the data protection LAContext policy.
            item[kSecUseAuthenticationUI as String] = VirtyLegacyAuthenticationUIFail()!.takeUnretainedValue()
        }
        item[kSecReturnData as String] = true
        item[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        try check(SecItemCopyMatching(item as CFDictionary, &result))
        guard let data = result as? Data, let text = String(data: data, encoding: .utf8) else {
            throw Failure("VM login item has an unsupported format.")
        }
        return text
    }

    /// Broker/Keychain work must never occupy the manager's main actor.
    func readForTypingAsync(alias: String = "vm-login", cancellation: BrokerCancellation) async throws -> String {
        if mode == "broker" {
            guard let value = try await BrokerClient().callAsync("read", vmID: vmID, alias: alias,
                                                                 cancellation: cancellation)["value"] as? String else {
                throw Failure("Secret broker returned an unsupported value format.")
            }
            return value
        }
        // Legacy Keychain APIs have no cancellable subprocess. Keep them off-main;
        // cancel is rechecked before any value can be delivered to the guest.
        let value = try await Task.detached { try self.readForTyping(alias: alias) }.value
        guard !cancellation.isCancelled else { throw Failure("Secret access cancelled.") }
        return value
    }

    private func check(_ status: OSStatus) throws {
        guard status == errSecSuccess else {
            throw Failure("VM Keychain operation unavailable (OSStatus \(status)); no authentication UI requested.")
        }
    }
}
