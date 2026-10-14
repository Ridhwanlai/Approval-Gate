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

(function compareTabs() {
  const tabs = document.querySelectorAll('#compare [role="tab"]');
  const select = (tab) => tabs.forEach((t) => {
    const on = t === tab;
    t.setAttribute("aria-selected", String(on));
    t.tabIndex = on ? 0 : -1;
    document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
  });
  tabs.forEach((t, i) => {
    t.addEventListener("click", () => select(t));
    t.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
      select(next); next.focus();
    });
  });
  select(document.querySelector('#compare [aria-selected="true"]'));
})();
