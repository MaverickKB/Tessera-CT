import Foundation

@MainActor
final class SessionEventStream {
    private let client: TesseraClient
    private var task: Task<Void, Never>?
    private var generation = UUID()
    private(set) var lastEventID: String?

    init(client: TesseraClient) { self.client = client }

    func start(
        baseURL: URL, accessToken: String?, slug: String,
        onReady: @escaping (Bool) -> Void,
        onFrame: @escaping (SessionSSEParser.Frame) -> Void,
        onFail: @escaping (String) -> Void
    ) {
        guard task == nil else { return }
        let run = UUID()
        generation = run
        let key = "session.events." + baseURL.absoluteString + "/" + slug
        lastEventID = UserDefaults.standard.string(forKey: key)
        task = Task { [weak self] in
            guard let self else { return }
            var retry: UInt64 = 1
            while Task.isCancelled == false && self.generation == run {
                do {
                    let url = try self.client.eventsURL(baseURL: baseURL, slug: slug)
                    let request = self.client.makeSSERequest(url: url, accessToken: accessToken, lastEventID: self.lastEventID)
                    let (bytes, response) = try await self.client.urlSession().bytes(for: request)
                    guard self.generation == run, Task.isCancelled == false else { return }
                    guard let http = response as? HTTPURLResponse else { throw TesseraClientError.invalidResponse }
                    if (400..<500).contains(http.statusCode) {
                        onFail("Session events rejected (\(http.statusCode))")
                        return
                    }
                    guard http.statusCode == 200,
                          http.value(forHTTPHeaderField: "Content-Type")?.hasPrefix("text/event-stream") == true else {
                        throw TesseraClientError.invalidResponse
                    }
                    onReady(true)
                    retry = 1
                    var parser = SessionSSEParser()
                    var lineBytes = Data()
                    var frameBytes = 0
                    // AsyncBytes.lines drops empty lines, which delimit SSE frames.
                    for try await byte in bytes {
                        guard self.generation == run, Task.isCancelled == false else { return }
                        if byte != 10 {
                            if lineBytes.count >= 1_048_576 { throw TesseraClientError.invalidResponse }
                            lineBytes.append(byte)
                            continue
                        }
                        if lineBytes.last == 13 { lineBytes.removeLast() }
                        let line = String(decoding: lineBytes, as: UTF8.self)
                        frameBytes += lineBytes.count
                        lineBytes.removeAll(keepingCapacity: true)
                        if frameBytes > 1_048_576 { throw TesseraClientError.invalidResponse }
                        if line.isEmpty { frameBytes = 0 }
                        guard let frame = parser.push(line: line) else { continue }
                        if let id = frame.id, let number = Int(id), let last = Int(self.lastEventID ?? "0"), number <= last { continue }
                        onFrame(frame)
                        guard self.generation == run else { return }
                        if let id = frame.id {
                            self.lastEventID = id
                            UserDefaults.standard.set(id, forKey: key)
                        }
                    }
                } catch {
                    if Task.isCancelled || self.generation != run { return }
                }
                guard self.generation == run, Task.isCancelled == false else { return }
                onReady(false)
                do { try await Task.sleep(nanoseconds: retry * 1_000_000_000) } catch { return }
                retry = min(retry * 2, 10)
            }
        }
    }

    func close() {
        generation = UUID()
        task?.cancel()
        task = nil
    }
}
