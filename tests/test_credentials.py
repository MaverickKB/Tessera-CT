"""Only configured secret sources are queried, in deterministic order."""

import subprocess

import pytest

from tessera.credentials import CredentialError, CredentialResolver


def no_process(*args, **kwargs):
    raise AssertionError("No OS credential query was authorized")


def test_keychain_exists_does_not_read_the_secret():
    from tessera.credentials import KeychainStore, MemoryKeychain
    secret = "synthetic-os-secret"
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, secret, secret)

    store = KeychainStore(platform="darwin", which=lambda name: "/usr/bin/security", run=run)
    assert store.exists("example-service", "example-account") == "found"
    assert seen["argv"] == [
        "/usr/bin/security", "find-generic-password", "-s", "example-service", "-a", "example-account",
    ]
    assert "-w" not in seen["argv"] and "-g" not in seen["argv"]
    assert secret not in seen["argv"]
    assert seen["kwargs"]["stdin"] is subprocess.DEVNULL
    assert seen["kwargs"]["stdout"] is subprocess.DEVNULL
    assert seen["kwargs"]["stderr"] is subprocess.DEVNULL
    assert "shell" not in seen["kwargs"]

    def missing(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 44, secret, secret)

    absent = KeychainStore(platform="darwin", which=lambda name: "/usr/bin/security", run=missing)
    assert absent.exists("example-service", "example-account") == "missing"

    def refuse(*_args, **_kwargs):
        raise AssertionError("query")

    for platform in ("linux", "win32"):
        other = KeychainStore(platform=platform, which=lambda name: "/usr/bin/secret-tool", run=refuse)
        assert other.exists("example-service", "example-account") == "unavailable"
    no_helper = KeychainStore(platform="darwin", which=lambda name: None, run=refuse)
    assert no_helper.exists("example-service", "example-account") == "unavailable"

    memory = MemoryKeychain()
    assert memory.exists("example-service", "example-account") == "missing"
    memory.put("example-service", "example-account", secret)
    assert memory.exists("example-service", "example-account") == "found"
    assert secret not in memory.exists("example-service", "example-account")


def test_environment_wins_without_querying_keychain():
    resolver = CredentialResolver({"work": {
        "env": "EXAMPLE_KEY", "keychain_service": "example-service",
        "keychain_account": "example-account", "config_ref": "fallback",
    }}, environ={"EXAMPLE_KEY": "synthetic-env-secret"}, run=no_process)
    credential = resolver.resolve("work")
    assert credential.value == "synthetic-env-secret"
    assert credential.source == "env"
    assert credential.value not in repr(credential)


@pytest.mark.parametrize("platform,command,arguments", [
    ("darwin", "security", ["find-generic-password", "-s", "example-service", "-a", "example-account", "-w"]),
    ("linux", "secret-tool", ["lookup", "service", "example-service", "account", "example-account"]),
])
def test_keychain_query_uses_only_configured_selectors(platform, command, arguments):
    seen = []
    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "synthetic-os-secret\n", "")
    resolver = CredentialResolver({"work": {
        "env": "ABSENT_KEY", "keychain_service": "example-service",
        "keychain_account": "example-account", "keychain_timeout": .25,
    }}, environ={}, platform=platform, which=lambda name: name, run=run)
    result = resolver.resolve("work")
    assert result.value == "synthetic-os-secret" and result.source == "keychain"
    assert seen[0][0] == [command] + arguments
    assert seen[0][1]["timeout"] == .25 and "shell" not in seen[0][1]
    assert result.value not in repr(seen)


def test_missing_keychain_falls_through_to_named_config_reference():
    resolver = CredentialResolver({
        "work": {"env": "ABSENT_KEY", "keychain_service": "example-service",
                 "keychain_account": "example-account", "config_ref": "backup"},
        "backup": {"env": "BACKUP_KEY"},
    }, environ={"BACKUP_KEY": "synthetic-backup-secret"}, which=lambda name: None, run=no_process)
    assert resolver.resolve("work").value == "synthetic-backup-secret"


def test_timeout_and_helper_output_never_appear_in_error(capsys):
    secret = "synthetic-helper-secret"
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1, output=secret, stderr=secret)
    resolver = CredentialResolver({"work": {
        "keychain_service": "example-service", "keychain_account": "example-account",
    }}, environ={}, platform="darwin", which=lambda name: name, run=run)
    with pytest.raises(CredentialError, match="unavailable") as caught:
        resolver.resolve("work")
    assert secret not in str(caught.value)
    assert caught.value.__suppress_context__
    assert capsys.readouterr().out == ""


def test_unconfigured_resolution_is_inert_but_explicit_missing_source_errors():
    resolver = CredentialResolver({}, environ={}, run=no_process)
    assert resolver.resolve().value == ""
    with pytest.raises(CredentialError, match="unavailable"):
        resolver.resolve(env="ABSENT_KEY")
    with pytest.raises(CredentialError, match="reference"):
        resolver.resolve("missing")


def test_reference_cycle_is_actionable_without_revealing_values():
    resolver = CredentialResolver({"one": {"config_ref": "two"}, "two": {"config_ref": "one"}},
                                  environ={}, run=no_process)
    with pytest.raises(CredentialError, match="cycle"):
        resolver.resolve("one")


def test_explicit_legacy_env_precedes_referenced_sources():
    resolver = CredentialResolver({"work": {"env": "SECOND_KEY"}},
                                  environ={"FIRST_KEY": "synthetic-first", "SECOND_KEY": "synthetic-second"},
                                  run=no_process)
    assert resolver.resolve("work", env="FIRST_KEY").value == "synthetic-first"


def test_keychain_failure_uses_config_fallback_and_env_rotation_is_not_cached():
    environment = {"BACKUP_KEY": "synthetic-initial"}
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 44, "synthetic-untrusted-output", "synthetic-private-error")
    resolver = CredentialResolver({
        "work": {"keychain_service": "example-service", "keychain_account": "example-account", "config_ref": "backup"},
        "backup": {"env": "BACKUP_KEY"},
    }, environ=environment, platform="darwin", which=lambda name: name, run=run)
    assert resolver.resolve("work").value == "synthetic-initial"
    environment["BACKUP_KEY"] = "synthetic-rotated"
    assert resolver.resolve("work").value == "synthetic-rotated"


@pytest.mark.parametrize("sources", [[], {"work": {"keychain_service": "example-service"}},
                                      {"work": {"keychain_timeout": 0}}])
def test_invalid_source_descriptors_fail_without_lookup(sources):
    with pytest.raises(ValueError):
        CredentialResolver(sources, run=no_process)
