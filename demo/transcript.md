# Tessera demo transcript

Mode: scripted FakeBackend; offline; temporary files

The offline path asserts every listed feature against the real Session modules.

## Turn 1

**User:** Create note.txt with the decision and inspect it using tools.

**Agent:** Created and read note.txt. The shell smoke passed. The decision is preserved in the note.

## Turn 2

**User:** Use tools to ask a helper to read note.txt and report its contents.

**Agent:** The helper report confirms the note contents.

## Turn 3

**User:** Use zoom tools to recover the original first request and date.

**Agent:** Zoom recovered the exact first request; date returned its original UTC timestamp.

## Turn 4

**User:** What is two plus two?

**Agent:** Four.

## Turn 5

**User:** Delegate an independent task to read note.txt and report the decision.

**Agent:** Started [turn-5]. Its report will be appended to this chat.

## Background report

[reader] note.txt says to keep the original request recoverable through zoom.
[turn-5] note.txt says to keep the original request recoverable through zoom.

## Persisted summary view after compaction

```text
<chat>
0+16|[user] Create note.txt with the decision and inspect it using tools. [user] Use tools to ask a helper to read note.txt and repor
16+4|[user] Use zoom tools to recover the original first request and date. [agent] The helper report confirms the note contents. [age
20+2|[echo] Create note.txt with the decision and inspect it using tools. [tool] {"name": "date", "arguments": {"i": 0}}
22+1|2026-10-15T14:59:49.894320+00:00
23+1|Zoom recovered the exact first request; date returned its original UTC timestamp.
24+1|What is two plus two?
25+1|Four.
26+1|Delegate an independent task to read note.txt and report the decision.
27+1|Started [turn-5]. Its report will be appended to this chat.
28+1|[turn-5] note.txt says to keep the original request recoverable through zoom.
</chat>
```

## Routing and capture evidence

```json
{
  "routes": [
    {
      "route": "tools",
      "p": 0.8,
      "probabilities": {
        "direct": 0.1,
        "tools": 0.8,
        "delegate": 0.1
      },
      "source": "heuristic"
    },
    {
      "route": "direct",
      "p": 0.8,
      "probabilities": {
        "direct": 0.8,
        "tools": 0.1,
        "delegate": 0.1
      },
      "source": "heuristic"
    },
    {
      "route": "delegate",
      "p": 0.8,
      "probabilities": {
        "direct": 0.1,
        "tools": 0.1,
        "delegate": 0.8
      },
      "source": "heuristic"
    }
  ],
  "status_before_close": {
    "backends": [],
    "roles": {},
    "messages": 29,
    "view_bytes": 790,
    "write_debt": 5,
    "active_agents": 0
  },
  "queued_captures_after_close": 6
}
```

Offline write-debt proves durable local queuing. It does not prove delivery to a live memory service.
