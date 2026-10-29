//! Managed Python core: spawn `python -m tessera desktop`, validate its ready
//! record (docs/DESKTOP_IPC.md, version 1), and stop it on exit.
//!
//! With `keep_core`, the app is the supervisor instead: it spawns `serve`
//! detached on the configured port, never stops it, and respawns it while the
//! app is open. Closing the window leaves the core serving; the next app run
//! attaches to it rather than starting a second one.

use crate::config::{is_loopback, Settings};
use std::io::{BufRead, BufReader, Read};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant};
use url::Url;

pub struct Core {
    pub origin: Url,
    pub pid: u32,
    child: Option<Child>,
    stdin: Option<ChildStdin>,
    /// Never stop this core; it outlives the window.
    keep: bool,
    /// Enough to spawn it again after it exits on its own.
    home: Option<String>,
    env: std::collections::HashMap<String, String>,
    /// Key this core expects, so the app's own health checks can reach it.
    auth: String,
}

/// Parse a configured core URL: http, loopback IP, explicit port, root path only.
pub fn loopback_origin(raw: &str) -> Result<Url, String> {
    let url = Url::parse(raw).map_err(|_| "attach_url is not a URL".to_string())?;
    // A fragment is allowed: it names the page the window opens on (for example
    // `#view=settings`) and is never sent to the core, so it changes nothing
    // about the address this app probes or supervises. Everything else about
    // the URL still has to be a bare loopback origin.
    if url.scheme() != "http" || !is_loopback(url.host_str().unwrap_or("")) || url.port().is_none()
        || !url.username().is_empty() || url.password().is_some() || url.path() != "/"
        || url.query().is_some() {
        return Err("attach_url must be an http loopback origin with a port".into());
    }
    Ok(url)
}

/// True when a Tessera core answers GET /api/status at `origin` within `limit`.
///
/// `key` is sent as X-Tessera-Key when non-empty. A core in server.auth_mode
/// 'always' answers 401 without it, and this probe is what decides whether the
/// app attaches or respawns -- so omitting it would strand the desktop app on a
/// core it had just started. The default 'loopback' mode needs no key here.
pub fn probe(origin: &Url, limit: Duration, key: &str) -> bool {
    use std::io::Write;
    use std::net::{SocketAddr, TcpStream};
    let host = origin.host_str().unwrap_or("").trim_start_matches('[').trim_end_matches(']');
    let Ok(ip) = host.parse::<std::net::IpAddr>() else { return false };
    let port = origin.port().unwrap_or(0);
    let Ok(mut stream) = TcpStream::connect_timeout(&SocketAddr::new(ip, port), limit) else { return false };
    let _ = stream.set_read_timeout(Some(limit));
    let _ = stream.set_write_timeout(Some(limit));
    let authority = format!("{}:{}", origin.host_str().unwrap_or(""), port);
    let header = if key.is_empty() {
        String::new()
    } else {
        // The key is already validated as whitespace-free by Tessera's config, and
        // a CR/LF here would be a header injection. Refuse rather than send it.
        if key.contains(['\r', '\n']) {
            return false;
        }
        format!("X-Tessera-Key: {key}\r\n")
    };
    if write!(stream, "GET /api/status HTTP/1.1\r\nHost: {authority}\r\n{header}Connection: close\r\n\r\n").is_err() {
        return false;
    }
    let mut reply = Vec::new();
    let _ = stream.take(1 << 20).read_to_end(&mut reply);
    let text = String::from_utf8_lossy(&reply);
    (text.starts_with("HTTP/1.1 200") || text.starts_with("HTTP/1.0 200"))
        && text.contains("\"session_count\"")
}

/// Validate one readiness line and return the core's own loopback origin.
pub fn parse_ready(line: &str, pid: u32) -> Result<Url, String> {
    let record: serde_json::Value = serde_json::from_str(line.trim()).map_err(|_| "Invalid core startup reply")?;
    let obj = record.as_object().ok_or("Invalid core startup reply")?;
    let expected = ["type", "version", "transport", "url", "pid"];
    if obj.len() != expected.len() || !expected.iter().all(|k| obj.contains_key(*k)) {
        return Err("Invalid core startup reply".into());
    }
    if obj["type"] != "tessera.ready" || obj["version"] != 1 || obj["transport"] != "loopback-http" {
        return Err("Core startup failed".into());
    }
    // The line arrived on the pipe of the child this app spawned, so the pipe is
    // the provenance. The pid is a second check where the OS hands back the pid
    // of the process that writes: on Windows a virtualenv's python.exe is a
    // launcher, and the interpreter that answers is its child, so the numbers
    // legitimately differ and the reported pid only has to be present.
    let reported = obj["pid"].as_u64().unwrap_or(0);
    #[cfg(unix)]
    if reported != pid as u64 {
        return Err("Core startup failed".into());
    }
    #[cfg(not(unix))]
    if reported == 0 {
        return Err("Core startup failed".into());
    }
    let url = Url::parse(obj["url"].as_str().ok_or("Invalid core address")?).map_err(|_| "Invalid core address")?;
    let host = url.host_str().unwrap_or("");
    if url.scheme() != "http" || !is_loopback(host) || url.port().is_none() || !url.username().is_empty()
        || url.password().is_some() || url.path() != "/" || url.query().is_some() || url.fragment().is_some() {
        return Err("Invalid core address".into());
    }
    Ok(url)
}

impl Core {
    /// Attach to the configured live core if it answers; otherwise spawn one with
    /// the configured home. Both paths serve the same web UI from a core origin.
    pub fn connect(settings: &Settings, env: &std::collections::HashMap<String, String>) -> Result<Core, String> {
        let home = if !settings.home.is_empty() { settings.home.clone() }
                   else { env.get("TESSERA_HOME").cloned().unwrap_or_default() };
        if !settings.attach_url.is_empty() {
            let origin = loopback_origin(&settings.attach_url)?;
            if probe(&origin, settings.attach(), &settings.auth_token) {
                // Keep the home even when attaching: if this core dies while the
                // window is open, the app respawns it here rather than going dark.
                return Ok(Core { origin, pid: 0, child: None, stdin: None,
                                 keep: settings.keep_core,
                                 home: if home.is_empty() { None } else { Some(home) },
                                 env: env.clone(),
                                 auth: settings.auth_token.clone() });
            }
        }
        if home.is_empty() {
            return Err("No live core at attach_url and no Tessera home configured (set home in desktop.toml)".into());
        }
        if settings.keep_core {
            return Core::spawn_detached(settings, &home, env);
        }
        Core::launch_with_home(settings, Some(&home), env)
    }

    pub fn attached(&self) -> bool { self.child.is_none() }

    /// A kept core outlives this window; the app supervises the address, not one pid.
    pub fn kept(&self) -> bool { self.keep }

    pub fn launch(settings: &Settings, env: &std::collections::HashMap<String, String>) -> Result<Core, String> {
        Core::launch_with_home(settings, None, env)
    }

    /// PATH for a spawned core. A window opened from Finder inherits
    /// `/usr/bin:/bin:/usr/sbin:/sbin`, so the agent CLIs in `~/.local/bin`
    /// (codex, node, claude) would be invisible to the core without this.
    pub fn spawn_path(settings: &Settings, env: &std::collections::HashMap<String, String>) -> String {
        if !settings.spawn_path.is_empty() { return settings.spawn_path.clone(); }
        if cfg!(windows) {
            let mut parts: Vec<String> = Vec::new();
            let home = env.get("USERPROFILE").filter(|v| !v.is_empty())
                .or_else(|| env.get("HOME").filter(|v| !v.is_empty()));
            if let Some(home) = home {
                parts.push(format!("{home}\\.local\\bin"));
            }
            if let Some(existing) = env.get("PATH").filter(|v| !v.is_empty()) {
                for part in existing.split(';') {
                    if !part.is_empty() && !parts.iter().any(|p| p == part) { parts.push(part.to_string()); }
                }
            }
            return parts.join(";");
        }
        let mut parts: Vec<String> = Vec::new();
        if let Some(home) = env.get("HOME").filter(|v| !v.is_empty()) {
            parts.push(format!("{}/.local/bin", home));
        }
        for fixed in ["/opt/homebrew/bin", "/usr/local/bin"] { parts.push(fixed.to_string()); }
        if let Some(existing) = env.get("PATH").filter(|v| !v.is_empty()) {
            for part in existing.split(':') {
                if !part.is_empty() && !parts.iter().any(|p| p == part) { parts.push(part.to_string()); }
            }
        }
        parts.join(":")
    }

    fn command(settings: &Settings, home: Option<&str>, env: &std::collections::HashMap<String, String>) -> Command {
        let mut command = Command::new(settings.python());
        if let Some(home) = home { command.env("TESSERA_HOME", home); }
        command.env("PATH", Core::spawn_path(settings, env));
        command
    }

    fn launch_with_home(settings: &Settings, home: Option<&str>,
                        env: &std::collections::HashMap<String, String>) -> Result<Core, String> {
        let mut command = Core::command(settings, home, env);
        let mut child = command
            .args(["-u", "-m", "tessera", "desktop", "--host", &settings.core_host,
                   "--port", &settings.core_port.to_string()])
            .current_dir(settings.core_root())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|_| "Python core could not start".to_string())?;
        let pid = child.id();
        let stdin = child.stdin.take();
        // Core diagnostics can contain private configuration: drain, never forward.
        if let Some(mut err) = child.stderr.take() {
            std::thread::spawn(move || { let _ = std::io::copy(&mut err, &mut std::io::sink()); });
        }
        let stdout = child.stdout.take().ok_or("Python core has no stdout")?;
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let mut reader = BufReader::new(stdout.take(65536));
            let mut line = String::new();
            let result = reader.read_line(&mut line).map(|_| line);
            let _ = tx.send(result);
            // Keep draining so the core never blocks on a full pipe.
            let mut rest = reader.into_inner().into_inner();
            let _ = std::io::copy(&mut rest, &mut std::io::sink());
        });
        let mut core = Core { origin: Url::parse("http://127.0.0.1/").unwrap(), pid, child: Some(child), stdin,
                              keep: false, home: home.map(str::to_string), env: env.clone(),
                              auth: settings.auth_token.clone() };
        match rx.recv_timeout(settings.startup()) {
            Ok(Ok(line)) if !line.is_empty() => match parse_ready(&line, pid) {
                Ok(origin) => { core.origin = origin; Ok(core) }
                Err(e) => { core.kill(); Err(e) }
            },
            Ok(_) => { core.kill(); Err("Python core exited during startup".into()) }
            Err(_) => { core.kill(); Err("Core startup timed out".into()) }
        }
    }

    /// Spawn `serve` on the configured attach_url, detached from this window.
    /// stdin is null and output goes to `<home>/logs/core.log`, so the core keeps
    /// running when the window closes. `serve` drains on SIGTERM like a restart,
    /// so a deliberate stop is still graceful.
    fn spawn_detached(settings: &Settings, home: &str,
                      env: &std::collections::HashMap<String, String>) -> Result<Core, String> {
        let origin = loopback_origin(&settings.attach_url)?;
        let host = origin.host_str().unwrap_or("127.0.0.1").to_string();
        let port = origin.port().ok_or("attach_url has no port")?;
        let log_path = std::path::Path::new(home).join("logs").join("core.log");
        if let Some(dir) = log_path.parent() {
            std::fs::create_dir_all(dir).map_err(|_| "Core log directory could not be created".to_string())?;
        }
        let out = std::fs::OpenOptions::new().create(true).append(true).open(&log_path)
            .map_err(|_| "Core log file could not be opened".to_string())?;
        let err = out.try_clone().map_err(|_| "Core log file could not be opened".to_string())?;
        let mut command = Core::command(settings, Some(home), env);
        let child = command
            .args(["-u", "-m", "tessera", "--home", home, "serve",
                   "--host", &host, "--port", &port.to_string()])
            .current_dir(settings.core_root())
            .stdin(Stdio::null())
            .stdout(Stdio::from(out))
            .stderr(Stdio::from(err))
            .spawn()
            .map_err(|_| "Python core could not start".to_string())?;
        let pid = child.id();
        let mut core = Core { origin, pid, child: Some(child), stdin: None,
                              keep: true, home: Some(home.to_string()), env: env.clone(),
                              auth: settings.auth_token.clone() };
        if !core.wait_ready(settings) {
            core.kill();
            return Err("Core did not answer on its configured port (see <home>/logs/core.log)".into());
        }
        Ok(core)
    }

    fn wait_ready(&mut self, settings: &Settings) -> bool {
        let deadline = Instant::now() + settings.startup();
        while Instant::now() < deadline {
            if let Some(child) = self.child.as_mut() {
                if !matches!(child.try_wait(), Ok(None)) { return false; }
            }
            if probe(&self.origin, Duration::from_millis(300), &self.auth) { return true; }
            std::thread::sleep(Duration::from_millis(100));
        }
        false
    }

    /// Start the core again at the address this app was using.
    ///
    /// With an `attach_url` the address is configured, so `respawn` applies.
    /// Without one the app chose the port when it launched the core, and a
    /// restart must come back on that same port or the open window is left
    /// pointing at nothing. This is the path Start server takes after Stop in
    /// the default mode on Linux and Windows.
    pub fn restart(&mut self, settings: &Settings) -> Result<(), String> {
        if !settings.attach_url.is_empty() {
            return self.respawn(settings);
        }
        let mut same = settings.clone();
        same.attach_url = self.origin.to_string();
        let home = self.home.clone().ok_or("No Tessera home configured for a restarted core")?;
        let env = self.env.clone();
        let mut fresh = Core::spawn_detached(&same, &home, &env)?;
        self.pid = fresh.pid;
        if let Some(mut dead) = self.child.take() {
            let _ = dead.wait();
        }
        self.child = fresh.child.take();
        self.origin = fresh.origin.clone();
        Ok(())
    }

    /// Start a fresh detached core at the same address after the last one exited.
    pub fn respawn(&mut self, settings: &Settings) -> Result<(), String> {
        let home = self.home.clone().ok_or("No Tessera home configured for a kept core")?;
        let env = self.env.clone();
        let mut fresh = Core::spawn_detached(settings, &home, &env)?;
        self.pid = fresh.pid;
        // Reap the child that exited, so it does not linger as a zombie.
        if let Some(mut dead) = self.child.take() {
            let _ = dead.wait();
        }
        self.child = fresh.child.take();
        self.origin = fresh.origin.clone();
        Ok(())
    }

    pub fn running(&mut self) -> bool {
        if self.keep {
            // The app supervises the address: an attached core and our own child
            // both have to answer, or the app respawns one.
            return probe(&self.origin, Duration::from_millis(600), &self.auth);
        }
        match self.child.as_mut() {
            Some(child) => matches!(child.try_wait(), Ok(None)),
            None => true, // An attached core is managed elsewhere (launchd); restarts are expected.
        }
    }

    fn wait_for(&mut self, limit: Duration) -> bool {
        let deadline = Instant::now() + limit;
        while Instant::now() < deadline {
            if !self.running() { return true; }
            std::thread::sleep(Duration::from_millis(25));
        }
        !self.running()
    }

    fn kill(&mut self) {
        // On Windows a virtualenv's python.exe is a launcher whose child is the
        // interpreter that owns the port, so killing the handle alone leaves the
        // core serving. taskkill /T takes the tree.
        #[cfg(windows)]
        {
            let _ = std::process::Command::new("taskkill")
                .args(["/T", "/F", "/PID", &self.pid.to_string()])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
        }
        if let Some(child) = self.child.as_mut() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }

    /// Close stdin (graceful), then SIGTERM, then kill; always reaps the child.
    pub fn stop(&mut self, settings: &Settings) {
        if self.keep { return; } // A kept core outlives the window by design.
        self.shutdown(settings);
    }

    /// Stop the core on the operator's explicit request.
    ///
    /// Unlike `stop`, this also stops a kept core, because Stop server in the
    /// workspace is a deliberate act and must not be answered with "still
    /// running". A core this app attached to but never started stays untouched.
    pub fn shutdown(&mut self, settings: &Settings) {
        if self.attached() { return; } // Never stop a core this app did not start.
        drop(self.stdin.take());
        if self.wait_for(settings.shutdown()) { return; }
        #[cfg(unix)]
        unsafe { libc::kill(self.pid as i32, libc::SIGTERM); }
        if self.wait_for(settings.terminate()) { return; }
        self.kill();
    }
}

impl Drop for Core {
    fn drop(&mut self) {
        if self.keep { return; }
        if !self.attached() && self.running() { self.kill(); }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ready_record_accepts_only_the_v1_loopback_contract() {
        let ok = r#"{"type":"tessera.ready","version":1,"transport":"loopback-http","url":"http://127.0.0.1:12345","pid":42}"#;
        assert_eq!(parse_ready(ok, 42).unwrap().as_str(), "http://127.0.0.1:12345/");
        let v6 = r#"{"type":"tessera.ready","version":1,"transport":"loopback-http","url":"http://[::1]:5000","pid":7}"#;
        assert!(parse_ready(v6, 7).is_ok());
        #[cfg(unix)]
        assert!(parse_ready(ok, 43).is_err(), "pid must be the owned child");
        #[cfg(not(unix))]
        assert!(parse_ready(ok, 43).is_ok(), "a Windows venv launcher reports the interpreter pid");
        for url in ["https://127.0.0.1:1", "http://0.0.0.0:1", "http://example.com:1", "http://127.0.0.1",
                    "http://127.0.0.1:1/private", "http://user@127.0.0.1:1", "http://127.0.0.1:1/?q", "http://127.evil.example:1"] {
            let line = format!(r#"{{"type":"tessera.ready","version":1,"transport":"loopback-http","url":"{url}","pid":1}}"#);
            assert!(parse_ready(&line, 1).is_err(), "{url}");
        }
        let extra = r#"{"type":"tessera.ready","version":1,"transport":"loopback-http","url":"http://127.0.0.1:1","pid":1,"token":"x"}"#;
        assert!(parse_ready(extra, 1).is_err(), "no extra fields such as tokens");
        assert!(parse_ready(r#"{"type":"tessera.ready","version":2,"transport":"loopback-http","url":"http://127.0.0.1:1","pid":1}"#, 1).is_err());
        assert!(parse_ready("not json", 1).is_err());
    }

    #[test]
    fn attach_url_is_loopback_only_and_missing_home_refuses_to_spawn() {
        assert!(loopback_origin("http://127.0.0.1:8740/").is_ok());
        for bad in ["https://127.0.0.1:8740/", "http://example.com:8740/", "http://127.0.0.1/", "http://127.0.0.1:1/x"] {
            assert!(loopback_origin(bad).is_err(), "{bad}");
        }
        let closed = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = closed.local_addr().unwrap().port();
        drop(closed);
        let mut settings = Settings::default();
        settings.attach_url = format!("http://127.0.0.1:{port}/");
        assert!(!probe(&loopback_origin(&settings.attach_url).unwrap(), settings.attach(), ""));
        let err = Core::connect(&settings, &std::collections::HashMap::new()).err().unwrap();
        assert!(err.contains("no Tessera home"), "{err}");
    }

    #[test]
    fn attaches_to_a_running_core_without_owning_it() {
        let home = std::env::temp_dir().join(format!("tessera-desktop-attach-{}", std::process::id()));
        std::fs::create_dir_all(&home).unwrap();
        let settings = Settings::default();
        let env = std::collections::HashMap::new();
        let mut owned = Core::launch_with_home(&settings, Some(home.to_str().unwrap()), &env).expect("core starts");
        let mut attach = Settings::default();
        attach.attach_url = owned.origin.to_string();
        let mut attached = Core::connect(&attach, &std::collections::HashMap::new()).expect("attaches");
        assert!(attached.attached() && attached.origin == owned.origin && attached.pid == 0);
        attached.stop(&attach);
        assert!(owned.running(), "stopping an attached core is a no-op");
        owned.stop(&settings);
        std::fs::remove_dir_all(home).ok();
    }

    fn http(origin: &Url, host_header: &str, origin_header: Option<&str>) -> u16 {
        use std::io::Write;
        let address = format!("{}:{}", origin.host_str().unwrap(), origin.port().unwrap());
        let mut stream = std::net::TcpStream::connect(address).unwrap();
        let extra = origin_header.map(|o| format!("Origin: {o}\r\n")).unwrap_or_default();
        write!(stream, "GET /api/status HTTP/1.1\r\nHost: {host_header}\r\n{extra}Connection: close\r\n\r\n").unwrap();
        let mut reply = String::new();
        stream.read_to_string(&mut reply).unwrap();
        reply.split_whitespace().nth(1).unwrap().parse().unwrap()
    }

    #[test]
    fn real_core_launches_on_loopback_serves_the_ui_origin_and_stops_on_stdin_close() {
        let home = std::env::temp_dir().join(format!("tessera-desktop-core-{}", std::process::id()));
        std::fs::create_dir_all(&home).unwrap();
        std::env::set_var("TESSERA_HOME", &home);
        for key in ["XAI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TESSERA_SERVER_TOKEN"] {
            std::env::remove_var(key);
        }
        let settings = Settings::default();
        let mut core = Core::launch(&settings, &std::collections::HashMap::new()).expect("core starts");
        assert!(core.running());
        let host = format!("{}:{}", core.origin.host_str().unwrap(), core.origin.port().unwrap());
        let same = core.origin.as_str().trim_end_matches('/').to_string();
        assert_eq!(http(&core.origin, &host, None), 200);
        assert_eq!(http(&core.origin, &host, Some(&same)), 200, "the window's own origin passes the fence");
        assert_eq!(http(&core.origin, "rebound.example", None), 403);
        assert_eq!(http(&core.origin, &host, Some("https://attacker.example")), 403);
        let started = Instant::now();
        core.stop(&settings);
        assert!(!core.running());
        assert!(started.elapsed() < settings.shutdown(), "stdin close is a graceful stop");
        std::fs::remove_dir_all(home).ok();
    }

    /// A kept core is spawned detached on a fixed port, survives the window's
    /// stop and drop, and the next run attaches to the same pid instead of
    /// starting a second core.
    #[test]
    fn kept_core_outlives_the_window_and_the_next_run_attaches_to_it() {
        let home = std::env::temp_dir().join(format!("tessera-desktop-keep-{}", std::process::id()));
        std::fs::create_dir_all(&home).unwrap();
        let env = std::collections::HashMap::new();
        let mut settings = Settings::default();
        settings.keep_core = true;
        settings.home = home.to_str().unwrap().to_string();
        let port = {
            let probe = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
            probe.local_addr().unwrap().port()
        };
        settings.attach_url = format!("http://127.0.0.1:{port}/");

        let mut first = Core::connect(&settings, &env).expect("kept core starts");
        assert!(first.kept() && first.pid != 0, "the app spawned it, so it owns a pid");
        let pid = first.pid;
        let origin = first.origin.clone();
        assert_eq!(http(&origin, &format!("127.0.0.1:{port}"), None), 200);

        // Closing the window: stop and drop must both leave the core serving.
        first.stop(&settings);
        drop(first);
        std::thread::sleep(Duration::from_millis(200));
        assert_eq!(http(&origin, &format!("127.0.0.1:{port}"), None), 200, "kept core still answers");

        // Next run: the same address answers, so nothing new is spawned.
        let second = Core::connect(&settings, &env).expect("attaches");
        assert!(second.attached(), "attached to the kept core, not spawned again");
        assert_eq!(second.origin, origin);

        // A deliberate stop of a kept core is still graceful (SIGTERM drains).
        #[cfg(unix)]
        unsafe { libc::kill(pid as i32, libc::SIGTERM); }
        let deadline = Instant::now() + settings.shutdown();
        while Instant::now() < deadline && probe(&origin, Duration::from_millis(200), "") {
            std::thread::sleep(Duration::from_millis(50));
        }
        assert!(!probe(&origin, Duration::from_millis(200), ""), "SIGTERM stops a kept core");
        std::fs::remove_dir_all(home).ok();
    }

    #[test]
    fn spawn_path_puts_the_user_bin_ahead_of_the_inherited_path() {
        if cfg!(windows) {
            let mut env = std::collections::HashMap::new();
            env.insert("USERPROFILE".to_string(), r"C:\Users\someone".to_string());
            env.insert("HOME".to_string(), r"D:\ignored".to_string());
            env.insert("PATH".to_string(), r"C:\Windows\System32".to_string());
            assert_eq!(
                Core::spawn_path(&Settings::default(), &env),
                r"C:\Users\someone\.local\bin;C:\Windows\System32",
            );
            let mut explicit = Settings::default();
            explicit.spawn_path = r"C:\custom\bin".to_string();
            assert_eq!(Core::spawn_path(&explicit, &env), r"C:\custom\bin");
            return;
        }
        let mut env = std::collections::HashMap::new();
        env.insert("HOME".to_string(), "/Users/someone".to_string());
        env.insert("USERPROFILE".to_string(), r"C:\Users\someone".to_string());
        env.insert("PATH".to_string(), "/usr/bin:/bin:/usr/sbin:/sbin".to_string());
        let path = Core::spawn_path(&Settings::default(), &env);
        assert_eq!(path, "/Users/someone/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin");
        assert!(!path.contains("C:"), "{path}");
        // No duplicate of a directory that is already there, and an explicit value wins.
        let mut env2 = std::collections::HashMap::new();
        env2.insert("HOME".to_string(), "/Users/someone".to_string());
        env2.insert("PATH".to_string(), "/opt/homebrew/bin:/usr/bin".to_string());
        assert_eq!(Core::spawn_path(&Settings::default(), &env2),
                   "/Users/someone/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin");
        let mut explicit = Settings::default();
        explicit.spawn_path = "/custom/bin".to_string();
        assert_eq!(Core::spawn_path(&explicit, &env2), "/custom/bin");
    }
}

#[cfg(test)]
mod auth_probe_tests {
    use super::*;
    use std::net::TcpListener;

    /// A core in server.auth_mode 'always' answers 401 without the key. The
    /// desktop app's readiness probe is what decides whether it attaches or
    /// respawns, so a probe that cannot present the key would strand the app on
    /// a core it had just started. This is the regression guard for that.
    #[test]
    fn probe_cannot_reach_an_always_mode_core_without_the_key_but_can_with_it() {
        // Serve one canned 401 and one canned 200, so the test exercises the real
        // socket path rather than a mock of it.
        fn serve(status: &'static str) -> u16 {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let port = listener.local_addr().unwrap().port();
            std::thread::spawn(move || {
                // Accept repeatedly: the test probes three times.
                for _ in 0..8 {
                    let Ok((mut stream, _)) = listener.accept() else { break };
                    use std::io::{Read, Write};
                    // Read until the header terminator. A single read() can return
                    // only part of the request, which made this test fail only under
                    // a full parallel run -- the key header was sometimes not yet in
                    // the buffer. That was a flaky test, not a flaky gate.
                    let mut request = Vec::new();
                    let mut buf = [0u8; 512];
                    loop {
                        match stream.read(&mut buf) {
                            Ok(0) => break,
                            Ok(n) => {
                                request.extend_from_slice(&buf[..n]);
                                if request.windows(4).any(|w| w == b"\r\n\r\n") { break; }
                            }
                            Err(_) => break,
                        }
                    }
                    let request = String::from_utf8_lossy(&request).to_string();
                    let body = "{\"session_count\":0}";
                    let (code, extra) = if status == "200" {
                        ("200 OK", "")
                    } else if request.to_ascii_lowercase().contains("x-tessera-key: right-key") {
                        ("200 OK", "")
                    } else {
                        ("401 Unauthorized", "")
                    };
                    let _ = write!(stream, "HTTP/1.1 {code}\r\nContent-Length: {}\r\n{extra}Connection: close\r\n\r\n{body}",
                                   body.len());
                }
            });
            port
        }

        let port = serve("auth");
        let origin = Url::parse(&format!("http://127.0.0.1:{port}/")).unwrap();
        // Generous on purpose. This test is about whether the key is SENT, not how
        // fast the peer answers: under a full parallel run the fake server's thread
        // can be scheduled late, and a tight limit made this fail only in the suite.
        let limit = Duration::from_secs(10);
        assert!(!probe(&origin, limit, ""), "no key must not reach an always-mode core");
        assert!(!probe(&origin, limit, "wrong-key"), "a wrong key must not reach it");
        assert!(probe(&origin, limit, "right-key"), "the configured key must reach it");

        // A newline in the key would be a header injection; refuse rather than send.
        assert!(!probe(&origin, limit, "right-key\r\nX-Evil: 1"));
    }

    /// The default posture must stay untouched: no key configured, no header sent.
    #[test]
    fn probe_with_no_key_still_reaches_a_plain_core() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            // Serve every connection, not just the first. The test below retries
            // against a deadline, so a late-scheduled thread that misses one window
            // still answers the next attempt instead of leaving the port dead.
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { break };
                use std::io::{Read, Write};
                let mut buf = [0u8; 2048];
                let n = stream.read(&mut buf).unwrap_or(0);
                let request = String::from_utf8_lossy(&buf[..n]).to_ascii_lowercase();
                let body = "{\"session_count\":0}";
                let code = if request.contains("x-tessera-key") {
                    "401 Unauthorized"
                } else {
                    "200 OK"
                };
                let _ = write!(stream, "HTTP/1.1 {code}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                               body.len());
            }
        });
        let origin = Url::parse(&format!("http://127.0.0.1:{port}/")).unwrap();
        // This asserts that NO KEY is sent to a plain core. It is not an assertion
        // about how fast the fake server's thread is scheduled: under a full parallel
        // run on a loaded machine a single probe can miss its window even though the
        // request was correct. Retry inside a real deadline instead.
        let deadline = std::time::Instant::now() + Duration::from_secs(30);
        while std::time::Instant::now() < deadline {
            if probe(&origin, Duration::from_secs(10), "") {
                return;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        panic!("no key was refused by a plain core for 30s; the request itself is wrong");
    }
}

#[cfg(test)]
mod auth_end_to_end_tests {
    use super::*;

    /// The stranding case, end to end, using the app's OWN spawn path.
    ///
    /// A real core in auth_mode 'always' requires the key on every request. The
    /// app's readiness probe is what decides whether it attaches or spawns, so a
    /// probe that cannot present the key leaves the operator looking at a window
    /// that gets 401 on every call. This proves the key is what closes that gap.
    #[test]
    fn a_keyed_core_is_reachable_only_when_the_desktop_has_the_key() {
        let home = std::env::temp_dir().join(format!("tessera-auth-e2e-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&home);
        std::fs::create_dir_all(&home).unwrap();
        std::fs::write(home.join("config.toml"),
            "[server]\nhost = \"127.0.0.1\"\nport = 0\nauth_token = \"e2e-key\"\nauth_mode = \"always\"\n").unwrap();
        std::env::set_var("TESSERA_HOME", &home);
        for key in ["XAI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TESSERA_SERVER_TOKEN"] {
            std::env::remove_var(key);
        }

        // Start it with the key, through the app's real launcher.
        let mut settings = Settings::default();
        settings.auth_token = "e2e-key".into();
        let mut core = Core::launch(&settings, &std::collections::HashMap::new()).expect("keyed core starts");
        assert!(core.running(), "the app must see its own keyed core as running");
        let origin = core.origin.clone();

        // The same core, probed WITHOUT the key: unreachable, which is what the
        // gate is for.
        assert!(!probe(&origin, Duration::from_millis(600), ""),
                "an always-mode core must refuse a keyless probe");
        // And with the key it answers.
        assert!(probe(&origin, Duration::from_millis(600), "e2e-key"),
                "the configured key must reach it");

        core.stop(&settings);
        assert!(!core.running());
        std::fs::remove_dir_all(&home).ok();
    }
}
