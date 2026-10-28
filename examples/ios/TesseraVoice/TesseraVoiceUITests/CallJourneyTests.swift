import XCTest

final class CallJourneyTests: XCTestCase {
    func testWorkspaceNavigationAndNativeCallSheet() throws {
        guard let endpoint = ProcessInfo.processInfo.environment["WORKSPACE_TEST_URL"] else {
            throw XCTSkip("Set WORKSPACE_TEST_URL for an explicitly selected workspace acceptance target.")
        }
        let app = XCUIApplication()
        app.launchArguments = ["-settings.serverURL", endpoint]
        app.launch()
        let web = app.webViews.firstMatch
        XCTAssertTrue(web.waitForExistence(timeout: 20))
        let menu = web.buttons["Menu"]
        XCTAssertTrue(menu.waitForExistence(timeout: 20))
        let initial = XCTAttachment(screenshot: app.screenshot())
        initial.name = "Workspace loaded"
        initial.lifetime = .keepAlways
        add(initial)
        for destination in ["WIP board", "Infrastructure board", "Integrations", "Settings", "Chat sessions"] {
            menu.tap()
            let opened = XCTAttachment(screenshot: app.screenshot())
            opened.name = "Menu for " + destination
            opened.lifetime = .keepAlways
            add(opened)
            let link = web.links[destination]
            XCTAssertTrue(link.waitForExistence(timeout: 5), destination)
            link.tap()
            let screenshot = XCTAttachment(screenshot: app.screenshot())
            screenshot.name = destination
            screenshot.lifetime = .keepAlways
            add(screenshot)
        }
        app.buttons["Voice"].firstMatch.tap()
        XCTAssertTrue(app.buttons["Start call"].waitForExistence(timeout: 5))
        app.buttons["Back to workspace"].tap()
        XCTAssertTrue(menu.waitForExistence(timeout: 5))
        app.buttons["Reload workspace"].tap()
        XCTAssertTrue(menu.waitForExistence(timeout: 20))
        XCTAssertFalse(app.buttons["End"].exists, "Browsing must not start a call")
    }

    func testMutedCallSurvivesBackground() {
        let app = XCUIApplication()
        app.launch()
        app.buttons["Voice"].firstMatch.tap()
        let start = app.buttons["Start call"]
        XCTAssertTrue(start.waitForExistence(timeout: 15))
        start.tap()
        XCTAssertTrue(app.staticTexts["Listening"].waitForExistence(timeout: 30))
        app.buttons["Mute"].tap()
        XCTAssertTrue(app.staticTexts["Muted"].waitForExistence(timeout: 5))
        XCUIDevice.shared.press(.home)
        Thread.sleep(forTimeInterval: 90)
        app.activate()
        XCTAssertTrue(app.buttons["End"].waitForExistence(timeout: 10), app.debugDescription)
        XCTAssertTrue(app.staticTexts["Muted"].exists)
        app.buttons["End"].tap()
        XCTAssertTrue(start.waitForExistence(timeout: 10))
    }

    func testConfiguredCallStartsAndEnds() {
        let app = XCUIApplication()
        app.launch()
        app.buttons["Voice"].firstMatch.tap()
        let start = app.buttons["Start call"]
        XCTAssertTrue(start.waitForExistence(timeout: 15))
        XCTAssertTrue(start.isEnabled)
        start.tap()

        let listening = app.staticTexts["Listening"]
        let connected = listening.waitForExistence(timeout: 30)
        if connected == false {
            let attachment = XCTAttachment(screenshot: app.screenshot())
            attachment.lifetime = .keepAlways
            add(attachment)
            XCTFail("Call did not reach Listening. " + app.debugDescription)
            return
        }

        let mute = app.buttons["Mute"]
        XCTAssertTrue(mute.waitForExistence(timeout: 5))
        mute.tap()
        XCTAssertTrue(app.staticTexts["Muted"].waitForExistence(timeout: 5))
        app.buttons["Unmute"].tap()
        XCTAssertTrue(listening.waitForExistence(timeout: 5))
        app.buttons["End"].tap()
        XCTAssertTrue(start.waitForExistence(timeout: 10))
    }
}
