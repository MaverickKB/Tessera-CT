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
        let deleteQuery: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account
        ]
        SecItemDelete(deleteQuery as CFDictionary)

        guard let token, token.isEmpty == false else {
            return
        }
        guard let data = token.data(using: .utf8) else {
            throw KeychainError.encodingFailed
        }

        let addQuery: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
            kSecValueData as String: data
        ]
        let status = SecItemAdd(addQuery as CFDictionary, nil)
        if status != errSecSuccess {
            throw KeychainError.saveFailed(status)
        }
    }
}

enum KeychainError: Error {
    case encodingFailed
    case saveFailed(OSStatus)
}
