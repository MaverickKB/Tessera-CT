"""Passive, finite provider metadata. No credential or transport operations."""


ERROR_CATEGORIES = frozenset(("authentication", "permission", "rate_limit", "quota", "timeout",
                              "connection", "provider", "configuration", "aethermind"))


def initial_status(needs_credential):
    return {"health": "not_checked", "credential": "not_reported" if needs_credential else "not_required"}


def build_provider_status(config, observations=()):
    by_backend = {}
    by_account = {}
    for stamp, name, account, record in observations:
        if stamp <= 0:
            continue
        for key, table in ((name, by_backend), (account, by_account)):
            if key not in table or table[key][0] < stamp:
                table[key] = (stamp, record)

    def record(configured, needs_credential, observed):
        result = {"configured": bool(configured), **initial_status(needs_credential)}
        if observed:
            snapshot = observed[1]
            if snapshot.get("health") in ("healthy", "not_checked", "error"):
                result["health"] = snapshot["health"]
            if snapshot.get("credential") in ("available", "missing", "not_required", "not_reported"):
                result["credential"] = snapshot["credential"]
            if result["health"] == "error" and snapshot.get("error_category") in ERROR_CATEGORIES:
                result["error_category"] = snapshot["error_category"]
        return result

    accounts = []
    for name, settings in config.accounts.items():
        needs = bool(settings.credential_ref or settings.provider_kind == "cli_worker")
        accounts.append(record(True, needs, by_account.get(name)))
    backends = []
    for name, settings in config.backends.items():
        account = config.accounts.get(settings.account)
        kind = account.provider_kind if account else settings.provider_kind
        reference = account.credential_ref if account else settings.credential_ref
        if kind == "cli_worker":
            cli = settings.extra.get("cli", {})
            configured = bool(cli.get("command") and cli.get("vendor") and cli.get("cwd"))
        else:
            configured = bool(settings.base_url and settings.model)
        needs = bool(reference or settings.api_key_env or kind == "cli_worker")
        backends.append({"id": name, **record(configured, needs, by_backend.get(name))})
    return {"accounts": accounts, "backends": backends}
