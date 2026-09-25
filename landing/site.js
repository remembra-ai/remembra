/* Remembra site: theme toggle, mobile menu, sticky header rule and
   copy-to-clipboard. No dependencies. Dark is the default; the page's
   inline head script has already applied a saved choice before paint. */
(function () {
  "use strict";
  var root = document.documentElement;
  var KEY = "remembra-theme";

  /* ---------------- Theme toggle ---------------- */
  function current() { return root.getAttribute("data-theme") === "light" ? "light" : "dark"; }
  function labelToggles() {
    var next = current() === "dark" ? "light" : "dark";
    document.querySelectorAll("[data-theme-toggle]").forEach(function (b) {
      b.setAttribute("aria-label", "Switch to " + next + " theme");
      b.setAttribute("title", "Switch to " + next + " theme");
    });
  }
  document.querySelectorAll("[data-theme-toggle]").forEach(function (b) {
    b.addEventListener("click", function () {
      var next = current() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem(KEY, next); } catch (e) { /* storage unavailable: the theme still applies for this visit */ }
      labelToggles();
      window.dispatchEvent(new CustomEvent("remembra:theme", { detail: { theme: next } }));
    });
  });
  labelToggles();

  /* ---------------- Mobile menu ---------------- */
  var menuBtn = document.querySelector("[data-menu-toggle]");
  var menu = document.getElementById("site-menu");
  if (menuBtn && menu) {
    var setOpen = function (open) {
      menuBtn.setAttribute("aria-expanded", String(open));
      menuBtn.setAttribute("aria-label", open ? "Close menu" : "Open menu");
      menu.classList.toggle("is-open", open);
    };
    menuBtn.addEventListener("click", function () { setOpen(menuBtn.getAttribute("aria-expanded") !== "true"); });
    menu.addEventListener("click", function (e) { if (e.target.closest("a")) setOpen(false); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && menuBtn.getAttribute("aria-expanded") === "true") { setOpen(false); menuBtn.focus(); }
    });
    window.addEventListener("resize", function () { if (window.innerWidth > 860) setOpen(false); });
  }

  /* ---------------- Header rule once the page scrolls ---------------- */
  var header = document.querySelector(".site-header");
  if (header) {
    var onScroll = function () { header.classList.toggle("is-stuck", window.scrollY > 4); };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }

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
