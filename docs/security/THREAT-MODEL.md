# Tessera threat model

Written against the tree at the commit that added this file. Every claim below
names the file and line that makes it true. Where the code does not
establish something, this document says **unverified** rather than assuming.

Tessera is a **single-owner local harness**. It is not a multi-tenant service,
and it has no per-user isolation. That is the design, not a gap to be closed
later, and it should be read before exposing a core to anything but yourself.

## 1. What the trust boundary is

The boundary is the operating-system account that runs the core. Anything that
can send an HTTP request to the core's listening port is, by default, treated as
the operator and can do what the operator can do: run turns, execute tools,
write files, read the transcript history, and mutate configuration.

There is no notion of a second user with fewer rights inside one core. Nothing
in this tree distinguishes callers: every request that passes the check in
section 3 is the operator.

Note for readers who also run a separate model router: that service has its own
caller-identity concept and its own policy file. It is a different program with a
different trust boundary, and nothing here should be read as a statement about it.

## 2. Who can reach the core

`ServerConfig.host` defaults to loopback and `ServerConfig.port` defaults to 0
(`tessera/config.py:214-215`), so a stock core listens on `127.0.0.1` on an
ephemeral port. `_binding()` in `tessera/server.py` resolves the family and
address from that setting.

The core still binds loopback only when a proxy is in front of it: the
`allowed_hosts` comment at `tessera/config.py:218-220` states that naming a
host such as `tailscale serve` presents does not widen the bind.

Therefore the reachable set is:

- any process running as the same OS user on the same machine (always);
- anything that can reach the port through a proxy you deliberately configured
  (only if you configured one);
- nothing else, unless you changed `host`.

## 3. What the shared key defends

`ServerConfig.auth_token` (`tessera/config.py:221-223`) is a **shared** key, and
`ServerConfig.auth_mode` (`tessera/config.py:224-231`) decides when it is
required when the key is non-empty (an empty key disables authentication):

- `auth_mode = 'loopback'` (**the default**): the key is required **only** when
  a request arrives through an `allowed_hosts` proxy. Direct loopback requests
  are unauthenticated. This is deliberate: it means a local web page, the desktop
  window, and a `curl` script cannot be stranded by a wrong or missing key.
- `auth_mode = 'always'`: the key is required on every `/api/` request. Choose
  this only on a genuinely shared machine, because every local client must then
  present the key.

Two details worth knowing before you rely on it:

- Static UI is served without the key by design (`tessera/server.py:341-344`):
  a client should be able to load the page and *show* an auth error rather than
  present a blank window. Assets are code, not data, and contain no transcript.
- Comparison uses `hmac.compare_digest` (`tessera/server.py:348`), so the check
  is not timing-sensitive.

## 4. What the shared key does NOT defend

This is the important section.

- **It is not per-user identity.** One key, one privilege level. Anyone holding
  it is the operator. There is no audit trail that distinguishes two holders.
- **It gives no isolation between clients that share the key.** Two people with
  the key see the same sessions, the same board, and the same files.
- **Under the default `loopback` mode, it defends nothing against a local
  process.** Any program running as your OS user can call the unauthenticated
  loopback API. That is the same authority that program already has over your
  files, so it is not an escalation — but it means the key is *not* a boundary
  between local programs.
- **It does not encrypt.** Traffic is plain HTTP. On loopback that is the OS's
  socket. Through a proxy it is whatever TLS that proxy terminates. Tessera does
  not implement TLS itself.
- **It is not a substitute for filesystem permissions.** Anyone who can read the
  home directory can read the transcript and configuration directly, key or no
  key. See section 5.

## 5. What the bundled AetherMind store exposes

The AetherMind store is a local append-only record written under the Tessera
home directory. It lands at
`<home>/.aethermind/`, containing `layers.aem`, `texture.aem` and a skills
directory.

Consequences:

- The store is **plain files under the home directory**. It is not encrypted at
  rest, and it is not access-controlled beyond ordinary filesystem permissions.
  If an attacker can read that directory, the memory is theirs regardless of the
  HTTP key.
- Layers record decisions, notes, and session continuity — which in practice
  means they can contain whatever was written into them, including paths,
  hostnames, and content of conversations.
- A store seeded from a wheel carries the packaging seed (31 layers, 6 textures,
  and 5 skills, as listed in `seeds/README.md`). A store grown by use carries
  whatever the owner has done.

## 6. What tool execution can do

Approval policy is one of `ask`, `never`, `deny_risky`, `yolo`
(`tessera/tools.py:242`). The web UI labels `yolo` Unleashed (config value `yolo`).
`AUTO_APPROVE` is `('never', 'yolo')`
(`tessera/tools.py:243`), and the gate at `tessera/tools.py:381` skips the
prompt when the tool is not risky, the policy is in that set, or the tool is otherwise allowed.

Under `yolo` or `never`, a tool call runs without a human in the loop. What that
means depends on the tool, but the shell and file-writing tools do what the OS
user can do. A model that has been talked into a bad command, or a prompt
injection arriving through tool output or fetched content, is the realistic
threat — and the approval prompt is the only control that catches it. Running
unattended at `yolo` is a deliberate acceptance of that risk.

`ask` and `deny_risky` are the policies that put a human between the model and a
risky action. They are worth keeping for any core that touches data you would
not hand to a stranger.

## 7. Known open gaps

Stated plainly, not buried:

- **No per-user isolation, by design.** Section 1. Do not expose one core to
  several people who should not see each other's work.
- **Default `loopback` mode leaves local `/api/` unauthenticated.** Section 4.
  This is the documented posture, not an oversight, but it is a real limit.
- **The AetherMind store is unencrypted plain files.** Section 5.
- **No TLS in the core.** Section 4.
- **This document covers the Tessera core only.** The model router that fronts
  several backends is a separate program with a separate threat model, which
  does not exist yet. Do not read this file as covering it.
- **Unverified:** whether every adapter treats a malformed tool call as a refusal
  rather than an attempt. I did not audit the adapters for this document.

## 8. Reading this document

If you are about to expose Tessera beyond your own account, the minimum set is:
set `auth_mode = 'always'`, set a strong `auth_token`, keep the bind on loopback
behind a proxy you control, keep approval at `ask` or `deny_risky`, and treat the
home directory as the real secret — because it is.
