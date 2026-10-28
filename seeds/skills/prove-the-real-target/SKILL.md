---
name: prove-the-real-target
description: Use before claiming any change, fix, or feature is done. Turns a claim into a measurement on the surface the user actually touches, and names the missing proof honestly when the live path cannot be exercised yet.
---

# Prove the real target

A green test, a merged commit, a running service, and a plan are all supporting
evidence. None of them is the outcome the operator asked for. The outcome is
what happens on the surface they touch.

## The loop

1. **Name the outcome in their words.** "The phone app opens the project and
   shows its cards" — not "the sessions key is served".
2. **Find the live surface.** Which process serves it, on which port or host,
   under which route. Confirm it is running now, not that it was.
3. **Probe it early.** Before changing anything, capture what the surface
   currently does. Save the raw answer.
4. **Make the smallest change that addresses the observed blocker.**
5. **Probe again, identically.** The difference between the two answers is the
   result. Print both. Do not describe the difference from memory.

## When the live path cannot be exercised

Say which half is proven and which half is not, in the same sentence. "The
route is served and returns the shape; the page that reads it is not reloaded
yet, so I have not seen it render." That sentence is worth more than a
confident claim, because the next session can act on it.

Never upgrade a simulation to a delivery. If a test skips, it measured
nothing — check for the skip, not just for a success line. A skip and a pass
can print the same words.

## Closing

Close a card or report success only on the live evidence: the probe, the real
user path, or the operator's own ruling. If something still waits on a reload,
a device, or an approval, leave the work open with the missing proof named.
