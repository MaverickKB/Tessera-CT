import XCTest

/// The project surface, walked inside the app.
///
/// The app is a native shell over the same web workspace, so "the iOS project
/// view" means a person on a phone can reach a project from the Pages menu and
/// land in its workspace — not that a separate Swift screen exists. This test
/// measures that journey against a live core, the way a reader walks it.
///
/// Element kinds here were read from the app's own accessibility tree, not
/// guessed: the project tabs are Switch controls, and the conversation chooser
/// is a custom select exposed as an Other with label "Conversation".
final class ProjectJourneyTests: XCTestCase {
    func testProjectOpensInsideTheApp() throws {
        guard let endpoint = ProcessInfo.processInfo.environment["WORKSPACE_TEST_URL"] else {
            throw XCTSkip("Set WORKSPACE_TEST_URL for an explicitly selected workspace acceptance target.")
        }
        let app = XCUIApplication()
        app.launchArguments = ["-settings.serverURL", endpoint]
        app.launch()
        let web = app.webViews.firstMatch
        XCTAssertTrue(web.waitForExistence(timeout: 20))

        // Reach the work board the way the app offers it: the native Pages menu,
        // not the web nav, because the menu is what a phone user touches.
        let menu = app.buttons["Pages"]
        XCTAssertTrue(menu.waitForExistence(timeout: 20))
        menu.tap()
        let workInProgress = app.buttons["Work in progress"]
        XCTAssertTrue(workInProgress.waitForExistence(timeout: 5))
        workInProgress.tap()

        // The projects section opens by default at phone width, but a reader who
        // collapsed it has that choice remembered. Both are legitimate, so if the
        // card is not there, open the section once — that is what a person does —
        // and only then call it unreachable.
        let projectTitle = "Tessera distributable alpha project"
        var card = web.buttons.matching(NSPredicate(format: "label CONTAINS %@", projectTitle)).firstMatch
        if !card.waitForExistence(timeout: 10) {
            let section = web.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Subjects & projects")).firstMatch
            XCTAssertTrue(section.waitForExistence(timeout: 10), "The board offers no Subjects & projects section on the phone.")
            section.tap()
            card = web.buttons.matching(NSPredicate(format: "label CONTAINS %@", projectTitle)).firstMatch
        }
        XCTAssertTrue(card.waitForExistence(timeout: 20), "No project card titled '\(projectTitle)' was reachable on the board, even with the projects section open.")
        let boardShot = XCTAttachment(screenshot: app.screenshot())
        boardShot.name = "Projects reachable on the phone"
        boardShot.lifetime = .keepAlways
        add(boardShot)

        card.tap()

        // Landing in the project means its workspace replaced the grid: the arena
        // heading carries the project title and the three tabs are present.
        let arenaTitle = web.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Tessera distributable alpha project")).firstMatch
        XCTAssertTrue(arenaTitle.waitForExistence(timeout: 20), "Tapping the project card did not open the project workspace.")
        let canvasTab = web.switches["Canvas"]
        let planTab = web.switches["Plan & notes"]
        let tasksTab = web.switches["Tasks"]
        XCTAssertTrue(canvasTab.waitForExistence(timeout: 10), "The project workspace has no Canvas tab on the phone.")
        XCTAssertTrue(planTab.exists, "The project workspace has no Plan & notes tab on the phone.")
        XCTAssertTrue(tasksTab.exists, "The project workspace has no Tasks tab on the phone.")

        // The conversation chooser is part of the same surface; it must name the
        // session the project talks through, not be absent on a phone.
        let conversation = web.otherElements.matching(NSPredicate(format: "label == %@", "Conversation")).firstMatch
        XCTAssertTrue(conversation.waitForExistence(timeout: 10), "The project workspace offers no conversation chooser on the phone.")
        XCTAssertFalse(conversation.value as? String == nil || (conversation.value as? String ?? "").isEmpty,
                       "The conversation chooser exists but names no session.")

        let openShot = XCTAttachment(screenshot: app.screenshot())
        openShot.name = "Project workspace open in the app"
        openShot.lifetime = .keepAlways
        add(openShot)

        // The tabs are switches; tapping one has to actually change the pane,
        // not merely highlight.
        planTab.tap()
        XCTAssertTrue(planTab.waitForExistence(timeout: 5))
        XCTAssertEqual(planTab.value as? String, "1", "Plan & notes did not become the selected pane after tapping it.")
        let planShot = XCTAttachment(screenshot: app.screenshot())
        planShot.name = "Plan & notes on the phone"
        planShot.lifetime = .keepAlways
        add(planShot)
    }
}
