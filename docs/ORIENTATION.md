# Getting Started orientation session

The first-use test creates a normal persistent chat with `purpose: "orientation"`.
Its first reply greets the user and offers optional Jev/System 1 judging, autonomy
and floor pressure, infrastructure monitoring, accounts/models, voice, or real
work. Setup remains optional. The regular AetherMind seed, runtime, permissions,
model routing, and memory behavior remain in place.

The purpose belongs to session metadata, not its title. Renaming or reopening it
keeps orientation; an ordinary chat called Getting Started is not silently
converted. Existing chats are left alone. Use Send test message from Getting
started in Settings to create an orientation session after updating.

## Current documentation and the scoped skill

`orientation/skills/getting-started/SKILL.md` is attached only to orientation
sessions. It is not copied into the user's global skill shelf or the 31-layer
seed. The normal skill enable/disable controls still apply. Its current procedure
is loaded into that session's turn context; the global system prompt is unchanged.

`orientation_docs` lists or pages the current README and markdown files under
`docs/`, with path traversal/symlink escape checks. A checkout reads its current
files. A wheel carries the same documentation and orientation skill, so an
updated install reads its updated documentation without maintaining a second
orientation knowledge summary. The model is instructed to consult the relevant
current docs and settings before changing anything, and verify the result.

Small session-scoped adapters call the existing Settings service and OAuth login
desk. They do not create another configuration engine or OAuth implementation.
Settings edits and sign-in starts retain normal tool approvals. OAuth status
returns only public handoff fields, and can wait up to 30 seconds for completion.
The existing browser flow opens the sign-in page and stores the grant; access
and refresh tokens are not tool results. Pending is never treated as connected.

## Protected credential entry

The `request_credential(name, purpose)` tool creates a bounded, expiring request
containing metadata only. The value is never a tool argument or result. The
orientation skill explicitly forbids asking for keys/passwords in ordinary chat,
reading the value back from the OS store, or printing it through tools.

A protected password form appears in Chat outside the transcript. It identifies
the connection and whether it will replace an existing key. **Save securely and
continue** posts the value directly to the existing core credential store. The
form never uses the chat composer, draft persistence, browser storage, or message
API for the entered value. It clears on save, cancel, session change, or page
exit; metadata refreshes preserve a masked draft the user is still entering.

Only a value-free continuation containing the connection reference enters the
normal turn queue after saving. The model can then apply that reference to the
chosen feature and verify it. The value is excluded from model context, session
logs, events, hook inputs, configuration files/backups, summaries, and memory
capture. The intended durable destination is the OS credential store.

The existing credential store must be available (for example, Secret Service on
Linux). A storage failure gives a generic error, retains the pending request,
and does not echo the value or arbitrary provider errors. A repeated successful
submission does not write another value or queue another continuation. Cancel,
Stop, archive, expiry, a changed credential destination, and another session's
request identity cannot be used to submit the old request. Pending metadata is
in memory only: after a core restart the agent must request a new form.

Pasting a key into ordinary chat is outside this protected path. The skill tells
the user to replace/revoke such a key and never quotes it back. This is not an OS
sandbox and does not change the permissions of other existing agent tools.

### Shared/native client contract

All routes use the core's existing Host, Origin, and authentication checks.

- Create a session with the existing `POST /api/sessions` fields plus
  `"purpose": "orientation"`.
- A durable `credential_request` event contains request metadata only.
- `GET /api/sessions/<slug>/credential-prompts` returns pending requests for that
  session, including after a client reload. It returns no entered values.
- `POST` to that same route accepts `request_id` and either `value` or
  `cancel: true`. A successful response reports only the reference and state.
- Requests expire after 30 minutes. One pending request is allowed per session,
  with a process-wide cap. The reply must remain bound to its original session.

Native clients can render their own secure field against this same contract;
no native launch behavior or packaging is changed here. The shared workspace
client implements it without placing a password form inside the message log.
