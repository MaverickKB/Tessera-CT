import Foundation

struct SessionSummary: Codable, Identifiable, Hashable {
    var id: String { slug }
    let slug: String
    let title: String
    let archived: Bool
    let messages: Int?
    let updated: String?
    let created: String?
    let backend: String?
}

struct VoiceStatusReport: Codable {
    let enabled: Bool?
    let configured: Bool?
    let active_count: Int?
    let voices: [String]?
    let voice: String?
}

struct StatusResponse: Codable {
    let voice: VoiceStatusReport?
    let agent_name: String?
}

struct VoiceStartRequest: Codable {
    let voice: String?
}

struct VoiceStartResponse: Codable {
    let voice_id: String
    let websocket_url: String
    let protocols: [String]
    let expires_at: Double
    let session_expires_at: Double
    let max_session_seconds: Double
    let audio_sample_rate: Double
    let audio_format: String
    let session_update: JSONValue
}

struct VoiceEventsRequest: Codable {
    let voice_id: String
    let events: [JSONValue]
}

struct VoiceEventsResponse: Codable {
    let accepted: Int?
    let messages: [JSONValue]?
    let send: [JSONValue]?
    let session_turn: SessionTurnRef?
    var poll_after_ms: Int? = nil
}

struct SessionTurnRef: Codable, Equatable {
    let mode: String?
    let turn_id: String?
}

struct SpeechChunk: Codable {
    let audio_base64: String
    let next_offset: Int
}

struct VoiceStopRequest: Codable {
    let voice_id: String
}

struct APIErrorBody: Codable {
    let error: String?
    let error_category: String?
}

/// Minimal JSON tree so protocol objects stay flexible without inventing model fields.
enum JSONValue: Codable, Equatable {
    case object([String: JSONValue])
    case array([JSONValue])
    case string(String)
    case number(Double)
    case bool(Bool)
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
            return
        }
        if let value = try? container.decode(Bool.self) {
            self = .bool(value)
            return
        }
        if let value = try? container.decode(Double.self) {
            self = .number(value)
            return
        }
        if let value = try? container.decode(String.self) {
            self = .string(value)
            return
        }
        if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
            return
        }
        if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
            return
        }
        throw DecodingError.dataCorruptedError(in: container, debugDescription: "Unsupported JSON")
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .null:
            try container.encodeNil()
        case .bool(let value):
            try container.encode(value)
        case .number(let value):
            try container.encode(value)
        case .string(let value):
            try container.encode(value)
        case .object(let value):
            try container.encode(value)
        case .array(let value):
            try container.encode(value)
        }
    }

    var objectValue: [String: JSONValue]? {
        if case .object(let value) = self {
            return value
        }
        return nil
    }

    var arrayValue: [JSONValue]? {
        if case .array(let value) = self {
            return value
        }
        return nil
    }

    var stringValue: String? {
        if case .string(let value) = self {
            return value
        }
        return nil
    }

    var numberValue: Double? {
        if case .number(let value) = self {
            return value
        }
        return nil
    }

    var boolValue: Bool? {
        if case .bool(let value) = self {
            return value
        }
        return nil
    }

    subscript(key: String) -> JSONValue? {
        objectValue?[key]
    }

    static func from(_ any: Any?) -> JSONValue {
        if any == nil || any is NSNull {
            return .null
        }
        if let value = any as? String {
            return .string(value)
        }
        if let value = any as? Bool {
            return .bool(value)
        }
        if let value = any as? NSNumber {
            // Bool is an NSNumber in some bridges; detect it first via objCType when needed.
            let type = String(cString: value.objCType)
            if type == "c" || type == "B" {
                return .bool(value.boolValue)
            }
            return .number(value.doubleValue)
        }
        if let value = any as? [String: Any] {
            var object: [String: JSONValue] = [:]
            for (key, nested) in value {
                object[key] = JSONValue.from(nested)
            }
            return .object(object)
        }
        if let value = any as? [Any] {
            var array: [JSONValue] = []
            for nested in value {
                array.append(JSONValue.from(nested))
            }
            return .array(array)
        }
        return .null
    }

    func toAny() -> Any {
        switch self {
        case .null:
            return NSNull()
        case .bool(let value):
            return value
        case .number(let value):
            return value
        case .string(let value):
            return value
        case .object(let value):
            var object: [String: Any] = [:]
            for (key, nested) in value {
                object[key] = nested.toAny()
            }
            return object
        case .array(let value):
            var array: [Any] = []
            for nested in value {
                array.append(nested.toAny())
            }
            return array
        }
    }

    func jsonData() throws -> Data {
        try JSONSerialization.data(withJSONObject: toAny(), options: [])
    }
}
