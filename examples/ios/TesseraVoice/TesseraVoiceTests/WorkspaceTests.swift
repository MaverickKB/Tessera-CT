import XCTest
import Security
import WebKit
@testable import TesseraVoice

final class WorkspaceTests: XCTestCase {
    func testTokenReplacementNeverDeletesThePreviousTokenOnFailure() {
        XCTAssertThrowsError(try KeychainStore.writeToken("replacement", update: { _, _ in errSecInteractionNotAllowed },
            add: { _ in XCTFail("An update failure must not add a new item"); return errSecSuccess },
            delete: { _ in XCTFail("An update must never delete the existing token"); return errSecSuccess }))
        XCTAssertThrowsError(try KeychainStore.writeToken(nil, update: { _, _ in
            XCTFail("Clearing a token must not update it"); return errSecSuccess
        }, add: { _ in XCTFail("Clearing a token must not add it"); return errSecSuccess },
            delete: { _ in errSecInteractionNotAllowed }))
        var added = false
        XCTAssertNoThrow(try KeychainStore.writeToken("first-token", update: { _, _ in errSecItemNotFound },
            add: { _ in added = true; return errSecSuccess },
            delete: { _ in XCTFail("Adding a token must not delete it"); return errSecSuccess }))
        XCTAssertTrue(added)
    }

    @MainActor
    func testConnectionSaveValidatesAndStoresTokenBeforeCommittingAddress() throws {
        let suite = "connection-test-" + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let settings = AppSettingsStore(defaults: defaults, loadToken: { nil })
        settings.serverURLText = "https://old.example"
        settings.accessTokenText = "old-token"
        var stored: String?
        try settings.saveConnection(serverURL: " https://new.example/workspace/ ", token: " new-token ") { stored = $0 }
        XCTAssertEqual(stored, "new-token")
        XCTAssertEqual(settings.accessToken(), "new-token")
        XCTAssertEqual(settings.serverURLText, "https://new.example/workspace/")
        XCTAssertEqual(defaults.string(forKey: "settings.serverURL"), settings.serverURLText)
        XCTAssertThrowsError(try settings.saveConnection(serverURL: "invalid", token: "ignored") { _ in
            XCTFail("Invalid addresses must not change secure storage")
        })
        struct StorageFailure: Error {}
        XCTAssertThrowsError(try settings.saveConnection(serverURL: "https://other.example", token: "ignored") { _ in
            throw StorageFailure()
        })
        XCTAssertEqual(settings.serverURLText, "https://new.example/workspace/")
        XCTAssertEqual(settings.accessToken(), "new-token")
        try settings.saveConnection(serverURL: "https://new.example/workspace/", token: " ") { stored = $0 }
        XCTAssertNil(stored, "Clearing a token must also clear secure storage")
        XCTAssertNil(settings.accessToken())
        let relaunched = AppSettingsStore(defaults: defaults, loadToken: { stored })
        XCTAssertEqual(relaunched.serverURLText, settings.serverURLText)
        XCTAssertNil(relaunched.accessToken())
    }

    @MainActor
    func testConnectionRejectsCredentialsAndRouteStateInServerAddress() {
        let embeddedCredentials = "https://user:password" + "@" + "server.example"
        for text in ["", "hostname", "ftp://server.example", embeddedCredentials,
                     "https://server.example?token=value", "https://server.example#view=chat"] {
            XCTAssertThrowsError(try AppSettingsStore.validateServerURL(text), text)
        }
        XCTAssertNoThrow(try AppSettingsStore.validateServerURL("https://server.example/workspace/"))
    }

    func testOriginRequiresMatchingSchemeHostAndPort() {
        let connection = WorkspaceConnection(url: URL(string: "https://workspace.example")!, token: nil)
        XCTAssertTrue(connection.contains(URL(string: "https://workspace.example:443/api/status")!))
        XCTAssertFalse(connection.contains(URL(string: "http://workspace.example")!))
        XCTAssertFalse(connection.contains(URL(string: "https://workspace.example:8740")!))
        XCTAssertFalse(connection.contains(URL(string: "https://other.example")!))
    }

    func testSessionRoutePreservesReservedCharactersAndBasePath() {
        let connection = WorkspaceConnection(url: URL(string: "https://workspace.example/workspace/")!, token: nil)
        let url = connection.workspaceURL(slug: "planning & decisions")
        XCTAssertEqual(URLComponents(url: url, resolvingAgainstBaseURL: false)?.path, "/workspace/")
        var route = URLComponents()
        route.percentEncodedQuery = URLComponents(url: url, resolvingAgainstBaseURL: false)?.percentEncodedFragment
        XCTAssertEqual(route.queryItems?.first(where: { $0.name == "session" })?.value, "planning & decisions")
        XCTAssertNil(url.query)
    }

    func testBackgroundTaskPollingIsOptionalInBrokerReplies() throws {
        let legacy = try JSONDecoder().decode(VoiceEventsResponse.self,
            from: Data(#"{"send":[],"accepted":1}"#.utf8))
        XCTAssertNil(legacy.poll_after_ms)
        let pending = try JSONDecoder().decode(VoiceEventsResponse.self,
            from: Data(#"{"send":[],"poll_after_ms":1000}"#.utf8))
        XCTAssertEqual(pending.poll_after_ms, 1000)
    }

    @MainActor
    func testVoiceClicksAndNavigationStaySeparate() async throws {
        let fixture = WebFixture()
        let configuration = WKWebViewConfiguration()
        configuration.userContentController.add(fixture, name: "tesseraNative")
        let source = "window.tesseraConnection = {origin:'https://workspace.example',token:''};\n" + NativeWorkspaceBridge.script
        configuration.userContentController.addUserScript(WKUserScript(source: source, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = fixture
        webView.loadHTMLString("<html><body><button id='voice-toggle'>Voice</button><select id='voice-pick'><option>Rex</option></select></body></html>", baseURL: URL(string: "https://workspace.example"))
        await fulfillment(of: [fixture.loaded], timeout: 10)
        _ = try await webView.evaluateJavaScript("history.pushState(null, '', '#view=chat&session=operations'); document.getElementById('voice-toggle').click();")
        await fulfillment(of: [fixture.clicked], timeout: 5)
        XCTAssertEqual(fixture.voiceActions.first?["slug"], "operations")
        XCTAssertEqual(fixture.voiceActions.first?["voice"], "Rex")
        _ = try await webView.evaluateJavaScript("window.tesseraNativeState({running:true,label:'Listening',slug:'operations'}); history.pushState(null, '', '#view=wip&session=planning');")
        let label = try await webView.evaluateJavaScript("document.getElementById('voice-toggle').textContent") as? String
        XCTAssertEqual(label, "End native call")
        XCTAssertEqual(fixture.voiceActions.count, 1, "Page navigation must not emit a voice stop/start action")
        configuration.userContentController.removeScriptMessageHandler(forName: "tesseraNative")
    }
}

@MainActor
private final class WebFixture: NSObject, WKNavigationDelegate, WKScriptMessageHandler {
    let loaded = XCTestExpectation(description: "Workspace loaded")
    let clicked = XCTestExpectation(description: "Voice routed to native")
    var voiceActions: [[String: String]] = []

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        loaded.fulfill()
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: String] else { return }
        if body["action"] == "voice" {
            voiceActions.append(body)
            clicked.fulfill()
        }
    }
}
