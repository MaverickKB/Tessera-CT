import Foundation
import Security

/// Optional access token storage.
/// Accessibility is after-first-unlock-this-device-only so a locked screen can still use a saved token.
enum KeychainStore {
    private static let service = ProductManifest.bundleIdentifier
    private static let account = "server-access-token"

    static func loadToken() -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne
        ]

        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound {
            return nil
        }
        if status != errSecSuccess {
            return nil
        }
        guard let data = item as? Data else {
            return nil
        }
        let text = String(data: data, encoding: .utf8)
        if let text, text.isEmpty == false {
            return text
        }
        return nil
    }

    static func saveToken(_ token: String?) throws {
        try writeToken(token, update: SecItemUpdate,
                       add: { SecItemAdd($0, nil) }, delete: SecItemDelete)
    }

    static func writeToken(_ token: String?,
                           update: (CFDictionary, CFDictionary) -> OSStatus,
                           add: (CFDictionary) -> OSStatus,
                           delete: (CFDictionary) -> OSStatus) throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account
        ]
        guard let token, !token.isEmpty else {
            let status = delete(query as CFDictionary)
            guard status == errSecSuccess || status == errSecItemNotFound else {
                throw KeychainError.saveFailed(status)
            }
            return
        }
        guard let data = token.data(using: .utf8) else {
            throw KeychainError.encodingFailed
        }
        let attributes: [String: Any] = [
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
            kSecValueData as String: data
        ]
        // Updating in place preserves the old token when storage refuses the edit.
        // Never delete a working credential before attempting its replacement.
        var status = update(query as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            let addQuery = query.merging(attributes) { _, new in new }
            status = add(addQuery as CFDictionary)
        }
        guard status == errSecSuccess else {
            throw KeychainError.saveFailed(status)
        }
    }

}

enum KeychainError: Error {
    case encodingFailed
    case saveFailed(OSStatus)
}
