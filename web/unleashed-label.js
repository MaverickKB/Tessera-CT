/* The approval button's label. The mode value stays yolo; this only paints words.
   The cartoon is web/brand/f-it-gremlin.svg. This file does not redraw it.
   The F-it setting alone switches the gremlin pack on. The icon picker is a
   plain logo choice and never blocks the pack. */
(() => {
  "use strict";

  const ALT = "Mischievous cartoon gremlin pressed against a block-letter IT";
  const LOGO_ALT = "Cartoon gremlin on a yellow sticker";
  const MARK_URL = "/static/brand/f-it-gremlin.svg";
  const LOGO_URL = "/static/brand/gremlin-logo.svg";

  function packOn(ui) {
    return Boolean(ui) && ui.easter_egg_f_it === true;
  }

  function activeMark(ui) {
    if (packOn(ui)) return "gremlin";
    return ui && ui.mark === "gremlin" ? "gremlin" : "cairn";
  }

  function iconFor(active) {
    if (active === "gremlin") return {href: LOGO_URL, type: "image/svg+xml", alt: LOGO_ALT};
    return null;
  }

  function mark() {
    const image = document.createElement("img");
    image.className = "f-it-mark";
    image.setAttribute("src", MARK_URL);
    image.setAttribute("alt", ALT);
    return image;
  }

  function paint(button, ui) {
    if (!button) return;
    const on = packOn(ui);
    const label = document.createElement("span");
    label.className = "approval-label";
    label.textContent = on ? "F-it" : "Unleashed";
    button.replaceChildren(label);
    if (on) button.append(mark());
  }

  window.TesseraUnleashed = {
    paint, mark, packOn, activeMark, iconFor, ALT, LOGO_ALT, MARK_URL, LOGO_URL,
  };
})();
