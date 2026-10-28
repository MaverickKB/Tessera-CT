/* The shared-key handling for a proxied core.
 *
 * Kept in its own file so it can be tested without a DOM: the behaviour that
 * matters here is "does the key reach the request, and does a missing or wrong
 * key leave the operator able to fix it", not layout.
 *
 * Three forms exist because no single one covers every client:
 *   Authorization: Bearer  -- the iOS app already sends this (server side)
 *   X-Tessera-Key          -- fetch() and scripts
 *   ?key=                  -- EventSource cannot set headers
 */
(() => {
  "use strict";
  const STORAGE = "tessera.auth.key";

  function storedKey(storage) {
    const store = storage || (typeof localStorage !== "undefined" ? localStorage : null);
    if (!store) return "";
    try { return store.getItem(STORAGE) || ""; } catch (_) { return ""; }
  }

  /* Store a ?key= from the URL once, then strip it so it is not left in the
   * address bar, the history, or a link the operator copies to a colleague. */
  function takeKeyFromLocation(win, storage) {
    const window_ = win || (typeof window !== "undefined" ? window : null);
    const store = storage || (typeof localStorage !== "undefined" ? localStorage : null);
    if (!window_ || !store) return "";
    let url;
    try { url = new URL(window_.location.href); } catch (_) { return ""; }
    const supplied = url.searchParams.get("key");
    if (!supplied) return "";
    try { store.setItem(STORAGE, supplied); } catch (_) { /* private mode */ }
    url.searchParams.delete("key");
    try {
      window_.history.replaceState(null, "", url.pathname + url.search + url.hash);
    } catch (_) { /* ignore */ }
    return supplied;
  }

  function headers(storage) {
    const out = { Accept: "application/json" };
    const key = storedKey(storage);
    if (key) out["X-Tessera-Key"] = key;
    return out;
  }

  /* EventSource cannot set request headers, so the stream carries the key in the
   * query string. Without this, turning auth on would break live updates and look
   * like a dead page -- the exact way a shared key strands the operator. */
  function withKey(path, storage) {
    const key = storedKey(storage);
    if (!key) return path;
    return path + (path.includes("?") ? "&" : "?") + "key=" + encodeURIComponent(key);
  }

  window.TesseraAuthKey = Object.freeze({ STORAGE, storedKey, takeKeyFromLocation, headers, withKey });
})();
