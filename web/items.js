"use strict";
// Compact items for tool calls, results and system notes: a kind, a one-line summary and a structured body.
(function () {
  function parse(text) {
    try { return JSON.parse(text); } catch (_) { return undefined; }
  }

  // A clipped recall breaks the JSON; keep every layer object that is still whole.
  function salvageLayers(text) {
    const start = text.indexOf('"layers"');
    if (start < 0) return undefined;
    const layers = [];
    let depth = 0, quoted = false, escaped = false, open = -1;
    for (let i = text.indexOf("[", start) + 1; i > 0 && i < text.length; i++) {
      const c = text[i];
      if (quoted) {
        if (escaped) escaped = false;
        else if (c === "\\") escaped = true;
        else if (c === '"') quoted = false;
        continue;
      }
      if (c === '"') quoted = true;
      else if (c === "{") { if (depth++ === 0) open = i; }
      else if (c === "}" && depth > 0 && --depth === 0) {
        try { layers.push(JSON.parse(text.slice(open, i + 1))); } catch (_) { /* The clipped layer is dropped. */ }
      } else if (c === "]" && depth === 0) break;
    }
    return layers.length ? { layers, clipped: true } : undefined;
  }

  function oneLine(value, limit) {
    const cap = Number.isFinite(limit) && limit > 0 ? limit : 120;
    const text = (typeof value === "string" ? value : JSON.stringify(value) || "").replace(/\s+/g, " ").trim();
    return text.length > cap ? text.slice(0, cap - 1).trimEnd() + "…" : text;
  }

  function fieldValue(value) {
    if (typeof value !== "string") return JSON.stringify(value, null, 2);
    const nested = value.trim().startsWith("{") || value.trim().startsWith("[") ? parse(value) : undefined;
    return nested === undefined ? value : JSON.stringify(nested, null, 2);
  }

  function bodyOf(value, text) {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      if (Array.isArray(value.layers)) {
        return { kind: "layers", layers: value.layers.map((layer) => ({
          head: [layer.type, layer.author, layer.timestamp || layer.ts].filter(Boolean).join(" · "),
          text: typeof layer.body === "string" ? layer.body : JSON.stringify(layer, null, 2),
        })) };
      }
      return { kind: "fields", fields: Object.entries(value).map(([key, item]) => [key, fieldValue(item)]) };
    }
    return { kind: "text", text: value === undefined || typeof value === "string" ? text : JSON.stringify(value, null, 2) };
  }

  function resultSummary(value, text, limit) {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      if (Array.isArray(value.layers)) return value.layers.length + (value.layers.length === 1 ? " layer" : " layers") + (value.clipped ? " (clipped)" : "");
      const parts = Object.entries(value).filter(([, item]) => item === null || typeof item !== "object")
        .map(([key, item]) => key + ": " + item);
      return oneLine(parts.join(", ") || Object.keys(value).join(", "), limit);
    }
    return oneLine(text.split("\n").find((line) => line.trim()) || "", limit);
  }

  function toolTitle(name, args = {}) {
    const titles = {
      read_file: "Read file", write_file: "Write file", edit_file: "Edit file",
      shell: "Run command", spawn_agent: "Delegate work", swarm_job: "Run agent job",
      memory_recall: "Recall memory", memory_note: "Save memory", skill_load: "Load skill",
      zoom: "Open original context",
    };
    const label = titles[name] || name;
    let target = args.path || args.filename || args.command || args.task || args.query || args.name || "";
    if (typeof target !== "string") target = JSON.stringify(target);
    return {label, target: oneLine(target, 100)};
  }

  // Returns null for conversation turns (user, agent, work); everything else becomes a compact item.
  function describe(message, limit = 120) {
    const kind = message.kind || "agent";
    const text = typeof message.text === "string" ? message.text : "";
    const meta = message.meta || {};
    if (kind === "tool") {
      const call = parse(text) || {};
      const name = typeof call.name === "string" ? call.name : "tool";
      const args = call.arguments && typeof call.arguments === "object" ? call.arguments : {};
      if (name === "memory_note") {
        return { kind: "memory", label: "Memory note", summary: oneLine(args.text || "", limit), body: bodyOf(args, text) };
      }
      const title = toolTitle(name, args);
      let summary = title.target;
      if (!summary) {
        const fields = [];
        for (const [key, value] of Object.entries(args)) fields.push(key + ": " + oneLine(value, 60));
        summary = fields.join(", ");
      }
      return { kind: "tool", label: title.label, summary: oneLine(summary, limit), body: bodyOf(args, text) };
    }
    if (kind === "echo") {
      const value = parse(text) ?? salvageLayers(text);
      const label = meta.blocked ? "Blocked" : meta.success === false ? "Failed" : "Result";
      return { kind: "result", label, summary: resultSummary(value, text, limit), body: bodyOf(value, text) };
    }
    if (kind !== "note") return null;
    if (meta.label === "skill-shelf") {
      const lines = text.split("\n").slice(1);
      return { kind: "memory", label: "Skill shelf", summary: `${meta.count ?? lines.length} skills on the shelf`, body: { kind: "text", text: lines.join("\n") } };
    }
    const colon = text.indexOf(":");
    const prefix = colon > 0 && colon <= 40 ? text.slice(0, colon) : "";
    const rest = prefix ? text.slice(colon + 1).trim() : text;
    const value = parse(rest) ?? salvageLayers(rest);
    let label = prefix || "Note";
    if (meta.approval_result) label = "Approval";
    else if (meta.source === "inspector") label = "Inspector";
    return { kind: "note", label, summary: resultSummary(value, rest, limit), body: bodyOf(value, rest) };
  }

  function memoryRows(view) {
    const rows = [];
    for (const line of view.split("\n")) {
      if (!line.trim()) continue;
      const match = line.match(/^\[x(\d+)-(\d+) L(\d+)\] (.*)$/);
      if (match) {
        const start = Number(match[1]);
        const level = Number(match[3]);
        const index = start / (2 ** level);
        if (Number.isSafeInteger(index) && level <= 64) {
          rows.push({text: match[4], level, index, label: "Records " + match[1] + " to " + match[2]});
          continue;
        }
      }
      rows.push({text: line, label: oneLine(line, 100)});
    }
    return rows;
  }

  window.TesseraItems = { describe, toolTitle, memoryRows };
})();
