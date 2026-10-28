/* Header crew for the gremlin pack. The tree stays in the page.
   This file decides the pose from signals the page already shows.
   It does not invent a workload. */
(() => {
  "use strict";

  const TREE = "Tessera.";
  const NAP = "Napping.";
  const HAUL = "Hauling.";
  const TUMBLE = "One took a tumble.";
  const TURN_ERROR = "Turn ended with an error";
  const RUNNING = new Set(["running"]);
  const ATTENTION = new Set(["failed", "blocked"]);

  function whole(value) {
    return Number.isInteger(value) && value > 0 ? value : 0;
  }

  function rows(list, states) {
    if (!Array.isArray(list)) return 0;
    let count = 0;
    for (const row of list) {
      if (row && typeof row.status === "string" && states.has(row.status)) count += 1;
    }
    return count;
  }

  function text(value) {
    return typeof value === "string" && value.trim() !== "";
  }

  function motion(input) {
    const source = input && typeof input === "object" ? input : {};
    if (source.pack !== true) return {pose: "tree", goblins: 0, caption: TREE};
    if (source.connected !== true) return {pose: "nap", goblins: 1, caption: NAP};
    const load = (source.busy === true ? 1 : 0)
      + whole(source.activeAgents)
      + whole(source.queued)
      + rows(source.jobs, RUNNING)
      + rows(source.cards, RUNNING);
    const error = text(source.sessionError) || source.turnError === true
      || rows(source.jobs, ATTENTION) > 0
      || rows(source.cards, ATTENTION) > 0;
    if (error) {
      const haulers = Math.min(2, load);
      return {pose: "tumble", goblins: 1 + haulers, caption: TUMBLE};
    }
    if (load === 0) return {pose: "nap", goblins: 1, caption: NAP};
    return {pose: "haul", goblins: load >= 4 ? 3 : load >= 2 ? 2 : 1, caption: HAUL};
  }

  function apply(trail, caption, input) {
    const result = motion(input);
    if (trail && trail.dataset) {
      trail.dataset.motion = result.pose;
      trail.dataset.goblins = String(result.goblins);
    }
    if (caption) caption.textContent = result.caption;
    return result;
  }

  window.TesseraGremlinTrail = {motion, apply, TREE, NAP, HAUL, TUMBLE, TURN_ERROR};
})();
