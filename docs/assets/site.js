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

/* Replay demo: renders recorded decisions from demo-data.json. The page decides nothing. */
(function demo() {
  const docket = document.getElementById("docket"), panel = document.getElementById("decision");
  if (!docket) return;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const LABEL = { APPROVE: "Approved", HOLD_FOR_REVIEW: "Held for review", BLOCK: "Blocked" };
  const GLYPH = { APPROVE: "✓", HOLD_FOR_REVIEW: "‖", BLOCK: "✕" };
  const CHIP = { APPROVE: "ok", HOLD_FOR_REVIEW: "hold", BLOCK: "block" };
  const STATUS = { verified: ["Verified", "ok"], unverified: ["Not on firm list", "hold"], fabricated: ["Fabricated", "block"], near_miss: ["Misstated", "block"], misstated: ["Misstated", "block"] };

  function annotate(text, evidence) {
    let html = esc(text);
    evidence.filter((e) => e.cited && e.status).sort((a, b) => b.cited.length - a.cited.length).forEach((c) => {
      const key = esc(c.cited);
      if (html.includes(key) && !html.includes(`>${key}</mark>`)) html = html.split(key).join(`<mark class="${esc(c.status)}">${key}</mark>`);
    });
    return html;
  }

  function evidenceHtml(evidence) {
    let h = "";
    const cites = evidence.filter((e) => e.cited);
    if (cites.length) h += `<h4>Firm citation lists</h4><table class="ev"><thead><tr><th>In the memo</th><th>Firm list</th><th>Status</th></tr></thead><tbody>${cites.map((c) => {
      const [label, cls] = STATUS[c.status] || [c.status, "hold"];
      return `<tr><td>${esc(c.cited)}</td><td>${esc(c.firm_list_entry || "Not on either list")}${c.note ? `<small>${esc(c.note)}</small>` : ""}</td><td><b class="st ${cls}">${esc(label)}</b></td></tr>`;
    }).join("")}</tbody></table>`;
    const rules = evidence.filter((e) => e.signal === "rule_check");
    rules.forEach((r) => { h += `<h4>Rule check</h4><p class="body">${esc(r.rule)}: ${esc((r.matches || []).join(", "))}</p>`; });
    const ez = evidence.find((e) => e.signal === "ezio");
    if (ez && ez.reachable) {
      const low = (v, floor) => typeof v === "number" && v < floor ? " low" : "";
      h += `<h4>Ezio</h4><div class="ezio"><span>Tier <b>${esc(ez.tier)} ${esc(Math.round(ez.score))}</b></span><span>Accuracy <b>${esc(String(ez.accuracy_status).replace("_", " "))}</b></span>
        <span class="${low(ez.tone, 60)}">Tone <b>${esc(Math.round(ez.tone))}</b></span><span class="${low(ez.bias, 60)}">Bias <b>${esc(Math.round(ez.bias))}</b></span>
        <span class="${ez.claims_contradicted ? "low" : ""}">Contradicted <b>${esc(ez.claims_contradicted)}</b></span></div>`;
      (ez.contradicted_claims || []).forEach((c) => { h += `<p class="body">Ezio contradicted: “${esc(c.claim)}”${c.source ? ` (${esc(c.source)})` : ""}</p>`; });
    }
    const agents = evidence.filter((e) => e.signal === "agent");
    if (agents.length) {
      h += `<h4>AI agents on HarnessRouter</h4><div class="ai-find">${agents.map((a) => {
        const finds = (a.findings || []).map((f) => f.status
          ? `“${esc(f.quote)}” → <b>${esc((STATUS[f.status] || [f.status])[0])}</b>${f.problems && f.problems.length ? ` · ${esc(f.problems.join("; "))}` : ""}`
          : `<b>${esc(String(f.kind).replace("_", "-"))}</b>: “${esc(f.quote)}” · ${esc(f.why)}`);
        const body = !a.ok ? esc(a.error) : finds.length ? finds.join("<br>") : esc(a.notes || (a.feature === "memo_style_review" ? "Reads as analysis, not advocacy." : "No authorities found."));
        return `<div class="row"><b>${esc(a.label)}</b><br>${body}<div class="meta">${esc(a.harness)} · ${esc(a.model || "")}</div></div>`;
      }).join("")}</div>`;
    }
    return h;
  }

  function show(m, memos) {
    docket.querySelectorAll("button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.id === m.id)));
    const sent = m.sent ? "Sent to the partner." : "Not sent. The partner never received it.";
    let fix = "";
    if (m.revision) fix = `<div class="fix"><button class="btn ghost small" type="button" id="showfix">Draft a fix with AI ✎</button><div id="fixbox"></div></div>`;
    else if (m.decision !== "APPROVE") fix = `<div class="fix"><p class="note">In the app, “Draft a fix with AI” asks the revision drafter for a corrected draft. Recorded examples: pl-07 and pl-12.</p></div>`;
    panel.innerHTML = `<div class="stamp-big ${esc(m.decision)}"><span aria-hidden="true">${GLYPH[m.decision]}</span>${esc(LABEL[m.decision])}</div>
      <p class="reason">${esc(m.reason)}</p><p class="sent">${sent}</p>
      <h4>The memo · ${esc(m.id)}</h4><div class="memo-view">${annotate(m.text, m.evidence)}</div>
      ${evidenceHtml(m.evidence)}${fix}`;
    const btn = document.getElementById("showfix");
    if (btn) btn.onclick = () => {
      const r = m.revision;
      btn.remove();
      document.getElementById("fixbox").innerHTML = `<div class="revision-box"><b>Revision drafter's proposal</b>
        <ul>${r.changes.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>
        <h4>Revised memo</h4><div class="memo-view">${esc(r.revised_memo)}</div>
        <div class="recheck">Sent back through the gate: <b class="stamp-mini ${CHIP[r.recheck.decision]}">${GLYPH[r.recheck.decision]} ${esc(LABEL[r.recheck.decision])}</b></div>
        <p class="body">${esc(r.recheck.reason)}</p></div>`;
    };
  }

  fetch("assets/demo-data.json").then((r) => r.json()).then((data) => {
    const memos = data.memos;
    docket.innerHTML = memos.map((m) => `<button type="button" role="option" aria-selected="false" data-id="${esc(m.id)}">
      <span class="id">${esc(m.id)}</span><span class="snip">${esc(m.text)}</span>
      <span class="res"><b class="st ${CHIP[m.decision]}">${esc(LABEL[m.decision])}</b></span></button>`).join("");
    docket.querySelectorAll("button").forEach((b) => (b.onclick = () => {
      show(memos.find((m) => m.id === b.dataset.id), memos);
      if (matchMedia("(max-width: 900px)").matches) panel.scrollIntoView({ block: "start" });  // the panel is below the list on phones
    }));
    show(memos.find((m) => m.id === "pl-07") || memos[0], memos);
  }).catch(() => { docket.innerHTML = `<p class="muted">The recorded decisions could not be loaded.</p>`; });
})();

/* Hosted sign-up. Set APP_URL to the live service (e.g. https://para-memo-gate.onrender.com) once it is deployed;
   until then the pricing buttons lead to the free download and say so, rather than to a dead link. */
const APP_URL = "";
(function appLinks() {
  const links = document.querySelectorAll("[data-app-path]");
  if (APP_URL) {
    links.forEach((a) => { a.href = APP_URL.replace(/\/$/, "") + a.dataset.appPath; });
    return;
  }
  const note = document.getElementById("pricing-note");
  if (note) note.insertAdjacentHTML("afterbegin", "<b>Hosted sign-up opens soon.</b> Until then, the buttons take you to the free download. ");
})();
