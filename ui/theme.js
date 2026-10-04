// Theme toggle: system -> paper -> stage. The pre-paint script in app.html
// applies the stored (or default) choice before first paint; this file wires
// the button and keeps the paired theme-color metas in step with an explicit
// choice. Storage is a per-viewer convenience; every access is guarded.
(function () {
  var KEY = "orchestral.theme";
  var ORDER = ["system", "paper", "stage"];
  var LABEL = { system: "Theme: System", paper: "Theme: Paper", stage: "Theme: Stage" };
  var root = document.documentElement;
  var btn = document.getElementById("theme-toggle");
  var metas = document.querySelectorAll('meta[name="theme-color"]');
  var original = [];
  metas.forEach(function (m) { original.push(m.getAttribute("content")); });

  function metaFor(theme) {
    // metas are [light, dark]; a forced theme overrides both
    var forced = theme === "paper" ? original[0] : theme === "stage" ? original[1] : null;
    metas.forEach(function (m, i) { m.setAttribute("content", forced || original[i]); });
  }

  function apply(theme) {
    if (root.getAttribute("data-theme") !== theme) root.setAttribute("data-theme", theme);
    metaFor(theme);
    if (btn) {
      btn.textContent = LABEL[theme];
      btn.setAttribute("aria-label", "Colour theme: " + theme + ". Activate to change.");
    }
  }

  function current() {
    var t = root.getAttribute("data-theme");
    return ORDER.indexOf(t) >= 0 ? t : "system";
  }

  if (btn) {
    btn.addEventListener("click", function () {
      var next = ORDER[(ORDER.indexOf(current()) + 1) % ORDER.length];
      try { localStorage.setItem(KEY, next); } catch (e) {}
      apply(next);
    });
  }
  apply(current());
})();
