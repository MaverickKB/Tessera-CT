"""The Tauri shell keeps Tessera branding, grants the page no IPC and passes its Rust tests."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

TAURI = Path(__file__).resolve().parents[1] / "desktop"


def test_tauri_manifest_keeps_tessera_identity_and_bundles_for_every_desktop():
    conf = json.loads((TAURI / "tauri.conf.json").read_text())
    assert conf["productName"] == "Tessera"
    assert conf["bundle"]["targets"] == "all"
    assert conf["app"]["withGlobalTauri"] is False
    assert conf["app"]["security"]["capabilities"] == []
    assert conf["app"]["windows"] == [], "the window is built at runtime from the core's ready origin"
    for icon in conf["bundle"]["icon"]:
        assert (TAURI / icon).is_file()
    assert not (TAURI / "capabilities").exists()


def test_shell_source_has_no_hardwired_remote_targets_or_credentials():
    # Shipped code only; unit-test fixtures below #[cfg(test)] name hostile URLs on purpose.
    source = "\n".join(p.read_text().split("#[cfg(test)]")[0] for p in (TAURI / "src").glob("*.rs"))
    assert re.findall(r"https?://[^\s\"]*", source) == ["http://127.0.0.1/"], "only the pre-ready placeholder"
    assert not re.search(r"authorization|bearer|api[_-]?key\s*=", source, re.I)
    assert "WebviewUrl::External(origin" in source
    assert "on_navigation" in source


def _stripped_env():
    return {k: v for k, v in os.environ.items() if not k.endswith(("_API_KEY", "_TOKEN"))}


def _edition2024_blocker(cargo):
    """Skip reason when this Cargo cannot parse edition 2024, else None.

    Current Tauri crates publish manifests that need it. Cargo 1.85 stabilized
    the edition; older stable releases fail while parsing the lockfile.
    """
    with tempfile.TemporaryDirectory(prefix="tessera-cargo-edition-") as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        (root / "src" / "lib.rs").write_text("\n")
        (root / "Cargo.toml").write_text(
            '[package]\nname = "edition_probe"\nversion = "0.0.0"\nedition = "2024"\n'
        )
        env = _stripped_env()
        env["CARGO_NET_OFFLINE"] = "true"
        result = subprocess.run(
            [cargo, "metadata", "--no-deps", "--offline", "--format-version", "1"],
            cwd=root, env=env, capture_output=True, text=True, timeout=60,
        )
    if result.returncode == 0:
        return None
    detail = (result.stderr or result.stdout or "").strip()
    if "edition2024" not in detail and "edition 2024" not in detail.lower():
        return None
    version = subprocess.run([cargo, "--version"], capture_output=True, text=True, timeout=30)
    shown = (version.stdout or version.stderr or cargo).strip()
    return f"Rust toolchain cannot parse edition 2024 ({shown})"


def _webkitgtk_blocker():
    """Skip reason when Linux cannot link the Tauri webview, else None."""
    if not sys.platform.startswith("linux"):
        return None
    pkg = shutil.which("pkg-config")
    if not pkg:
        return "WebKitGTK 4.1 development libraries were not found (pkg-config is not installed)"
    if subprocess.run([pkg, "--exists", "webkit2gtk-4.1"], capture_output=True).returncode == 0:
        return None
    return "WebKitGTK 4.1 development libraries are not installed (pkg-config did not find webkit2gtk-4.1)"


def test_rust_shell_unit_and_real_core_lifecycle_tests_pass():
    cargo = shutil.which("cargo")
    if not cargo:
        pytest.skip("Rust toolchain unavailable; the Tauri shell cannot be compiled here")
    reasons = [reason for reason in (_edition2024_blocker(cargo), _webkitgtk_blocker()) if reason]
    if reasons:
        pytest.skip("; ".join(reasons))
    result = subprocess.run(
        [cargo, "test", "--quiet"], cwd=TAURI, env=_stripped_env(), capture_output=True, text=True, timeout=1800,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    # Assert a real pass rather than a hardcoded count: the shell gains tests as it
    # gains features, and a count that is not re-measured turns a green suite red.
    assert re.search(r"test result: ok\. \d+ passed; 0 failed", result.stdout), result.stdout[-4000:]
