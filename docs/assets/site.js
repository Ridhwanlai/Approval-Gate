/* Para site behaviour. Static: nothing here calls an API. */
(function theme() {
  const root = document.documentElement, btn = document.getElementById("theme");
  let saved = null;
  try { saved = localStorage.getItem("para-theme"); } catch (e) { /* storage blocked: follow the system */ }
  if (saved === "light" || saved === "dark") root.dataset.theme = saved;
  const current = () => root.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const label = () => { if (btn) btn.textContent = current() === "dark" ? "Light mode" : "Dark mode"; };
  label();
  if (btn) btn.onclick = () => {
    root.dataset.theme = current() === "dark" ? "light" : "dark";
    try { localStorage.setItem("para-theme", root.dataset.theme); } catch (e) { /* not remembered */ }
    label();
  };
})();
