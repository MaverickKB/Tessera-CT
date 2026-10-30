//! Shell settings: built-in defaults, then an optional TOML file, then env.
//!
//! File: `$TESSERA_DESKTOP_CONFIG`, else `<TESSERA_HOME or ~/.tessera>/desktop.toml`.
//! Env: `TESSERA_DESKTOP_<FIELD>` in upper case, e.g. `TESSERA_DESKTOP_CORE_PORT`.

use serde::Deserialize;
use std::collections::HashMap;
use std::net::IpAddr;
use std::path::{Path, PathBuf};
use std::time::Duration;

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Settings {
    /// Python interpreter. Empty: `<core_root>/.venv/bin/python` if present, else `python3`.
    pub python: String,
    /// Directory the core runs in (the Tessera checkout). Empty: the build-time checkout.
    pub core_root: String,
    pub core_host: String,
    /// Zero asks the OS for a free port.
    pub core_port: u16,
    pub startup_seconds: f64,
    pub shutdown_seconds: f64,
    pub terminate_seconds: f64,
    pub width: f64,
    pub height: f64,
    pub min_width: f64,
    pub min_height: f64,
    pub title: String,
    /// Live core to attach to (loopback http origin). Empty disables attaching.
    pub attach_url: String,
    /// Tessera home for a spawned core when none is running. Empty: TESSERA_HOME;
    /// with neither set the app refuses to spawn (never an empty default home).
    pub home: String,
    pub attach_seconds: f64,
    /// Keep the core after the window closes. The app spawns `serve` detached,
    /// never stops it, and respawns it while the app is open. Needs attach_url:
    /// the kept core must sit on a stable address so a later app run attaches to
    /// it instead of starting a second core.
    pub keep_core: bool,
    /// PATH for a spawned core. Empty: this app's PATH with $HOME/.local/bin,
    /// /opt/homebrew/bin and /usr/local/bin in front, because a window launched
    /// from Finder inherits /usr/bin:/bin:/usr/sbin:/sbin and the agent CLIs
    /// (codex, node) live in ~/.local/bin.
    pub spawn_path: String,
    /// LaunchAgent plist owned by the native server controls. Empty uses child lifecycle.
    pub launch_agent: String,
    /// Shared key for a core with server.auth_mode = 'always'. Empty
    /// means no key, which is correct for the default loopback mode. Without this
    /// the readiness probe below would get a 401 and the app would refuse to
    /// attach to a core it just started.
    pub auth_token: String,
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            python: String::new(),
            core_root: String::new(),
            core_host: "127.0.0.1".into(),
            core_port: 0,
            startup_seconds: 30.0,
            shutdown_seconds: 5.0,
            terminate_seconds: 2.0,
            width: 1280.0,
            height: 900.0,
            min_width: 390.0,
            min_height: 540.0,
            title: "Tessera".into(),
            attach_url: String::new(),
            home: String::new(),
            attach_seconds: 2.0,
            keep_core: false,
            spawn_path: String::new(),
            launch_agent: String::new(),
            auth_token: String::new(),
        }
    }
}

pub fn is_loopback(host: &str) -> bool {
    let bare = host.trim_start_matches('[').trim_end_matches(']');
    bare.parse::<IpAddr>().map(|ip| ip.is_loopback()).unwrap_or(false)
}

fn seconds(name: &str, value: f64) -> Result<Duration, String> {
    if !value.is_finite() || value <= 0.0 || value > 3600.0 {
        return Err(format!("{name} must be between 0 and 3600 seconds"));
    }
    Ok(Duration::from_secs_f64(value))
}

impl Settings {
    pub fn startup(&self) -> Duration { seconds("startup_seconds", self.startup_seconds).unwrap() }
    pub fn shutdown(&self) -> Duration { seconds("shutdown_seconds", self.shutdown_seconds).unwrap() }
    pub fn terminate(&self) -> Duration { seconds("terminate_seconds", self.terminate_seconds).unwrap() }

    pub fn validate(self) -> Result<Self, String> {
        if !self.launch_agent.is_empty() {
            if !cfg!(target_os = "macos") || !Path::new(&self.launch_agent).is_absolute() {
                return Err("launch_agent requires an absolute macOS LaunchAgent plist path".into());
            }
            if self.keep_core || self.attach_url.is_empty() {
                return Err("launch_agent requires attach_url and keep_core = false".into());
            }
        }
        if !is_loopback(&self.core_host) {
            return Err("core_host must be a loopback IP address".into());
        }
        seconds("startup_seconds", self.startup_seconds)?;
        seconds("shutdown_seconds", self.shutdown_seconds)?;
        seconds("terminate_seconds", self.terminate_seconds)?;
        seconds("attach_seconds", self.attach_seconds)?;
        if !self.attach_url.is_empty() {
            crate::core::loopback_origin(&self.attach_url)?;
        }
        if self.keep_core && self.attach_url.is_empty() {
            return Err("keep_core needs attach_url so the kept core has a stable address".into());
        }
        for (name, value) in [("width", self.width), ("height", self.height),
                              ("min_width", self.min_width), ("min_height", self.min_height)] {
            if !value.is_finite() || !(100.0..=16384.0).contains(&value) {
                return Err(format!("{name} must be between 100 and 16384"));
            }
        }
        if self.min_width > self.width || self.min_height > self.height {
            return Err("minimum window size exceeds the window size".into());
        }
        Ok(self)
    }

    pub fn attach(&self) -> Duration { seconds("attach_seconds", self.attach_seconds).unwrap() }

    pub fn core_root(&self) -> PathBuf {
        if !self.core_root.is_empty() {
            return PathBuf::from(&self.core_root);
        }
        // The build-time checkout, valid only while a checkout produced this
        // binary and its `.venv`. An installed `.deb`/`.AppImage` on a clean
        // machine has no checkout here: fall back to the current directory
        // rather than spawning the core into a path that does not exist.
        let checkout = Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
        if checkout.join("pyproject.toml").is_file() {
            return checkout;
        }
        std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
    }

    pub fn python(&self) -> PathBuf {
        if !self.python.is_empty() {
            return PathBuf::from(&self.python);
        }
        let root = self.core_root();
        let local = if cfg!(windows) { root.join(".venv").join("Scripts").join("python.exe") }
                    else { root.join(".venv").join("bin").join("python") };
        if local.exists() { local } else if cfg!(windows) { "python".into() } else { "python3".into() }
    }
}

/// Platform config locations, first existing file wins.
///
/// `$HOME/.config/tessera/desktop.toml` on every system, then
/// `%APPDATA%\tessera\desktop.toml` on Windows. An empty list means the
/// caller falls through to the Tessera home.
fn config_candidates(env: &HashMap<String, String>) -> Vec<PathBuf> {
    let mut paths = Vec::new();
    if let Some(home) = env.get("HOME").filter(|v| !v.is_empty()) {
        paths.push(PathBuf::from(home).join(".config").join("tessera").join("desktop.toml"));
    }
    if cfg!(windows) {
        if let Some(appdata) = env.get("APPDATA").filter(|v| !v.is_empty()) {
            paths.push(PathBuf::from(appdata).join("tessera").join("desktop.toml"));
        }
    }
    paths
}

fn home_dir(env: &HashMap<String, String>) -> Option<PathBuf> {
    if let Some(home) = env.get("TESSERA_HOME").filter(|v| !v.is_empty()) {
        return Some(PathBuf::from(home));
    }
    env.get("HOME").or_else(|| env.get("USERPROFILE")).map(|h| Path::new(h).join(".tessera"))
}

/// Persistent nonsecret selection written by the public Python launcher.
/// Keep this separate from desktop.toml so installation never rewrites an
/// operator's window, attach, service, or lifecycle settings.
fn bootstrap_path(env: &HashMap<String, String>) -> Option<PathBuf> {
    let profile = env.get("USERPROFILE").filter(|value| !value.is_empty())
        .or_else(|| env.get("HOME").filter(|value| !value.is_empty()));
    #[cfg(windows)]
    {
        let directory = env.get("APPDATA").filter(|value| !value.is_empty()).map(PathBuf::from)
            .or_else(|| profile.map(|value| Path::new(value).join("AppData").join("Roaming")))?;
        Some(directory.join("tessera").join("desktop-bootstrap.json"))
    }
    #[cfg(not(windows))]
    {
        let home = env.get("HOME").filter(|value| !value.is_empty()).or(profile)?;
        Some(Path::new(home).join(".config").join("tessera").join("desktop-bootstrap.json"))
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Bootstrap { home: String, python: String, core_root: String }

fn read_bootstrap(env: &HashMap<String, String>) -> Result<Option<Bootstrap>, String> {
    let Some(path) = bootstrap_path(env).filter(|path| path.exists()) else { return Ok(None); };
    let data = std::fs::read_to_string(path)
        .map_err(|_| "Native menu launch selection is unreadable; run tessera start to repair it".to_string())?;
    let record: Bootstrap = serde_json::from_str(&data)
        .map_err(|_| "Native menu launch selection is invalid; run tessera --home <home> start to repair it".to_string())?;
    if record.home.is_empty() || record.python.is_empty() || record.core_root.is_empty() {
        return Err("Native menu launch selection is empty; run tessera --home <home> start to repair it".into());
    }
    Ok(Some(record))
}

fn missing_bootstrap_fields(table: &toml::Table, env: &HashMap<String, String>) -> Vec<&'static str> {
    ["home", "python", "core_root"].into_iter().filter(|field| {
        let name = format!("TESSERA_DESKTOP_{}", field.to_uppercase());
        // Plain TESSERA_HOME is also an explicit native core selection.
        let explicit_home = *field == "home" && env.get("TESSERA_HOME").is_some_and(|value| !value.is_empty());
        !env.contains_key(&name) && !explicit_home && match table.get(*field) {
            None => true,
            Some(value) => value.as_str() == Some(""),
        }
    }).collect()
}

/// Load settings from `env` (a snapshot of the process environment).
pub fn load(env: &HashMap<String, String>) -> Result<Settings, String> {
    let explicit = env.get("TESSERA_DESKTOP_CONFIG").filter(|v| !v.is_empty()).map(PathBuf::from);
    let user_config = config_candidates(env).into_iter().find(|p| p.exists());
    let explicit_home = env.get("TESSERA_DESKTOP_HOME").filter(|value| !value.is_empty())
        .or_else(|| env.get("TESSERA_HOME").filter(|value| !value.is_empty()));
    let mut bootstrap = None;
    let mut bootstrap_read = false;
    let path = if let Some(path) = explicit.clone().or(user_config) {
        Some(path)
    } else {
        // A menu launch has no transient CLI environment. Select the remembered
        // home's desktop.toml before applying fallback fields, so its operator
        // lifecycle, authentication and window settings are preserved too.
        if explicit_home.is_none() {
            bootstrap = read_bootstrap(env)?;
            bootstrap_read = true;
        }
        explicit_home.map(PathBuf::from)
            .or_else(|| bootstrap.as_ref().map(|record| PathBuf::from(&record.home)))
            .or_else(|| home_dir(env))
            .map(|home| home.join("desktop.toml"))
    };
    let mut table: toml::Table = match path {
        Some(ref p) if p.exists() => std::fs::read_to_string(p)
            .map_err(|_| "desktop settings file is unreadable".to_string())?
            .parse()
            .map_err(|e: toml::de::Error| format!("desktop settings file is invalid: {}", e.message()))?,
        Some(_) if explicit.is_some() => return Err("TESSERA_DESKTOP_CONFIG names a missing file".into()),
        _ => toml::Table::new(),
    };
    let missing = missing_bootstrap_fields(&table, env);
    if !missing.is_empty() && !bootstrap_read {
        bootstrap = read_bootstrap(env)?;
    }
    if let Some(record) = bootstrap {
        for field in missing {
            let value = match field {
                "home" => &record.home, "python" => &record.python, _ => &record.core_root,
            };
            table.insert(field.into(), toml::Value::String(value.clone()));
        }
    }
    let defaults = toml::Table::try_from(Settings::default()).expect("serializable defaults");
    for (key, default) in defaults.iter() {
        let name = format!("TESSERA_DESKTOP_{}", key.to_uppercase());
        if let Some(raw) = env.get(&name) {
            let value = match default {
                toml::Value::String(_) => toml::Value::String(raw.clone()),
                toml::Value::Integer(_) => toml::Value::Integer(raw.trim().parse().map_err(|_| format!("{name} must be an integer"))?),
                toml::Value::Float(_) => toml::Value::Float(raw.trim().parse().map_err(|_| format!("{name} must be a number"))?),
                // A boolean override must be read, not skipped. Accept the words
                // a shell writes, and refuse anything else rather than ignore it.
                toml::Value::Boolean(_) => toml::Value::Boolean(match raw.trim().to_ascii_lowercase().as_str() {
                    "true" | "1" | "yes" | "on" => true,
                    "false" | "0" | "no" | "off" => false,
                    _ => return Err(format!("{name} must be true or false")),
                }),
                _ => continue,
            };
            table.insert(key.clone(), value);
        }
    }
    // Integers are accepted where floats are expected (e.g. width = 1280).
    for (key, default) in defaults.iter() {
        if let (toml::Value::Float(_), Some(toml::Value::Integer(i))) = (default, table.get(key)) {
            let f = *i as f64;
            table.insert(key.clone(), toml::Value::Float(f));
        }
    }
    let settings: Settings = toml::Value::Table(table).try_into()
        .map_err(|e: toml::de::Error| format!("desktop settings are invalid: {}", e.message()))?;
    settings.validate()
}

impl serde::Serialize for Settings {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        use serde::ser::SerializeStruct;
        let mut st = s.serialize_struct("Settings", 18)?;
        st.serialize_field("python", &self.python)?;
        st.serialize_field("core_root", &self.core_root)?;
        st.serialize_field("core_host", &self.core_host)?;
        st.serialize_field("core_port", &(self.core_port as i64))?;
        st.serialize_field("startup_seconds", &self.startup_seconds)?;
        st.serialize_field("shutdown_seconds", &self.shutdown_seconds)?;
        st.serialize_field("terminate_seconds", &self.terminate_seconds)?;
        st.serialize_field("width", &self.width)?;
        st.serialize_field("height", &self.height)?;
        st.serialize_field("min_width", &self.min_width)?;
        st.serialize_field("min_height", &self.min_height)?;
        st.serialize_field("title", &self.title)?;
        st.serialize_field("attach_url", &self.attach_url)?;
        st.serialize_field("home", &self.home)?;
        st.serialize_field("attach_seconds", &self.attach_seconds)?;
        st.serialize_field("keep_core", &self.keep_core)?;
        st.serialize_field("spawn_path", &self.spawn_path)?;
        st.serialize_field("launch_agent", &self.launch_agent)?;
        st.end()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect()
    }

    fn bootstrap_fixture(label: &str) -> (PathBuf, HashMap<String, String>) {
        let root = std::env::temp_dir().join(format!("tessera-bootstrap-{label}-{}", std::process::id()));
        let home = root.join("profile");
        let appdata = root.join("appdata");
        let selected = root.join("selected-home");
        std::fs::create_dir_all(&selected).unwrap();
        let environment = env(&[("HOME", home.to_str().unwrap()),
                                ("USERPROFILE", home.to_str().unwrap()),
                                ("APPDATA", appdata.to_str().unwrap())]);
        let path = bootstrap_path(&environment).unwrap();
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, serde_json::json!({
            "home": selected.to_str().unwrap(),
            "python": root.join("venv-python").to_str().unwrap(),
            "core_root": root.join("workspace").to_str().unwrap(),
        }).to_string()).unwrap();
        (root, environment)
    }

    #[test]
    fn remembered_home_loads_its_operator_desktop_settings() {
        let (root, environment) = bootstrap_fixture("operator-settings");
        let selected = root.join("selected-home");
        std::fs::write(selected.join("desktop.toml"),
            "width = 1111\nattach_url = 'http://127.0.0.1:8740'\nkeep_core = true\nauth_token = 'fixture-token'\n").unwrap();
        let loaded = load(&environment).unwrap();
        assert_eq!(PathBuf::from(&loaded.home), selected);
        assert_eq!(PathBuf::from(&loaded.python), root.join("venv-python"));
        assert_eq!(PathBuf::from(&loaded.core_root), root.join("workspace"));
        assert_eq!(loaded.width, 1111.0);
        assert_eq!(loaded.attach_url, "http://127.0.0.1:8740");
        assert!(loaded.keep_core);
        assert_eq!(loaded.auth_token, "fixture-token");
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn plain_explicit_home_selects_its_file_and_never_uses_remembered_home() {
        let (root, mut environment) = bootstrap_fixture("explicit-home");
        let selected = root.join("explicit-home");
        std::fs::create_dir_all(&selected).unwrap();
        std::fs::write(selected.join("desktop.toml"), "width = 1001\n").unwrap();
        environment.insert("TESSERA_HOME".into(), selected.to_str().unwrap().into());
        let loaded = load(&environment).unwrap();
        // Core::connect uses TESSERA_HOME when the settings home is empty.
        assert!(loaded.home.is_empty());
        assert_eq!(loaded.width, 1001.0);
        assert_eq!(PathBuf::from(&loaded.python), root.join("venv-python"));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn explicit_home_interpreter_and_root_ignore_a_malformed_bootstrap() {
        let (root, mut environment) = bootstrap_fixture("explicit-all");
        std::fs::write(bootstrap_path(&environment).unwrap(), "not JSON").unwrap();
        environment.insert("TESSERA_HOME".into(), root.join("selected-home").to_str().unwrap().into());
        environment.insert("TESSERA_DESKTOP_PYTHON".into(), "chosen-python".into());
        environment.insert("TESSERA_DESKTOP_CORE_ROOT".into(), "chosen-root".into());
        let loaded = load(&environment).unwrap();
        assert!(loaded.home.is_empty());
        assert_eq!(loaded.python, "chosen-python");
        assert_eq!(loaded.core_root, "chosen-root");
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn explicit_settings_fields_win_without_rewriting_the_operator_file() {
        let (root, mut environment) = bootstrap_fixture("settings-win");
        let file = root.join("operator.toml");
        let text = "home = 'file-home'\npython = 'file-python'\ncore_root = 'file-root'\nwidth = 1099\n";
        std::fs::write(&file, text).unwrap();
        std::fs::write(bootstrap_path(&environment).unwrap(), "not JSON").unwrap();
        environment.insert("TESSERA_DESKTOP_CONFIG".into(), file.to_str().unwrap().into());
        let loaded = load(&environment).unwrap();
        assert_eq!(loaded.home, "file-home");
        assert_eq!(loaded.python, "file-python");
        assert_eq!(loaded.core_root, "file-root");
        assert_eq!(loaded.width, 1099.0);
        assert_eq!(std::fs::read_to_string(file).unwrap(), text);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn malformed_remembered_selection_reports_repair_instead_of_wrong_home() {
        let (root, environment) = bootstrap_fixture("invalid");
        std::fs::write(bootstrap_path(&environment).unwrap(), "not JSON").unwrap();
        assert!(load(&environment).unwrap_err().contains("run tessera --home <home> start to repair"));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn defaults_are_offline_loopback_and_valid() {
        let s = load(&env(&[("TESSERA_HOME", "/nonexistent-tessera-home")])).unwrap();
        assert_eq!(s, Settings::default());
        assert_eq!(s.core_host, "127.0.0.1");
        assert_eq!(s.core_port, 0);
        assert_eq!(s.title, "Tessera");
    }

    #[test]
    fn explicit_core_root_wins_and_is_used_verbatim() {
        let mut s = Settings::default();
        s.core_root = "/explicit/anywhere".into();
        assert_eq!(s.core_root(), PathBuf::from("/explicit/anywhere"));
    }

    #[test]
    fn file_then_env_override() {
        let dir = std::env::temp_dir().join(format!("tessera-desktop-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let file = dir.join("desktop.toml");
        std::fs::write(&file, "width = 1000\nheight = 700\ncore_port = 4100\n").unwrap();
        let s = load(&env(&[("TESSERA_DESKTOP_CONFIG", file.to_str().unwrap()),
                            ("TESSERA_DESKTOP_HEIGHT", "720.5"),
                            ("TESSERA_DESKTOP_CORE_HOST", "::1")])).unwrap();
        assert_eq!((s.width, s.height, s.core_port), (1000.0, 720.5, 4100));
        assert_eq!(s.core_host, "::1");
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn remote_hosts_bad_numbers_and_unknown_keys_are_rejected() {
        for host in ["0.0.0.0", "203.0.113.5", "localhost.evil.example", "127.evil.example"] {
            assert!(load(&env(&[("TESSERA_HOME", "/x"), ("TESSERA_DESKTOP_CORE_HOST", host)])).is_err(), "{host}");
        }
        assert!(load(&env(&[("TESSERA_HOME", "/x"), ("TESSERA_DESKTOP_CORE_PORT", "99999")])).is_err());
        assert!(load(&env(&[("TESSERA_HOME", "/x"), ("TESSERA_DESKTOP_STARTUP_SECONDS", "0")])).is_err());
        assert!(load(&env(&[("TESSERA_HOME", "/x"), ("TESSERA_DESKTOP_WIDTH", "300")])).is_err());
        assert!(load(&env(&[("TESSERA_DESKTOP_CONFIG", "/definitely/missing.toml")])).is_err());
        let dir = std::env::temp_dir().join(format!("tessera-desktop-unknown-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let file = dir.join("desktop.toml");
        std::fs::write(&file, "remote_url = \"https://example.com\"\n").unwrap();
        assert!(load(&env(&[("TESSERA_DESKTOP_CONFIG", file.to_str().unwrap())])).is_err());
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn loopback_check_matches_only_loopback_ips() {
        assert!(is_loopback("127.0.0.1") && is_loopback("127.4.5.6") && is_loopback("::1") && is_loopback("[::1]"));
        assert!(!is_loopback("0.0.0.0") && !is_loopback("localhost") && !is_loopback("198.51.100.1") && !is_loopback(""));
    }

    #[test]
    fn appdata_desktop_toml_is_windows_only() {
        let dir = std::env::temp_dir().join(format!("tessera-desktop-appdata-{}", std::process::id()));
        let nested = dir.join("tessera");
        std::fs::create_dir_all(&nested).unwrap();
        std::fs::write(nested.join("desktop.toml"), "width = 1100\nheight = 700\n").unwrap();
        let loaded = load(&env(&[("APPDATA", dir.to_str().unwrap()),
                                 ("TESSERA_HOME", "/nonexistent-tessera-home")])).unwrap();
        if cfg!(windows) {
            assert_eq!((loaded.width, loaded.height), (1100.0, 700.0));
        } else {
            assert_eq!((loaded.width, loaded.height), (1280.0, 900.0));
        }
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn xdg_and_application_support_are_not_config_paths() {
        let root = std::env::temp_dir().join(format!("tessera-desktop-xdg-{}", std::process::id()));
        let xdg = root.join("xdg");
        let home = root.join("home");
        std::fs::create_dir_all(xdg.join("tessera")).unwrap();
        std::fs::create_dir_all(home.join("Library").join("Application Support").join("tessera")).unwrap();
        std::fs::write(xdg.join("tessera").join("desktop.toml"), "width = 1100\nheight = 700\n").unwrap();
        std::fs::write(
            home.join("Library").join("Application Support").join("tessera").join("desktop.toml"),
            "width = 1111\nheight = 701\n",
        ).unwrap();
        let loaded = load(&env(&[
            ("XDG_CONFIG_HOME", xdg.to_str().unwrap()),
            ("HOME", home.to_str().unwrap()),
            ("TESSERA_HOME", "/nonexistent-tessera-home"),
        ])).unwrap();
        assert_eq!((loaded.width, loaded.height), (1280.0, 900.0));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn boolean_env_overrides_are_honoured_and_bad_values_refused() {
        // keep_core is the override a person actually sets. The attach_url is
        // required alongside it, so both are set here.
        let base = [("TESSERA_HOME", "/nonexistent-tessera-home"),
                    ("TESSERA_DESKTOP_ATTACH_URL", "http://127.0.0.1:8740")];
        for word in ["true", "TRUE", "1", "yes", "on"] {
            let mut pairs = base.to_vec();
            pairs.push(("TESSERA_DESKTOP_KEEP_CORE", word));
            assert!(load(&env(&pairs)).unwrap().keep_core, "{word} must enable keep_core");
        }
        for word in ["false", "0", "no", "off"] {
            let mut pairs = base.to_vec();
            pairs.push(("TESSERA_DESKTOP_KEEP_CORE", word));
            assert!(!load(&env(&pairs)).unwrap().keep_core, "{word} must leave keep_core off");
        }
        // Anything else is refused rather than silently ignored.
        let mut pairs = base.to_vec();
        pairs.push(("TESSERA_DESKTOP_KEEP_CORE", "maybe"));
        let err = load(&env(&pairs)).unwrap_err();
        assert!(err.contains("TESSERA_DESKTOP_KEEP_CORE must be true or false"), "{err}");
    }
}
