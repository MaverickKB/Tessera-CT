---
name: search-before-building
description: Use before writing new code, a new script, a new integration, or a new procedure. Checks the shelf, the tree, and the docs for what already exists, so you extend the general machine instead of adding a parallel one.
---

# Search before building

Most of what looks missing is already somewhere in the tree, or was solved once
and written down. Building a second copy of a thing is the most common way to
turn a small task into a large one.

## Order of search

1. **The skill shelf.** Search by plain text for the thing you are about to do.
   Load the skill that matches before acting on the hit. If it does not fit,
   say why and continue — a skill is guidance, not a gate.
2. **The tree.** Grep for the concept, not just the word you would have chosen.
   Another author may have called it differently. Look for the existing
   function, config key, route, or tool.
3. **The docs and the worklog.** Prior decisions and their reasons live there.
   A design that looks odd usually has a note explaining what it was avoiding.
4. **The live system.** Ask the running process what it actually serves. Source
   and served content can differ, and the difference matters.

## Then decide

- **Extend** when the existing thing is the general capability and needs one
  more case.
- **Add** when the existing thing genuinely cannot express what you need, and
  say in the commit message why extending it was wrong.
- **Do not** hand-carve a special case to satisfy one caller. Schemas should
  close over the state space, not over today's test.

## After

If you built something that will be done again, write it as a skill: what it is
for, the order of steps, and the failure it prevents. One procedure per skill.
