/* Hide the macOS missing-object sentence and give the banner a usable line. */
(function () {
  if (window.__tesseraShellGuard) return;
  window.__tesseraShellGuard = true;

  function textOf(value) {
    if (typeof value === "string") return value;
    if (value && typeof value.message === "string") return value.message;
    return "";
  }

  function isMissingObject(value) {
    return textOf(value).indexOf("object can not be found here") !== -1;
  }

  function bannerText(message) {
    var text = textOf(message).trim();
    if (!text) return "";
    if (isMissingObject(text)) return "";
    if (text.indexOf("Interrupted by a previous runtime") === 0) return "";
    if (text === "Not found") return "The core does not have that file.";
    return text;
  }

  var previous = window.TesseraShell || {};
  window.TesseraShell = {
    bannerText: bannerText,
    isMissingObject: isMissingObject,
    platform: previous.platform,
    service: previous.service
  };

  window.addEventListener("error", function (event) {
    if (!isMissingObject(event.message)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);

  window.addEventListener("unhandledrejection", function (event) {
    if (!isMissingObject(event.reason)) return;
    event.preventDefault();
  });

  var webkit = window.webkit;
  if (!webkit || !webkit.messageHandlers) return;
  var handlers = webkit.messageHandlers;
  var quietHandler = {
    postMessage: function () {}
  };
  var proxy = new Proxy(handlers, {
    get: function (target, name, receiver) {
      if (typeof name !== "string") return Reflect.get(target, name, receiver);
      var found = null;
      try {
        found = Reflect.get(target, name, receiver);
      } catch (error) {
        if (!isMissingObject(error)) throw error;
        return quietHandler;
      }
      if (found) return found;
      return quietHandler;
    }
  });
  try {
    Object.defineProperty(webkit, "messageHandlers", {
      configurable: true,
      get: function () { return proxy; }
    });
  } catch (error) {
    // The native handlers object can be sealed. The listeners above still drop the banner.
  }
})();
