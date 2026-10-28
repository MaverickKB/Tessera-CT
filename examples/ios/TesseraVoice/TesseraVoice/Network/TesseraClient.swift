import Foundation

/// REST client for Tessera session and voice endpoints.
/// Matches the web client's request shape. Does not call desk activity APIs.
final class TesseraClient: @unchecked Sendable {
    private let session: URLSession
    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()

    init(session: URLSession = .shared) {
        self.session = session
    }

    func fetchStatus(baseURL: URL, accessToken: String?) async throws -> StatusResponse {
        let url = try join(baseURL, path: "api/status")
        return try await get(url: url, accessToken: accessToken)
    }

    func listSessions(baseURL: URL, accessToken: String?) async throws -> [SessionSummary] {
        let url = try join(baseURL, path: "api/sessions")
        return try await get(url: url, accessToken: accessToken)
    }

    func startVoice(
        baseURL: URL,
        accessToken: String?,
        slug: String,
        voice: String?
    ) async throws -> VoiceStartResponse {
        let encodedSlug = percentEncodePath(slug)
        let url = try join(baseURL, path: "api/sessions/\(encodedSlug)/voice/start")
        var body: [String: Any] = [:]
        if let voice, voice.isEmpty == false {
            body["voice"] = voice
        }
        return try await post(url: url, accessToken: accessToken, jsonObject: body)
    }

    func postEvents(
        baseURL: URL,
        accessToken: String?,
        slug: String,
        voiceId: String,
        events: [JSONValue]
    ) async throws -> VoiceEventsResponse {
        let encodedSlug = percentEncodePath(slug)
        let url = try join(baseURL, path: "api/sessions/\(encodedSlug)/voice/events")
        var eventObjects: [Any] = []
        for event in events {
            eventObjects.append(event.toAny())
        }
        let body: [String: Any] = [
            "voice_id": voiceId,
            "events": eventObjects
        ]
        return try await post(url: url, accessToken: accessToken, jsonObject: body)
    }

    func stopVoice(
        baseURL: URL,
        accessToken: String?,
        slug: String,
        voiceId: String
    ) async throws {
        let encodedSlug = percentEncodePath(slug)
        let url = try join(baseURL, path: "api/sessions/\(encodedSlug)/voice/stop")
        let body: [String: Any] = ["voice_id": voiceId]
        let _: JSONValue = try await post(url: url, accessToken: accessToken, jsonObject: body)
    }

    func interruptSession(baseURL: URL, accessToken: String?, slug: String) async throws {
        let encodedSlug = percentEncodePath(slug)
        let url = try join(baseURL, path: "api/sessions/\(encodedSlug)/interrupt")
        let _: JSONValue = try await post(url: url, accessToken: accessToken, jsonObject: [:])
    }

    func fetchRecentMessages(baseURL: URL, accessToken: String?, slug: String) async throws -> [JSONValue] {
        let encodedSlug = percentEncodePath(slug)
        let url = try join(baseURL, path: "api/sessions/\(encodedSlug)/messages?tail=40")
        return try await get(url: url, accessToken: accessToken)
    }

    func fetchSpeech(baseURL: URL, accessToken: String?, slug: String, messageIndex: Int, offset: Int) async throws -> SpeechChunk {
        let encodedSlug = percentEncodePath(slug)
        let url = try join(baseURL, path: "api/sessions/\(encodedSlug)/speech")
        let body: [String: Any] = [
            "message_i": messageIndex,
            "offset": offset,
            "limit": 300,
            "encoding": "json"
        ]
        return try await post(url: url, accessToken: accessToken, jsonObject: body)
    }

    private func join(_ baseURL: URL, path: String) throws -> URL {
        var root = baseURL.absoluteString
        if root.hasSuffix("/") == false {
            root += "/"
        }
        let trimmedPath: String
        if path.hasPrefix("/") {
            trimmedPath = String(path.dropFirst())
        } else {
            trimmedPath = path
        }
        guard let url = URL(string: root + trimmedPath) else {
            throw TesseraClientError.invalidResponse
        }
        return url
    }

    private func percentEncodePath(_ value: String) -> String {
        var allowed = CharacterSet.urlPathAllowed
        allowed.remove(charactersIn: "/")
        return value.addingPercentEncoding(withAllowedCharacters: allowed) ?? value
    }

    private func get<T: Decodable>(url: URL, accessToken: String?) async throws -> T {
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        applyToken(accessToken, to: &request)
        return try await send(request)
    }

    private func post<T: Decodable>(url: URL, accessToken: String?, jsonObject: [String: Any]) async throws -> T {
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        applyToken(accessToken, to: &request)
        request.httpBody = try JSONSerialization.data(withJSONObject: jsonObject, options: [])
        return try await send(request)
    }

    private func applyToken(_ token: String?, to request: inout URLRequest) {
        guard let token, token.isEmpty == false else {
            return
        }
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
    }

    private func send<T: Decodable>(_ request: URLRequest) async throws -> T {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw TesseraClientError.unreachable
        }

        guard let http = response as? HTTPURLResponse else {
            throw TesseraClientError.invalidResponse
        }

        if http.statusCode == 204 {
            if T.self == JSONValue.self {
                return JSONValue.null as! T
            }
        }

        if http.statusCode >= 400 {
            let message = safeErrorMessage(data: data, status: http.statusCode)
            throw TesseraClientError.http(status: http.statusCode, message: message)
        }

        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            // voice/stop may return a small object; fall through with JSONValue when needed
            if T.self == JSONValue.self {
                let object = (try? JSONSerialization.jsonObject(with: data)) ?? NSNull()
                return JSONValue.from(object) as! T
            }
            throw TesseraClientError.invalidResponse
        }
    }

    /// Never echo credentials or raw Authorization material into UI errors.
    private func safeErrorMessage(data: Data, status: Int) -> String {
        if let body = try? decoder.decode(APIErrorBody.self, from: data) {
            if let error = body.error, error.isEmpty == false {
                let lowered = error.lowercased()
                if lowered.contains("bearer") || lowered.contains("authorization") || lowered.contains("token") {
                    return "Request rejected (\(status))"
                }
                if error.count > 200 {
                    return "Request rejected (\(status))"
                }
                return error
            }
        }
        return "Request failed (\(status))"
    }
}

enum TesseraClientError: LocalizedError {
    case unreachable
    case invalidResponse
    case http(status: Int, message: String)

    var errorDescription: String? {
        switch self {
        case .unreachable:
            return "Server unreachable"
        case .invalidResponse:
            return "Invalid server response"
        case .http(_, let message):
            return message
        }
    }
}
