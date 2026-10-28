#!/usr/bin/env python3
"""TypeSafe (System One / Jev) helper. Stdlib only.

Reads TYPESAFE_API_KEY, TYPESAFE_URL and TYPESAFE_MODEL from env; never prints or logs the key.
The endpoint and model have no built-in default. A call without both is a configuration error.
(docs: https://docs.typesafe.ai/api.md)

Library use:
    import sys; sys.path.insert(0, "docs/reference"); import typesafe_client_example as ts
    ts.noul(state, "Is this about X?", true="...", false="...")   -> float 0..1
    ts.choice(state, "Which desk?", {"a": "desc", "b": None})     -> dict(choice, probabilities, confidence)
    ts.score(state, "How relevant?", ["low", "mid", "high"])      -> dict(score, legend, probabilities, confidence)
    ts.evaluate(state, {"qid": {...question...}, ...})            -> full response (batch; questions run in parallel)

CLI:
    ts.py --selftest
    ts.py noul   --state TEXT|@file.json --q "question" [--true ..] [--false ..]
    ts.py choice --state ... --q "question" --opt key=desc --opt key2
    ts.py score  --state ... --q "question" --level L0 --level L1 [...]
Prints JSON answer to stdout. Exit 2 on missing key, 1 on API error.
"""
import json, os, sys, time, urllib.request, urllib.error

def _target(model):
    url = os.environ.get("TYPESAFE_URL", "").strip()
    chosen = os.environ.get("TYPESAFE_MODEL", "").strip() if model is None else str(model).strip()
    missing = [name for name, value in (("TYPESAFE_URL", url), ("TYPESAFE_MODEL", chosen)) if not value]
    if missing:
        raise TypeSafeError(0, "configuration error: " + " and ".join(missing) + " must be set")
    return url, chosen


class TypeSafeError(RuntimeError):
    def __init__(self, status, body):
        super().__init__(f"TypeSafe HTTP {status}: {body[:500]}")
        self.status, self.body = status, body


def _key():
    k = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not k:
        raise TypeSafeError(0, "TYPESAFE_API_KEY not set in environment")
    return k


def evaluate(state, questions, model=None, timeout=30, retries=3):
    """Send one request with a map of questions. Returns parsed JSON response."""
    url, chosen = _target(model)
    body = json.dumps({"state": state, "model": chosen, "questions": questions}).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": "Bearer " + _key(),
            "Content-Type": "application/json",
            "User-Agent": "kb-grokbot-ts/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            txt = e.read().decode(errors="replace")
            if e.code in (429, 529) and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise TypeSafeError(e.code, txt) from None
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise TypeSafeError(0, f"network error: {e.reason}") from None


def _one(state, q, model):
    return evaluate(state, {"q": q}, model=model)["answers"]["q"]


def noul(state, instructions, true=None, false=None, model=None):
    q = {"type": "noul", "instructions": instructions}
    if true or false:
        q["criteria"] = {k: v for k, v in (("true", true), ("false", false)) if v}
    return _one(state, q, model)["noul"]


def choice(state, instructions, options, model=None):
    if isinstance(options, (list, tuple)):
        options = {o: None for o in options}
    return _one(state, {"type": "choice", "instructions": instructions, "criteria": options}, model)


def score(state, instructions, levels, model=None):
    return _one(state, {"type": "score", "instructions": instructions, "criteria": list(levels)}, model)


def _load_state(s):
    if s.startswith("@"):
        txt = open(s[1:]).read()
        try:
            return json.loads(txt)
        except ValueError:
            return txt
    return s


def selftest():
    """One request, three tiny questions (one per primitive)."""
    state = "Help! Sign-in has been failing for 3 days."
    r = evaluate(state, {
        "urgent": {"type": "noul", "instructions": "Does this convey urgency?"},
        "dept": {"type": "choice", "instructions": "Which team should handle this?",
                 "criteria": {"accounts": "Account access and records", "technical": "Bugs, outages",
                              "sales": "Plans and upgrades"}},
        "frustration": {"type": "score", "instructions": "How frustrated is the customer?",
                        "criteria": ["Calm", "Frustrated", "Very angry"]},
    })
    a = r["answers"]
    ok = (a["urgent"]["type"] == "noul" and 0 <= a["urgent"]["noul"] <= 1
          and a["dept"]["choice"] in ("accounts", "technical", "sales")
          and 0 <= a["frustration"]["score"] <= 2)
    print(json.dumps({"ok": ok, "model": r.get("model"), "usage": r.get("usage"),
                      "urgent": a["urgent"]["noul"], "dept": a["dept"]["choice"],
                      "dept_conf": a["dept"].get("confidence"),
                      "frustration": a["frustration"]["score"]}, indent=1))
    return 0 if ok else 1


def main(argv):
    import argparse
    p = argparse.ArgumentParser(description="TypeSafe helper")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("kind", nargs="?", choices=["noul", "choice", "score"])
    p.add_argument("--state"); p.add_argument("--q")
    p.add_argument("--true"); p.add_argument("--false")
    p.add_argument("--opt", action="append", default=[], help="key=description (or key)")
    p.add_argument("--level", action="append", default=[])
    p.add_argument("--model", default=None)
    a = p.parse_args(argv)
    try:
        if a.selftest:
            return selftest()
        if not (a.kind and a.state and a.q):
            p.error("need kind, --state and --q (or --selftest)")
        st = _load_state(a.state)
        if a.kind == "noul":
            out = {"noul": noul(st, a.q, a.true, a.false, a.model)}
        elif a.kind == "choice":
            opts = dict((o.split("=", 1) + [None])[:2] for o in a.opt)
            out = choice(st, a.q, opts, a.model)
        else:
            out = score(st, a.q, a.level, a.model)
        print(json.dumps(out))
        return 0
    except TypeSafeError as e:
        print(json.dumps({"error": str(e), "status": e.status}), file=sys.stderr)
        return 2 if e.status == 0 and "not set" in e.body else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
