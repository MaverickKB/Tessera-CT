/* Shared board chrome. Scoped to the infrastructure and WIP pages. */
(() => {
  "use strict";

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function svg(name) {
    if (typeof document.createElementNS === "function") {
      return document.createElementNS("http://www.w3.org/2000/svg", name);
    }
    return document.createElement(name);
  }

  function statusDot(tone) {
    const dot = el("span", "ds-dot");
    if (tone) dot.dataset.tone = tone;
    dot.setAttribute("aria-hidden", "true");
    return dot;
  }

  function pill(text, tone) {
    const node = el("span", "ds-pill", text);
    if (tone) node.dataset.tone = tone;
    return node;
  }

  function classify(node, name) {
    node.setAttribute("class", name);
    // SVG className is an object in the browser; the unit-test element stores a string.
    if (typeof node.className === "string") node.className = name;
  }

  function meter(ratio, tone) {
    // Width is an SVG geometry attribute. Inline CSS is dropped by the page
    // content policy (style-src 'self'), which left every fill at width: 0.
    const bar = svg("svg");
    classify(bar, "ds-meter");
    bar.setAttribute("aria-hidden", "true");
    bar.setAttribute("viewBox", "0 0 100 4");
    bar.setAttribute("preserveAspectRatio", "none");
    bar.setAttribute("width", "100%");
    bar.setAttribute("height", "4");
    const fill = svg("rect");
    classify(fill, "ds-meter-fill");
    if (tone) fill.dataset.tone = tone;
    const width = Math.max(0, Math.min(100, Math.round(Number(ratio) * 100)));
    fill.setAttribute("x", "0");
    fill.setAttribute("y", "0");
    fill.setAttribute("width", String(width));
    fill.setAttribute("height", "4");
    bar.append(fill);
    return bar;
  }

  function metricRow(label, value, options = {}) {
    const row = el("div", "ds-metric");
    row.append(el("span", "ds-metric-label", label), el("span", "ds-metric-value", value));
    if (typeof options.ratio === "number" && Number.isFinite(options.ratio)) row.append(meter(options.ratio, options.tone || ""));
    return row;
  }

  function bigNumber(value, unit) {
    const node = el("div", "ds-big");
    node.append(el("span", "ds-big-value", value));
    if (unit) node.append(el("span", "ds-big-unit", unit));
    return node;
  }

  function kpi(label, value, unit) {
    const tile = el("div", "ds-kpi");
    tile.append(el("div", "ds-kicker", label), bigNumber(value, unit));
    return tile;
  }

  function sparkline(points, tone) {
    const nums = (Array.isArray(points) ? points : []).filter((value) => typeof value === "number" && Number.isFinite(value));
    if (nums.length < 2) return null;
    const wrap = el("span", "ds-spark");
    wrap.setAttribute("aria-hidden", "true");
    const plot = svg("svg");
    plot.setAttribute("class", "ds-spark-line");
    plot.setAttribute("viewBox", "0 0 120 28");
    plot.setAttribute("preserveAspectRatio", "none");
    const min = Math.min(...nums);
    const span = Math.max(...nums) - min || 1;
    const coords = nums.map((value, index) => {
      const x = (index / (nums.length - 1)) * 120;
      const y = 26 - ((value - min) / span) * 22;
      return x.toFixed(1) + "," + y.toFixed(1);
    }).join(" ");
    const line = svg("polyline");
    line.setAttribute("points", coords);
    line.setAttribute("fill", "none");
    if (tone) line.setAttribute("data-tone", tone);
    plot.append(line);
    wrap.append(plot);
    return wrap;
  }

  function emptyState(label) {
    return el("div", "ds-empty", label);
  }

  function activityFeed(items, live) {
    const feed = el("section", "ds-feed");
    const head = el("div", "ds-feed-head");
    head.append(el("h2", "ds-kicker", "Activity"));
    if (live) head.append(pill("Live", "ready"));
    feed.append(head);
    if (!items.length) {
      feed.append(emptyState("No activity"));
      return feed;
    }
    const list = el("ul", "ds-feed-list");
    for (const item of items) {
      const row = el("li", "ds-feed-item");
      row.append(statusDot(item.tone || ""));
      row.append(el("span", "ds-feed-time", item.time || "—"));
      row.append(el("span", "ds-feed-text", item.text));
      list.append(row);
    }
    feed.append(list);
    return feed;
  }

  window.TesseraDesign = {statusDot, pill, meter, metricRow, bigNumber, kpi, sparkline, emptyState, activityFeed};
})();
