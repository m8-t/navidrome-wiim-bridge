(function () {
  var theme = "dark";
  try {
    var stored = localStorage.getItem("remote-theme");
    if (stored === "macchiato" || stored === "frappe" || stored === "contrast") theme = stored;
  } catch (e) {
    // storage unavailable, keep the default
  }
  document.documentElement.dataset.theme = theme;
  var colors = { dark: "#0d0e11", macchiato: "#24273a", frappe: "#303446", contrast: "#000000" };
  var meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", colors[theme]);
})();
