//! LaunchAgent lifecycle commands for the workspace Settings page.
use crate::{config::Settings, core};
use serde::Serialize;
use std::process::Command;
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Manager, State, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

pub struct Server {
    settings: Settings,
    target: String,
    domain: String,
    operation: Mutex<()>,
}

#[derive(Serialize)]
pub struct Status {
    loaded: bool,
    running: bool,
}

impl Server {
    pub fn new(settings: Settings) -> Result<Self, String> {
        let output = Command::new("/usr/bin/plutil")
            .args(["-convert", "json", "-o", "-", &settings.launch_agent])
            .output().map_err(|e| e.to_string())?;
        if !output.status.success() { return Err("Cannot read configured LaunchAgent".into()); }
        let plist: serde_json::Value = serde_json::from_slice(&output.stdout).map_err(|e| e.to_string())?;
        let label = plist["Label"].as_str().ok_or("LaunchAgent has no Label")?;
        if label.is_empty() || label.contains('/') { return Err("Invalid LaunchAgent label".into()); }
        let uid = Command::new("/usr/bin/id").arg("-u").output().map_err(|e| e.to_string())?;
        if !uid.status.success() { return Err("Cannot resolve user domain".into()); }
        let domain = format!("gui/{}", String::from_utf8_lossy(&uid.stdout).trim());
        let target = format!("{domain}/{label}");
        Ok(Self { settings, target, domain, operation: Mutex::new(()) })
    }

    fn loaded(&self) -> Result<bool, String> {
        let result = Command::new("/bin/launchctl").args(["print", &self.target])
            .output().map_err(|e| e.to_string())?;
        if result.status.success() { return Ok(true); }
        let error = String::from_utf8_lossy(&result.stderr);
        if error.contains("Could not find service") { return Ok(false); }
        Err(format!("Cannot read server service: {}", error.trim()))
    }

    fn status(&self) -> Result<Status, String> {
        let origin = core::loopback_origin(&self.settings.attach_url)?;
        Ok(Status { loaded: self.loaded()?, running: core::probe(&origin, Duration::from_millis(600), &self.settings.auth_token) })
    }

    fn launchctl(&self, args: &[&str]) -> Result<(), String> {
        let result = Command::new("/bin/launchctl").args(args).output().map_err(|e| e.to_string())?;
        if !result.status.success() {
            return Err(format!("Server control failed: {}", String::from_utf8_lossy(&result.stderr).trim()));
        }
        Ok(())
    }

    fn change(&self, start: bool) -> Result<Status, String> {
        let _operation = self.operation.lock().map_err(|_| "Server control unavailable")?;
        let before = self.status()?;
        if start {
            if !before.loaded {
                if before.running { return Err("The address is served outside the configured LaunchAgent".into()); }
                self.launchctl(&["bootstrap", &self.domain, &self.settings.launch_agent])?;
            } else if !before.running {
                self.launchctl(&["kickstart", &self.target])?;
            }
        } else if before.loaded {
            // Removing the job also removes KeepAlive, so a deliberate stop stays stopped.
            self.launchctl(&["bootout", &self.target])?;
        } else if before.running {
            return Err("The address is served outside the configured LaunchAgent".into());
        }
        let deadline = Instant::now() + self.settings.startup();
        loop {
            let state = self.status()?;
            if state.running == start && state.loaded == start { return Ok(state); }
            if Instant::now() >= deadline {
                return Err("Server did not reach the requested state. Check the LaunchAgent logs.".into());
            }
            std::thread::sleep(Duration::from_millis(200));
        }
    }
}

fn check_window(window: &WebviewWindow, server: &Server) -> Result<(), String> {
    let origin = core::loopback_origin(&server.settings.attach_url)?;
    let current = window.url().map_err(|error| error.to_string())?;
    if window.label() != "main" || current.origin() != origin.origin() {
        return Err("Server controls belong to the configured workspace".into());
    }
    Ok(())
}

#[tauri::command]
pub async fn server_status(window: WebviewWindow, state: State<'_, Server>) -> Result<Status, String> {
    check_window(&window, &state)?;
    state.status()
}

#[tauri::command]
pub async fn server_change(window: WebviewWindow, state: State<'_, Server>, start: bool) -> Result<Status, String> {
    check_window(&window, &state)?;
    state.change(start)
}

fn open_workspace(app: &AppHandle, settings: &Settings) -> Result<(), String> {
    let origin = core::loopback_origin(&settings.attach_url)?;
    let allowed = origin.clone();
    let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::External(origin.clone()))
        .title(&settings.title)
        .initialization_script(include_str!("../../web/shell-errors.js"))
        .initialization_script(crate::shell_banner("launchd"))
        .initialization_script(
            "window.TesseraServer = Object.freeze({status: () => window.__TAURI_INTERNALS__.invoke('server_status'), change: start => window.__TAURI_INTERNALS__.invoke('server_change', {start})});"
        )
        .inner_size(settings.width, settings.height)
        .min_inner_size(settings.min_width, settings.min_height)
        .on_navigation(move |url| url.origin() == allowed.origin())
        .build().map_err(|error| error.to_string())?;
    crate::install_local_file_drop(&window, origin);
    Ok(())
}

pub fn run(settings: Settings, context: tauri::Context<tauri::Wry>) -> Result<(), String> {
    let server = Server::new(settings.clone())?;
    // Launching the app opens its workspace, as in the child-supervised mode.
    server.change(true)?;
    let app = tauri::Builder::default()
        .manage(server)
        .invoke_handler(tauri::generate_handler![server_status, server_change])
        .setup(move |app| {
            let origin = core::loopback_origin(&settings.attach_url)?;
            let permission = tauri::ipc::CapabilityBuilder::new("workspace-server")
                .window("main")
                .local(false)
                .remote(format!("{}/*", origin.origin().ascii_serialization()))
                .permission("server-controls");
            app.add_capability(permission)?;
            app.set_menu(crate::menu::build(app.handle(), &settings.title)?)?;
            app.on_menu_event(|app, event| crate::menu::handle(app, event.id().as_ref()));
            open_workspace(app.handle(), &settings)?;
            Ok(())
        })
        .build(context).map_err(|error| error.to_string())?;
    app.run(|_, _| {});
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn service_configuration_requires_one_owner_and_a_stable_address() {
        let mut settings = Settings::default();
        settings.launch_agent = "/example/core.plist".into();
        assert!(settings.clone().validate().is_err());
        settings.attach_url = "http://127.0.0.1:12345/".into();
        settings.keep_core = true;
        assert!(settings.validate().is_err());
    }
}
