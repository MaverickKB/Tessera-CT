import SwiftUI
import WebKit

struct WorkspaceConnection: Equatable {
    let url: URL
    let token: String?

    func contains(_ candidate: URL) -> Bool {
        let expectedScheme = url.scheme?.lowercased()
        let candidateScheme = candidate.scheme?.lowercased()
        let expectedPort = url.port ?? (expectedScheme == "https" ? 443 : 80)
        let candidatePort = candidate.port ?? (candidateScheme == "https" ? 443 : 80)
        return candidateScheme == expectedScheme
            && candidate.host?.lowercased() == url.host?.lowercased()
            && candidatePort == expectedPort
    }

    func workspaceURL(slug: String) -> URL {
        var parts = URLComponents(url: url, resolvingAgainstBaseURL: false)!
        var route = URLComponents()
        route.queryItems = [URLQueryItem(name: "view", value: "chat")]
        if !slug.isEmpty {
            route.queryItems?.append(URLQueryItem(name: "session", value: slug))
        }
        parts.percentEncodedFragment = route.percentEncodedQuery
        return parts.url!
    }
}

struct WorkspaceDownload: Identifiable {
    let id = UUID()
    let url: URL
}

@MainActor
final class WorkspaceState: ObservableObject {
    @Published var error: String?
    @Published var loading = false
    @Published var download: WorkspaceDownload?
    weak var webView: WKWebView?

    func reload() {
        error = nil
        webView?.reload()
    }

    // The workspace itself owns every page: chat, the WIP board, infrastructure and
    // the rest. The native shell picks a page by writing the same hash route the web
    // router listens to, so no reload is needed and the conversation state survives
    // a round trip to the board and back.
    func openPage(_ name: String) {
        guard let webView else { return }
        let script = "location.hash = '#view=" + name + "'"
        webView.evaluateJavaScript(script, completionHandler: nil)
    }
}

struct WorkspaceView: UIViewRepresentable {
    let connection: WorkspaceConnection
    @ObservedObject var state: WorkspaceState
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var voice: VoiceCoordinator
    @EnvironmentObject private var settings: AppSettingsStore

    func makeCoordinator() -> Coordinator {
        Coordinator(self)
    }

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.allowsInlineMediaPlayback = true
        let controller = configuration.userContentController
        controller.add(context.coordinator, name: "tesseraNative")
        let bootstrap: [String: Any] = [
            "origin": connection.url.absoluteString,
            "token": connection.token ?? ""
        ]
        let data = try! JSONSerialization.data(withJSONObject: bootstrap)
        let source = "window.tesseraConnection = " + String(decoding: data, as: UTF8.self) + ";\n" + NativeWorkspaceBridge.script
        controller.addUserScript(WKUserScript(source: source, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        webView.isInspectable = false
        webView.allowsBackForwardNavigationGestures = true
        state.webView = webView
        var request = URLRequest(url: connection.workspaceURL(slug: settings.selectedSlug))
        if let token = connection.token, !token.isEmpty {
            request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        }
        webView.load(request)
        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {
        context.coordinator.parent = self
        context.coordinator.publishCallState(webView)
    }

    static func dismantleUIView(_ webView: WKWebView, coordinator: Coordinator) {
        webView.configuration.userContentController.removeScriptMessageHandler(forName: "tesseraNative")
        webView.navigationDelegate = nil
        webView.uiDelegate = nil
        // The application-owned voice coordinator has no web view lifetime dependency.
    }

    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler, WKDownloadDelegate {
        var parent: WorkspaceView
        private var downloads: [ObjectIdentifier: URL] = [:]

        init(_ parent: WorkspaceView) {
            self.parent = parent
        }

        func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
            guard message.frameInfo.isMainFrame,
                  let url = message.frameInfo.request.url,
                  parent.connection.contains(url),
                  let body = message.body as? [String: String],
                  let action = body["action"] else { return }
            if action == "selection" {
                if !parent.voice.isRunning, let slug = body["slug"], !slug.isEmpty {
                    parent.settings.selectedSlug = slug
                }
            } else if action == "voice" {
                if parent.voice.isRunning {
                    parent.voice.stop()
                } else if let slug = body["slug"], !slug.isEmpty {
                    parent.settings.selectedSlug = slug
                    if let selectedVoice = body["voice"], !selectedVoice.isEmpty {
                        parent.settings.selectedVoice = selectedVoice
                    }
                    Task { await parent.model.startCall() }
                }
            } else if action == "failure", var detail = body["detail"] {
                if let token = parent.connection.token, !token.isEmpty {
                    detail = detail.replacingOccurrences(of: token, with: "[redacted]")
                }
                parent.state.error = "Workspace error: " + detail
            } else if action == "mute" {
                parent.voice.setMuted(!parent.voice.isMuted)
            } else if action == "ready", let webView = message.webView {
                publishCallState(webView)
            }
        }

        func publishCallState(_ webView: WKWebView) {
            let state: [String: Any] = [
                "running": parent.voice.isRunning,
                "muted": parent.voice.isMuted,
                "label": parent.voice.status.label,
                "slug": parent.voice.activeSessionSlug ?? ""
            ]
            guard let data = try? JSONSerialization.data(withJSONObject: state) else { return }
            let script = "window.tesseraNativeState?.(" + String(decoding: data, as: UTF8.self) + ")"
            webView.evaluateJavaScript(script, completionHandler: nil)
        }

        func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
            parent.state.loading = true
            parent.state.error = nil
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            parent.state.loading = false
            publishCallState(webView)
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            showConnectionError(error)
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            showConnectionError(error)
        }

        private func showConnectionError(_ error: Error) {
            if (error as NSError).code == NSURLErrorCancelled { return }
            parent.state.loading = false
            parent.state.error = "Workspace unavailable. Check the server address and Tailscale connection, then retry. Native call controls remain available."
        }

        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
            parent.state.error = "The workspace needs to reload. Your native call has its own connection."
            parent.state.loading = false
        }

        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let url = navigationAction.request.url else {
                decisionHandler(.cancel)
                return
            }
            if navigationAction.shouldPerformDownload,
               (parent.connection.contains(url) || (url.scheme == "blob" && webView.url.map(parent.connection.contains) == true)) {
                decisionHandler(.download)
            } else if parent.connection.contains(url) {
                decisionHandler(.allow)
            } else {
                decisionHandler(.cancel)
                if navigationAction.navigationType == .linkActivated {
                    UIApplication.shared.open(url)
                }
            }
        }

        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let url = navigationAction.request.url {
                if parent.connection.contains(url) {
                    webView.load(navigationAction.request)
                } else {
                    UIApplication.shared.open(url)
                }
            }
            return nil
        }

        func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
            download.delegate = self
        }

        func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
            decisionHandler(navigationResponse.canShowMIMEType ? .allow : .download)
        }

        func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
            download.delegate = self
        }

        func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
            let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
            do {
                try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
                let filename = (suggestedFilename as NSString).lastPathComponent
                let destination = folder.appendingPathComponent(filename.isEmpty ? "Export" : filename)
                downloads[ObjectIdentifier(download)] = destination
                completionHandler(destination)
            } catch {
                parent.state.error = "The export could not be saved. Try again."
                completionHandler(nil)
            }
        }

        func downloadDidFinish(_ download: WKDownload) {
            if let url = downloads.removeValue(forKey: ObjectIdentifier(download)) {
                parent.state.download = WorkspaceDownload(url: url)
            }
        }

        func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
            downloads.removeValue(forKey: ObjectIdentifier(download))
            parent.state.error = "The export did not finish. Check the connection and try again."
        }

        func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin, initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType, decisionHandler: @escaping (WKPermissionDecision) -> Void) {
            // CallKit and the native audio engine exclusively own microphone capture.
            decisionHandler(.deny)
        }
    }
}

struct WorkspaceShareView: UIViewControllerRepresentable {
    let url: URL

    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: [url], applicationActivities: nil)
    }

    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}
