"""Resolve explicitly named secret sources without storing or logging values."""

from dataclasses import dataclass, field
import math
import os
import shutil
import subprocess
import sys


class CredentialError(RuntimeError):
    pass


@dataclass
class CredentialReference:
    env: str = ""
    keychain_service: str = ""
    keychain_account: str = ""
    config_ref: str = ""
    keychain_command: str = ""
    keychain_timeout: float = 3.0
    # "xai" reads this harness's xAI OAuth grant. Empty means this source is not OAuth.
    oauth: str = ""

    def validate(self):
        for name in ("env", "keychain_service", "keychain_account", "config_ref", "keychain_command", "oauth"):
            if not isinstance(getattr(self, name), str):
                raise ValueError("Credential source selectors must be text")
        if self.oauth not in ("", "xai", "openai-codex"):
            raise ValueError("oauth must be empty, xai, or openai-codex")
        if bool(self.keychain_service) != bool(self.keychain_account):
            raise ValueError("Keychain lookup requires both service and account")
        timeout = self.keychain_timeout
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Keychain timeout must be positive and finite")
        return self


@dataclass
class ResolvedCredential:
    value: str = field(repr=False)
    source: str = "none"


class CredentialResolver:
    def __init__(self, references=None, *, environ=None, platform=None,
                 which=shutil.which, run=subprocess.run, oauth_home=""):
        self.references = {}
        if references is not None and not isinstance(references, dict):
            raise ValueError("Credentials must be named reference tables")
        for name, settings in (references or {}).items():
            if not isinstance(name, str) or not name:
                raise ValueError("Credential reference names must be nonempty text")
            if isinstance(settings, dict):
                try:
                    settings = CredentialReference(**settings)
                except TypeError:
                    raise ValueError("Unknown credential source options; use references, not secrets") from None
            if not isinstance(settings, CredentialReference):
                raise ValueError("Credential sources must be reference tables")
            self.references[name] = settings.validate()
        self.environ = os.environ if environ is None else environ
        self.platform = platform or sys.platform
        self.which = which
        self.run = run
        self.oauth_home = str(oauth_home or "")

    def resolve(self, reference="", *, env=""):
        if env:
            value = self.environ.get(env, "").strip()
            if value:
                return ResolvedCredential(value, "env")
        if not reference:
            if env:
                raise CredentialError("Credential unavailable: configured environment variable is unset")
            return ResolvedCredential("")
        seen = set()
        while reference:
            if reference in seen:
                raise CredentialError("Credential config reference cycle")
            seen.add(reference)
            settings = self.references.get(reference)
            if settings is None:
                raise CredentialError("Credential config reference is not configured")
            if settings.oauth in ("xai", "openai-codex"):
                from .account_oauth import OAuthError, access_token
                try:
                    token = access_token(self.oauth_home, settings.oauth)
                except OAuthError as error:
                    raise CredentialError(str(error)) from None
                return ResolvedCredential(token, "oauth")
            if settings.env:
                value = self.environ.get(settings.env, "").strip()
                if value:
                    return ResolvedCredential(value, "env")
            value, state = self._keychain(settings)
            if value:
                return ResolvedCredential(value, "keychain")
            reference = settings.config_ref
        raise CredentialError("Credential unavailable: environment unset; keychain " + state + "; no config fallback") from None

    def _keychain(self, settings):
        if not settings.keychain_service:
            return "", "not configured"
        if self.platform == "darwin":
            command = settings.keychain_command or "security"
            arguments = ["find-generic-password", "-s", settings.keychain_service,
                         "-a", settings.keychain_account, "-w"]
        elif self.platform.startswith("linux"):
            command = settings.keychain_command or "secret-tool"
            arguments = ["lookup", "service", settings.keychain_service,
                         "account", settings.keychain_account]
        else:
            return "", "unsupported on this OS"
        executable = self.which(command)
        if not executable:
            return "", "helper unavailable"
        try:
            result = self.run([executable] + arguments, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=settings.keychain_timeout, check=False)
        except (OSError, subprocess.SubprocessError):
            return "", "lookup unavailable"
        if result.returncode != 0 or not result.stdout.strip():
            return "", "item unavailable"
        return result.stdout.strip(), "available"


class KeychainStore:
    """Write or delete one generic-password item. The secret never appears in errors."""

    def __init__(self, *, platform=None, which=shutil.which, run=subprocess.run):
        self.platform = platform or sys.platform
        self.which = which
        self.run = run

    def put(self, service, account, secret):
        self._names(service, account)
        if not isinstance(secret, str) or not secret or "\x00" in secret or len(secret) > 4096:
            raise ValueError("Credential value is empty or too long")
        if self.platform == "darwin":
            self._invoke("security", ["add-generic-password", "-U", "-s", service, "-a", account, "-w", secret], None)
        elif str(self.platform).startswith("linux"):
            self._invoke("secret-tool", ["store", "--label", account, "service", service, "account", account], secret)
        else:
            raise ValueError("Keychain is not available on this OS")

    def delete(self, service, account):
        self._names(service, account)
        if self.platform == "darwin":
            self._invoke("security", ["delete-generic-password", "-s", service, "-a", account], None)
        elif str(self.platform).startswith("linux"):
            self._invoke("secret-tool", ["clear", "service", service, "account", account], None)
        else:
            raise ValueError("Keychain is not available on this OS")

    def get(self, service, account):
        """Read one item without mutating it. Returns (value, state) where state is
        'available', 'missing', or 'unavailable'. A missing item is not an error; an
        unavailable helper is reported separately so a caller never mistakes it for
        an empty slot and deletes a working credential on that guess."""
        self._names(service, account)
        if self.platform == "darwin":
            command, argv = "security", ["find-generic-password", "-s", service, "-a", account, "-w"]
        elif str(self.platform).startswith("linux"):
            command, argv = "secret-tool", ["lookup", "service", service, "account", account]
        else:
            return "", "unavailable"
        executable = self.which(command)
        if not executable:
            return "", "unavailable"
        try:
            result = self.run([executable, *argv], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            return "", "unavailable"
        if result.returncode != 0:
            return "", "missing"
        value = result.stdout.strip()
        return value, ("available" if value else "missing")

    def exists(self, service, account):
        """Whether the item is present. Does not read the secret and does not prompt.

        On macOS this runs find-generic-password with -s and -a only. Stdout and
        stderr are discarded, so the secret is never collected. Exit 0 is
        'found' and any other exit is 'missing'. Any other platform, a missing
        helper, or a failed launch is 'unavailable'.
        """
        self._names(service, account)
        if self.platform != "darwin":
            return "unavailable"
        executable = self.which("security")
        if not executable:
            return "unavailable"
        argv = ["find-generic-password", "-s", service, "-a", account]
        try:
            result = self.run(
                [executable, *argv],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return "unavailable"
        if getattr(result, "returncode", 1) == 0:
            return "found"
        return "missing"

    @staticmethod
    def _names(service, account):
        if not isinstance(service, str) or not service or not isinstance(account, str) or not account:
            raise ValueError("Keychain needs both a service and an account")
        if "\x00" in service or "\x00" in account or len(service) > 256 or len(account) > 256:
            raise ValueError("Keychain service or account is invalid")

    def _invoke(self, command, argv, stdin):
        executable = self.which(command)
        if not executable:
            raise ValueError("Keychain helper is unavailable")
        try:
            result = self.run([executable, *argv], input=stdin, capture_output=True, text=True,
                              timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            raise ValueError("Keychain update failed") from None
        if result.returncode != 0:
            raise ValueError("Keychain update failed")


class MemoryKeychain:
    """Process-local stand-in for tests. It never writes config or logs."""

    def __init__(self):
        self.items = {}

    def put(self, service, account, secret):
        KeychainStore._names(service, account)
        if not isinstance(secret, str) or not secret or "\x00" in secret or len(secret) > 4096:
            raise ValueError("Credential value is empty or too long")
        self.items[(service, account)] = secret

    def delete(self, service, account):
        KeychainStore._names(service, account)
        self.items.pop((service, account), None)

    def get(self, service, account):
        KeychainStore._names(service, account)
        value = self.items.get((service, account), "")
        return value, ("available" if value else "missing")

    def exists(self, service, account):
        KeychainStore._names(service, account)
        return "found" if (service, account) in self.items else "missing"


def redact_secret(value, secret):
    """Remove a resolved secret from provider data before it reaches session logs."""
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    if isinstance(value, list):
        result = []
        for item in value:
            result.append(redact_secret(item, secret))
        return result
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            result[redact_secret(key, secret)] = redact_secret(item, secret)
        return result
    return value
