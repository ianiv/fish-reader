import Foundation
import Security

/// Connection settings shared by the app and the share extension through the app group: the server URL in the
/// group's UserDefaults, the token in the group's keychain.
enum ReaderSettings {
    static let appGroup: String = Bundle.main.object(forInfoDictionaryKey: "AppGroup") as? String ?? "group.ca.blogaholics.FishReader"
    static var defaults: UserDefaults { UserDefaults(suiteName: appGroup) ?? .standard }

    static var serverURL: String {
        get { defaults.string(forKey: "serverURL") ?? "" }
        set { defaults.set(newValue.trimmingCharacters(in: .whitespacesAndNewlines), forKey: "serverURL") }
    }

    static var voice: String? {
        get { defaults.string(forKey: "voice") }
        set { defaults.set(newValue, forKey: "voice") }
    }

    static var token: String {
        get { Keychain.read("token") ?? "" }
        set { Keychain.write("token", newValue.trimmingCharacters(in: .whitespacesAndNewlines)) }
    }

    static var isConfigured: Bool { !serverURL.isEmpty && !token.isEmpty }
}

enum Keychain {
    private static func query(_ key: String, group: Bool) -> [String: Any] {
        var q: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                kSecAttrService as String: "fish-reader",
                                kSecAttrAccount as String: key]
        if group { q[kSecAttrAccessGroup as String] = ReaderSettings.appGroup }
        return q
    }

    static func read(_ key: String) -> String? {
        for group in [true, false] {  // the group needs the entitlement (signed builds); fall back without it
            var q = query(key, group: group)
            q[kSecReturnData as String] = true
            var out: AnyObject?
            if SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let d = out as? Data {
                return String(data: d, encoding: .utf8)
            }
        }
        return nil
    }

    static func write(_ key: String, _ value: String) {
        for group in [true, false] {
            let q = query(key, group: group)
            SecItemDelete(q as CFDictionary)
            var add = q
            add[kSecValueData as String] = Data(value.utf8)
            add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
            if SecItemAdd(add as CFDictionary, nil) == errSecSuccess { return }
        }
    }
}
