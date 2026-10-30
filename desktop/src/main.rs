//! Tessera desktop shell (Tauri v2).
//!
//! Spawns the Python core on loopback, waits for its ready record, opens the
//! bundled web UI from the core's own origin (so the core's Host/Origin fence
//! passes) and stops the core when the app exits. The page gets no Tauri IPC.
//!
//! With `keep_core`, the app is the supervisor instead: it spawns the core
//! detached on a fixed port, leaves it serving when the window closes, and
//! respawns it if it dies while the app is open. Starting the app is then also
//! the way to bump a dead core.
#![cfg_attr(all(not(debug_assertions), target_os = "windows"), windows_subsystem = "windows")]

mod config;
mod core;
mod menu;
mod server;
mod supervisor;

use std::io::Write;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::webview::PageLoadEvent;
use std::sync::atomic::{AtomicBool, Ordering};
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent};

/// Tells the page which operating system this window is, and whether this
/// process owns the core as a child or through a system service.
///
/// The page's error guard already owns `window.TesseraShell`. This adds
/// `platform` and `service` onto that object. Replacing it would drop
/// `bannerText` and the missing-handler guard.
pub fn shell_banner(service: &str) -> String {
    let platform = if cfg!(target_os = "macos") { "macos" }
        else if cfg!(windows) { "windows" }
        else if cfg!(target_os = "linux") { "linux" }
        else { "other" };
    format!(
        "(function(){{var shell=window.TesseraShell||(window.TesseraShell={{}});shell.platform=\"{platform}\";shell.service=\"{service}\";}})();"
    )
}

fn fail(message: &str) -> ! {
    eprintln!("{}", serde_json::json!({"event": "tessera-desktop.error", "error": message}));
    std::process::exit(1);
}

/// Only the shell knows dropped absolute paths. Forward metadata, never file bytes.
fn install_local_file_drop(window: &tauri::WebviewWindow, origin: url::Url) {
    let target = window.clone();
    window.on_webview_event(move |event| {
        if let tauri::WebviewEvent::DragDrop(tauri::DragDropEvent::Drop { paths, position }) = event {
            let Ok(current) = target.url() else { return };
            if current.origin() != origin.origin() { return; }
            let Ok(scale) = target.scale_factor() else { return };
            let detail = serde_json::json!({
                "paths": paths, "x": position.x / scale, "y": position.y / scale,
            });
            let script = format!("window.dispatchEvent(new CustomEvent('cairn-local-file-drop', {{detail: {detail}}}));");
            if let Err(error) = target.eval(&script) {
                eprintln!("Local file drop could not reach the page: {error}");
            }
        }
    });
}

fn main() {
    let started = Instant::now();
    let smoke = std::env::args().any(|a| a == "--smoke");
    let env: std::collections::HashMap<String, String> = std::env::vars().collect();
    let settings = config::load(&env).unwrap_or_else(|e| fail(&e));
    let context = tauri::generate_context!();
    if !settings.launch_agent.is_empty() {
        server::run(settings, context).unwrap_or_else(|e| fail(&e));
        return;
    }
    let core = core::Core::connect(&settings, &env).unwrap_or_else(|e| fail(&e));
    let origin = core.origin.clone();
    let core_pid = core.pid;
    let core = Arc::new(Mutex::new(core));
    let reported = Arc::new(Mutex::new(false));
    let stopping = Arc::new(AtomicBool::new(false));

    // The workspace page gets real Start server and Stop server controls on
    // every platform. macOS can hand the core to launchd; elsewhere this app is
    // already the supervisor, so it exposes that supervisor the same way.
    let supervisor = supervisor::Supervisor { core: core.clone(), settings: settings.clone() };
    let app = tauri::Builder::default()
        .manage(supervisor)
        .manage(origin.clone())
        .invoke_handler(tauri::generate_handler![
            supervisor::supervisor_status,
            supervisor::supervisor_change
        ])
        .setup({
            let settings = settings.clone();
            let origin = origin.clone();
            let reported = reported.clone();
            move |app| {
                let allowed = origin.clone();
                let page_origin = origin.clone();
                let handle = app.handle().clone();
                app.set_menu(menu::build(app.handle(), &settings.title)?)?;
                app.on_menu_event(|app, event| menu::handle(app, event.id().as_ref()));
                let permission = tauri::ipc::CapabilityBuilder::new("workspace-server")
                    .window("main")
                    .local(false)
                    .remote(format!("{}/*", origin.origin().ascii_serialization()))
                    .permission("server-controls");
                app.add_capability(permission)?;
                // The window opens the address the operator configured, fragment included,
                // so a configured `#view=settings` lands on Settings. The core is
                // still addressed without it.
                let window_url = url::Url::parse(&settings.attach_url)
                    .ok()
                    .filter(|parsed| parsed.fragment().is_some())
                    .unwrap_or_else(|| origin.clone());
                let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::External(window_url))
                    .title(&settings.title)
                    // Runs before the page. A missing WebKit handler must not become a red banner.
                    .initialization_script(include_str!("../../web/shell-errors.js"))
                    .initialization_script(shell_banner("child"))
                    .initialization_script(supervisor::PAGE_BRIDGE)
                    .inner_size(settings.width, settings.height)
                    .min_inner_size(settings.min_width, settings.min_height)
                    // Navigation can never leave the core's own origin.
                    .on_navigation(move |url| url.origin() == allowed.origin())
                    .on_page_load(move |_window, payload| {
                        if payload.event() != PageLoadEvent::Finished || payload.url().origin() != page_origin.origin() {
                            return;
                        }
                        let mut done = reported.lock().unwrap();
                        if *done { return; }
                        *done = true;
                        let _ = writeln!(std::io::stdout(), "{}", serde_json::json!({
                            "event": "tessera-desktop.window-ready",
                            "startup_ms": started.elapsed().as_millis() as u64,
                            "shell_pid": std::process::id(), "core_pid": core_pid,
                        }));
                        if smoke {
                            let handle = handle.clone();
                            std::thread::spawn(move || {
                                std::thread::sleep(Duration::from_millis(1500));
                                handle.exit(0);
                            });
                        }
                    })
                    .build()?;
                install_local_file_drop(&window, origin.clone());
                Ok(())
            }
        })
        .build(context)
        .unwrap_or_else(|_| fail("The desktop window could not start"));

    // If the core dies on its own, the app cannot continue.
    {
        let core = core.clone();
        let handle = app.handle().clone();
        let stopping = stopping.clone();
        let settings = settings.clone();
        std::thread::spawn(move || {
            let mut failures: u32 = 0;
            loop {
                std::thread::sleep(Duration::from_millis(500));
                if stopping.load(Ordering::SeqCst) { break; }
                let mut core = core.lock().unwrap();
                let action = core.monitor_action();
                if action == core::MonitorAction::Idle { failures = 0; continue; }
                if action == core::MonitorAction::Exit {
                    drop(core);
                    eprintln!("{}", serde_json::json!({"event": "tessera-desktop.error", "error": "The local core stopped"}));
                    handle.exit(1);
                    break;
                }
                // A kept core is the app's to restart: the window stays open and the
                // page's reconnect banner clears when the address answers again.
                eprintln!("{}", serde_json::json!({"event": "tessera-desktop.core-restarting"}));
                match core.respawn(&settings) {
                    Ok(()) => {
                        failures = 0;
                        eprintln!("{}", serde_json::json!({"event": "tessera-desktop.core-restarted"}));
                    }
                    Err(error) => {
                        failures += 1;
                        eprintln!("{}", serde_json::json!({"event": "tessera-desktop.core-respawn-failed", "error": error}));
                        drop(core);
                        // Back off rather than hammer a home lock or a bad config.
                        std::thread::sleep(Duration::from_secs((failures.min(6) * 5) as u64));
                    }
                }
            }
        });
    }

    app.run(move |handle, event| {
        match event {
            RunEvent::WindowEvent {
                label,
                event: WindowEvent::CloseRequested { api, .. },
                ..
            } if label == "main" => {
                // A native close request (including Windows WM_CLOSE) must
                // stop the owned core before destroying the last WebView.
                // Depending on platform event-loop behavior, waiting for Exit
                // alone can leave that core alive after its window disappears.
                api.prevent_close();
                if !stopping.swap(true, Ordering::SeqCst) {
                    let core = core.clone();
                    let settings = settings.clone();
                    let handle = handle.clone();
                    // The launcher may have closed its readiness pipe already;
                    // diagnostics must never prevent the shutdown below.
                    let _ = writeln!(std::io::stderr(), "{}", serde_json::json!({"event": "tessera-desktop.window-closing"}));
                    std::thread::spawn(move || {
                        // Stop has the ownership policy: kept cores and cores
                        // attached from another owner are left serving.
                        core.lock().unwrap().stop(&settings);
                        handle.exit(0);
                    });
                }
            }
            RunEvent::Exit => {
                // Quit/menu/smoke exits still clean up if they did not originate
                // from the main-window close handler. Never race a second stop
                // against its existing graceful shutdown.
                if !stopping.swap(true, Ordering::SeqCst) {
                    core.lock().unwrap().stop(&settings);
                }
            }
            _ => {}
        }
    });
}

#[cfg(test)]
mod shell_banner_tests {
    use super::shell_banner;

    #[test]
    fn shell_banner_adds_platform_without_replacing_the_page_object() {
        let script = shell_banner("child");
        assert!(script.contains("window.TesseraShell||"), "{script}");
        assert!(script.contains("shell.platform="), "{script}");
        assert!(script.contains("shell.service=\"child\""), "{script}");
        let named = cfg!(target_os = "macos") && script.contains("macos")
            || cfg!(windows) && script.contains("windows")
            || cfg!(target_os = "linux") && script.contains("linux")
            || script.contains("other");
        assert!(named, "{script}");
    }
}
