"use strict";
// Safe text formatting for the chat transcript. No HTML is parsed from the message.
(function () {
  function inline(source) {
    const parts = [];
    let text = "";
    let index = 0;
    while (index < source.length) {
      if (source.startsWith("**", index)) {
        const end = source.indexOf("**", index + 2);
        if (end > index + 2) {
          if (text) parts.push({ type: "text", text: text });
          text = "";
          parts.push({ type: "strong", text: source.slice(index + 2, end) });
          index = end + 2;
          continue;
        }
      }
      if (source[index] === "`") {
        const end = source.indexOf("`", index + 1);
        if (end > index + 1) {
          if (text) parts.push({ type: "text", text: text });
          text = "";
          parts.push({ type: "code", text: source.slice(index + 1, end) });
          index = end + 1;
          continue;
        }
      }
      text += source[index];
      index += 1;
    }
    if (text) parts.push({ type: "text", text: text });
    return parts;
  }

  function isTableRow(line) {
    const trimmed = line.trim();
    return trimmed.includes("|") && trimmed.split("|").length >= 3;
  }

  function isSeparator(line) {
    const cells = line.trim().split("|");
    let marks = 0;
    for (const cell of cells) {
      const token = cell.trim();
      if (!token) continue;
      if (!/^:?-{3,}:?$/.test(token)) return false;
      marks += 1;
    }
    return marks >= 2;
  }

  function cellsOf(line) {
    const raw = line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|");
    const cells = [];
    for (const cell of raw) cells.push(cell.trim());
    return cells;
  }

  function blocks(source) {
    const text = String(source || "").replace(/\r\n/g, "\n");
    const lines = text.split("\n");
    const out = [];
    let index = 0;
    while (index < lines.length) {
      const line = lines[index];
      if (line.startsWith("```")) {
        const code = [];
        index += 1;
        while (index < lines.length && !lines[index].startsWith("```")) {
          code.push(lines[index]);
          index += 1;
        }
        if (index < lines.length) index += 1;
        out.push({ type: "code", text: code.join("\n") });
        continue;
      }
      if (isTableRow(line) && index + 1 < lines.length && isSeparator(lines[index + 1])) {
        const header = cellsOf(line);
        const rows = [];
        index += 2;
        while (index < lines.length && isTableRow(lines[index])) {
          rows.push(cellsOf(lines[index]));
          index += 1;
        }
        out.push({ type: "table", header: header, rows: rows });
        continue;
      }
      const heading = /^(#{1,3}) (.+)$/.exec(line);
      if (heading) {
        out.push({ type: "heading", level: heading[1].length, inlines: inline(heading[2]) });
        index += 1;
        continue;
      }
      const bullet = /^[-*] (.+)$/.exec(line);
      const numbered = /^(\d+)\. (.+)$/.exec(line);
      if (bullet || numbered) {
        const items = [];
        const ordered = Boolean(numbered);
        while (index < lines.length) {
          const nextBullet = /^[-*] (.+)$/.exec(lines[index]);
          const nextNumber = /^(\d+)\. (.+)$/.exec(lines[index]);
          if (ordered && !nextNumber) break;
          if (!ordered && !nextBullet) break;
          items.push(inline((nextNumber || nextBullet)[ordered ? 2 : 1]));
          index += 1;
        }
        out.push({ type: "list", ordered: ordered, items: items });
        continue;
      }
      if (!line.trim()) {
        index += 1;
        continue;
      }
      const paragraph = [line];
      index += 1;
      while (index < lines.length && lines[index].trim() && !lines[index].startsWith("```") && !/^(#{1,3}) /.test(lines[index]) && !/^[-*] /.test(lines[index]) && !/^\d+\. /.test(lines[index]) && !(isTableRow(lines[index]) && index + 1 < lines.length && isSeparator(lines[index + 1]))) {
        paragraph.push(lines[index]);
        index += 1;
      }
      out.push({ type: "paragraph", inlines: inline(paragraph.join(" ")) });
    }
    return out;
  }

  window.TesseraFormat = { blocks: blocks, inline: inline };
})();
