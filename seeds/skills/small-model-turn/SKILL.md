---
name: small-model-turn
description: Use when you are running on a small local model, or when a turn is drifting into long confident prose with nothing measured behind it. Keeps one turn to one check, one answer, and one next step, with the evidence quoted rather than described.
---

# Small model turn

A small model can do excellent work in this harness. What it cannot afford is
invention. The failure mode is a long, fluent answer built from assumptions,
which reads well and is wrong.

## The shape of a good turn

1. **One check.** Pick the single fact that decides your next step. Run it.
   A read, a status call, a grep, one command.
2. **Read the answer.** Actually look at what came back before deciding
   anything. Do not decide the next step from a plan you wrote before you had
   the answer.
3. **One next step.** From the answer, not from the plan.
4. **Quote the evidence.** The command and its result. The file and the line.
   When something fails, quote the failure instead of paraphrasing it —
   paraphrasing loses the detail that identifies the cause.

## Budget yourself

- One check, then a decision. Two checks in a row without a decision is a sign
  you are collecting rather than converging.
- If a step fails twice the same way, change the approach, not the retry
  count.
- If you cannot find the thing you expected, say "not found" and name where you
  looked. Do not fill the gap with a plausible guess.

## Writing the reply

Short. Name what you observed, what you inferred from it, and what you still
do not know — in that order, if the order matters. A confident paragraph with
no probe behind it is worse than three lines that say what you actually saw.

## When you are not sure

Say so, and make the uncertainty specific: "the route answers, but I have not
seen the page read it" is usable. "It should work" is not.
