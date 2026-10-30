/* The approval button's label. The mode value stays yolo; this only paints words.
   The art is the goblin avatar set in web/brand/goblin, one folder per avatar
   with a 64px and a 256px transparent PNG. The F-it setting alone switches the
   goblin pack on. The logo picker is a plain choice and never blocks it. */
(() => {
  "use strict";

  const ALT = "Goblin teammate avatar";
  const LOGO_ALT = "Goblin avatar on the app icon";
  const AVATAR_BASE = "/static/brand/goblin";
  const DEFAULT_AVATAR = "smith";
  const AVATAR_ID = /^[a-z][a-z0-9-]{0,31}$/;

  function avatarId(ui) {
    const value = ui && typeof ui.goblin_avatar === "string" ? ui.goblin_avatar : "";
    if (AVATAR_ID.test(value)) return value;
    return DEFAULT_AVATAR;
  }

  function avatarUrl(ui, size) {
    return AVATAR_BASE + "/" + avatarId(ui) + "/avatar-" + size + ".png";
  }

  // The default avatar, kept as constants so a caller that wants one fixed URL
  // does not have to know the folder layout.
  const MARK_URL = AVATAR_BASE + "/" + DEFAULT_AVATAR + "/avatar-64.png";
  const LOGO_URL = AVATAR_BASE + "/" + DEFAULT_AVATAR + "/avatar-256.png";

  function packOn(ui) {
    return Boolean(ui) && ui.easter_egg_f_it === true;
  }

  function activeMark(ui) {
    if (packOn(ui)) return "gremlin";
    return ui && ui.mark === "gremlin" ? "gremlin" : "cairn";
  }

  function iconFor(active, ui) {
    if (active === "gremlin") return {href: avatarUrl(ui, 256), type: "image/png", alt: LOGO_ALT};
    return null;
  }

  function mark(ui) {
    const image = document.createElement("img");
    image.className = "f-it-mark";
    image.setAttribute("src", avatarUrl(ui, 64));
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
    if (on) button.append(mark(ui));
  }

  window.TesseraUnleashed = {
    paint, mark, packOn, activeMark, iconFor, avatarId, avatarUrl,
    ALT, LOGO_ALT, MARK_URL, LOGO_URL, DEFAULT_AVATAR,
  };
})();
