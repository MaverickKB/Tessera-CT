//! Start and stop the core from the workspace page, on every platform.
//!
//! macOS can hand the core to launchd (`server.rs`). Linux and Windows have no
//! equivalent the app could own without a service install, so there the app is
//! the supervisor: it already spawns the core and respawns it when it dies
//! (`core.rs`). These two commands expose that same supervisor to the Settings
//! page, so Start server and Stop server are real buttons on all three
//! platforms instead of a sentence saying the controls are macOS-only.
use crate::{config::Settings, core::Core};
use serde::Serialize;
use std::sync::{Arc, Mutex};
use std::time::Instant;
use tauri::{State, WebviewWindow};

pub struct Supervisor {
    pub core: Arc<Mutex<Core>>,
    pub settings: Settings,
}

#[derive(Serialize)]
pub struct Status {
    /// This app can start the core: it owns one, or supervises a kept address.
    loaded: bool,
    running: bool,
}

impl Supervisor {
    fn lock(&self) -> Result<std::sync::MutexGuard<'_, Core>, String> {
        self.core.lock().map_err(|_| "Server control unavailable".to_string())
    }

    fn status(&self) -> Result<Status, String> {
        let mut core = self.lock()?;
        // A core the app attached to but never started (systemd, a terminal) is
        // not the app's to start or stop, so the page is told there is nothing
        // to control rather than offering buttons that would refuse.
        let loaded = !core.attached() || core.kept();
        Ok(Status { loaded, running: core.running() })
    }

    fn change(&self, start: bool) -> Result<Status, String> {
        {
            let mut core = self.lock()?;
            let running = core.running();
            if start && !running {
                core.restart(&self.settings)?;
            } else if !start && running {
                core.shutdown(&self.settings);
            }
        }
        // Report only the state that was asked for, and say so when it is not
        // reached in time instead of claiming success.
        let deadline = Instant::now() + self.settings.startup();
        loop {
            let state = self.status()?;
            if state.running == start {
                return Ok(state);
            }
            if Instant::now() >= deadline {
                return Err(if start {
                    "Server did not start. Check the core log in this home.".into()
                } else {
                    "Server did not stop. Check the core log in this home.".into()
                });
            }
            std::thread::sleep(std::time::Duration::from_millis(200));
        }
    }
}

fn check_window(window: &WebviewWindow, origin: url::Url) -> Result<(), String> {
    let current = window.url().map_err(|error| error.to_string())?;
    if window.label() != "main" || current.origin() != origin.origin() {
        return Err("Server controls belong to the configured workspace".into());
    }
    Ok(())
}

#[tauri::command]
pub async fn supervisor_status(window: WebviewWindow, state: State<'_, Supervisor>,
                           origin: State<'_, url::Url>) -> Result<Status, String> {
    check_window(&window, origin.inner().clone())?;
    state.status()
}

#[tauri::command]
pub async fn supervisor_change(window: WebviewWindow, state: State<'_, Supervisor>,
                           origin: State<'_, url::Url>, start: bool) -> Result<Status, String> {
    check_window(&window, origin.inner().clone())?;
    state.change(start)
}

/// The page-side bridge. Same shape the launchd mode installs, so
/// `web/desktop.js` renders one set of controls for every platform.
pub const PAGE_BRIDGE: &str = "window.TesseraServer = Object.freeze({status: () => window.__TAURI_INTERNALS__.invoke('supervisor_status'), change: start => window.__TAURI_INTERNALS__.invoke('supervisor_change', {start})});";

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_page_bridge_names_both_commands() {
        assert!(PAGE_BRIDGE.contains("supervisor_status"));
        assert!(PAGE_BRIDGE.contains("supervisor_change"));
        assert!(PAGE_BRIDGE.contains("window.TesseraServer"));
    }

    /// The buttons are only real if they move a live core. This starts one,
    /// stops it with the same call the page makes, and starts it again.
    #[test]
    fn stop_and_start_move_a_live_core() {
        let home = std::env::temp_dir()
            .join(format!("tessera-supervisor-{}", std::process::id()));
        std::fs::create_dir_all(&home).unwrap();
        for key in ["XAI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TESSERA_SERVER_TOKEN"] {
            std::env::remove_var(key);
        }
        let mut settings = Settings::default();
        settings.home = home.to_str().unwrap().to_string();
        settings.shutdown_seconds = 3.0;
        settings.terminate_seconds = 2.0;
        let core = Core::connect(&settings, &std::collections::HashMap::new()).expect("core starts");
        let supervisor = Supervisor {
            core: Arc::new(Mutex::new(core)),
            settings: settings.clone(),
        };

        let running = supervisor.status().expect("status");
        assert!(running.loaded && running.running, "a spawned core is loaded and running");

        let stopped = supervisor.change(false).expect("Stop server");
        assert!(!stopped.running, "Stop server must leave the core stopped");

        let started = supervisor.change(true).expect("Start server");
        assert!(started.running, "Start server must bring the core back");

        supervisor.change(false).ok();
    }
}
