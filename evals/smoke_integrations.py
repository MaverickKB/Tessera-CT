"""At most one TypeSafe call and a read-only MCP tool listing, when configured."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from tessera.aethermind import AetherMindClient
from tessera.jev import JevRouter


def run_smoke(output=None):
    results = {"generated_at": datetime.now(timezone.utc).isoformat()}
    router = JevRouter()
    configured = router.enabled and router.url and router.model and router.api_key_env
    if configured and os.environ.get(router.api_key_env, "").strip():
        score = router.judge("Reply with exactly hello", "hello")
        source = router.decisions[-1]["source"]
        results["typesafe"] = {"status": "passed" if source == "jev" else "failed",
                               "calls": 1, "source": source, "score": score}
    else:
        results["typesafe"] = {"status": "skipped", "reason": "Judgment integration not fully configured", "calls": 0}
    url = os.environ.get("TESSERA_AETHERMIND_URL", "").strip()
    if url:
        try:
            token_env = os.environ.get("TESSERA_AETHERMIND_TOKEN_ENV", "AETHERMIND_TOKEN")
            client = AetherMindClient(url, token_env=token_env, timeout=10)
            tools = client.mcp.list_tools()
            results["aethermind"] = {"status": "passed", "tool_count": len(tools), "read_only": True}
        except Exception as error:
            results["aethermind"] = {"status": "failed", "error": type(error).__name__, "read_only": True}
    else:
        results["aethermind"] = {"status": "skipped", "reason": "TESSERA_AETHERMIND_URL not set", "read_only": True}
    output = Path(output) if output else Path(__file__).with_name("smoke_results.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return results


if __name__ == "__main__":
    result = run_smoke()
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(1 if any(item.get("status") == "failed" for item in (result["typesafe"], result["aethermind"])) else 0)
