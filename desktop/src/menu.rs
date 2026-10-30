//! Native app menu. WKWebView gets no Edit or Reload shortcuts on its own: without these
//! items Cmd+C/V/Z/A and Cmd+R do nothing in the window.
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::{AppHandle, Manager, Runtime};

pub const RELOAD_ID: &str = "view.reload";
pub const RELOAD_ACCELERATOR: &str = "CmdOrCtrl+R";

/// The menu as data, so its shape is testable without a window server.
pub fn spec(app_name: &str) -> Vec<(String, Vec<&'static str>)> {
    // Hide, Hide Others and Show All are macOS application-menu commands.
    // Asking other platforms for them fails the menu build, so the window never opens.
    let mut app_items = vec!["about", "server.settings", "separator"];
    if cfg!(target_os = "macos") {
        app_items.extend(["hide", "hide_others", "show_all", "separator"]);
    }
    app_items.push("quit");
    vec![
        (app_name.to_string(), app_items),
        ("Edit".into(), vec!["undo", "redo", "separator", "cut", "copy", "paste", "select_all"]),
        ("View".into(), vec![RELOAD_ID, "fullscreen"]),
        ("Window".into(), vec!["minimize", "maximize", "separator", "close_window"]),
    ]
}

pub fn build<R: Runtime>(app: &AppHandle<R>, app_name: &str) -> tauri::Result<Menu<R>> {
    let menu = Menu::new(app)?;
    for (title, items) in spec(app_name) {
        let sub = Submenu::new(app, &title, true)?;
        for item in items {
            match item {
                "about" => sub.append(&PredefinedMenuItem::about(app, None, None)?)?,
                // The Settings page holds the server controls in both modes, so the menu
                // command has to be reachable in both: launchd hands the core to a
                // service, and elsewhere the app supervises it.
                "server.settings" => {
                    let controls = app.try_state::<crate::server::Server>().is_some()
                        || app.try_state::<crate::supervisor::Supervisor>().is_some();
                    sub.append(&MenuItem::with_id(app, "server.settings", "Settings…", controls, Some("CmdOrCtrl+,"))?)?
                }
                "separator" => sub.append(&PredefinedMenuItem::separator(app)?)?,
                "hide" => sub.append(&PredefinedMenuItem::hide(app, None)?)?,
                "hide_others" => sub.append(&PredefinedMenuItem::hide_others(app, None)?)?,
                "show_all" => sub.append(&PredefinedMenuItem::show_all(app, None)?)?,
                "quit" => sub.append(&PredefinedMenuItem::quit(app, None)?)?,
                "undo" => sub.append(&PredefinedMenuItem::undo(app, None)?)?,
                "redo" => sub.append(&PredefinedMenuItem::redo(app, None)?)?,
                "cut" => sub.append(&PredefinedMenuItem::cut(app, None)?)?,
                "copy" => sub.append(&PredefinedMenuItem::copy(app, None)?)?,
                "paste" => sub.append(&PredefinedMenuItem::paste(app, None)?)?,
                "select_all" => sub.append(&PredefinedMenuItem::select_all(app, None)?)?,
                "fullscreen" => sub.append(&PredefinedMenuItem::fullscreen(app, None)?)?,
                "minimize" => sub.append(&PredefinedMenuItem::minimize(app, None)?)?,
                "maximize" => sub.append(&PredefinedMenuItem::maximize(app, None)?)?,
                "close_window" => sub.append(&PredefinedMenuItem::close_window(app, None)?)?,
                RELOAD_ID => sub.append(&MenuItem::with_id(app, RELOAD_ID, "Reload", true, Some(RELOAD_ACCELERATOR))?)?,
                other => unreachable!("unknown menu item {other}"),
            }
        }
        menu.append(&sub)?;
    }
    Ok(menu)
}

/// Reload re-fetches the page from the live core, picking up a new UI build.
pub fn handle<R: Runtime>(app: &AppHandle<R>, id: &str) {
    if id == "server.settings" {
        if let Some(window) = app.get_webview_window("main") {
            let _ = window.eval("window.location.hash = 'view=settings'");
            let _ = window.set_focus();
        }
    }
    if id == RELOAD_ID {
        if let Some(window) = app.get_webview_window("main") {
            let _ = window.reload();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn menu_has_reload_edit_and_quit() {
        let spec = spec("Tessera");
        let find = |title: &str| spec.iter().find(|(t, _)| t == title).map(|(_, i)| i.clone()).unwrap();
        assert_eq!(spec[0].0, "Tessera", "macOS takes the first submenu as the app menu");
        assert!(find("Tessera").contains(&"quit"));
        if cfg!(target_os = "macos") {
            assert!(find("Tessera").contains(&"hide"));
            assert!(find("Tessera").contains(&"hide_others"));
            assert!(find("Tessera").contains(&"show_all"));
        } else {
            assert!(!find("Tessera").contains(&"hide"), "hide fails the menu build off macOS");
            assert!(!find("Tessera").contains(&"hide_others"));
            assert!(!find("Tessera").contains(&"show_all"));
        }
        for item in ["undo", "redo", "cut", "copy", "paste", "select_all"] {
            assert!(find("Edit").contains(&item), "{item}");
        }
        assert!(find("View").contains(&RELOAD_ID));
        assert_eq!(RELOAD_ACCELERATOR, "CmdOrCtrl+R");
    }
}
