/* Remembra site: theme toggle and copy-to-clipboard. No dependencies. */
(function () {
  "use strict";
  var root = document.documentElement;
  var KEY = "remembra-theme";

  /* ---------------- Theme toggle ---------------- */
  var mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  function effectiveTheme() {
    var t = root.getAttribute("data-theme");
    if (t === "light" || t === "dark") return t;
    return mq && mq.matches ? "dark" : "light";
  }
  function labelToggles() {
    var next = effectiveTheme() === "dark" ? "light" : "dark";
    document.querySelectorAll("[data-theme-toggle]").forEach(function (b) {
      b.setAttribute("aria-label", "Switch to " + next + " theme");
    });
  }
  document.querySelectorAll("[data-theme-toggle]").forEach(function (b) {
    b.addEventListener("click", function () {
      var next = effectiveTheme() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem(KEY, next); } catch (e) { /* storage unavailable: theme still applies for this visit */ }
      labelToggles();
    });
  });
  if (mq && mq.addEventListener) mq.addEventListener("change", labelToggles);
  labelToggles();

  /* ---------------- Copy command ---------------- */
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    var label = btn.querySelector(".copy-label");
    var status = btn.parentNode.querySelector("[data-copy-status]");
    var idle = btn.getAttribute("aria-label") || "Copy";
    var timer;
    btn.addEventListener("click", function () {
      var text = btn.getAttribute("data-copy");
      function done(ok) {
        btn.setAttribute("data-state", ok ? "copied" : "failed");
        if (label) label.textContent = ok ? "Copied" : "Select";
        if (status) status.textContent = ok ? "Copied to clipboard." : "Copy failed. Select the command and copy it manually.";
        clearTimeout(timer);
        timer = setTimeout(function () {
          btn.removeAttribute("data-state");
          if (label) label.textContent = "Copy";
          if (status) status.textContent = "";
          btn.setAttribute("aria-label", idle);
        }, 1800);
      }
      function fallback() {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed"; ta.style.top = "0"; ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        var ok = false;
        try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
        document.body.removeChild(ta);
        done(ok);
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () { done(true); }, fallback);
      } else {
        fallback();
      }
    });
  });
})();
