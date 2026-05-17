// Top Authors page — aggregates registry by author and renders a leaderboard.
(function () {
  const state = {
    authors: [],   // [{username, id, scripts, working, broken, stale, installs, latest, topScript}]
    sortBy: "installs",
    query: "",
  };

  async function boot() {
    try {
      const res = await fetch("../data/registry.json");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const registry = await res.json();
      state.authors = aggregate(registry);
      renderHeader();
      bindControls();
      render();
    } catch (e) {
      document.getElementById("authorsTableWrap").innerHTML =
        `<div class="featured-empty">Failed to load registry. ${esc(e.message || e)}</div>`;
    }
  }

  // ---------- Aggregation ----------
  function aggregate(registry) {
    const offered = registry.filter((r) => (r.intent || "offering") === "offering");
    const map = new Map();
    for (const r of offered) {
      const username = (r.author && r.author.username) || "Unknown";
      const id = (r.author && r.author.id) || null;
      const key = id ? `id:${id}` : `name:${username.toLowerCase()}`;

      let a = map.get(key);
      if (!a) {
        a = {
          key,
          username,
          id,
          scripts: 0,
          working: 0,
          stale: 0,
          broken: 0,
          urlLikely: 0,
          installs: 0,
          latest: 0,
          topScript: null,
          topScriptInstalls: -1,
        };
        map.set(key, a);
      }
      a.scripts += 1;

      const status = resolveStatus(r);
      if (status === "ok") a.working += 1;
      else if (status === "stale") a.stale += 1;
      else if (status === "broken") a.broken += 1;
      else if (status === "url-likely") a.urlLikely += 1;

      const installs = installCount(r) || 0;
      a.installs += installs;

      const ts = r.last_post_at ? Date.parse(r.last_post_at) : 0;
      if (ts && ts > a.latest) a.latest = ts;

      if (installs > a.topScriptInstalls) {
        a.topScriptInstalls = installs;
        a.topScript = r;
      }
    }

    return Array.from(map.values());
  }

  // ---------- Render ----------
  function renderHeader() {
    const totalAuthors = state.authors.length;
    const totalScripts = state.authors.reduce((s, a) => s + a.scripts, 0);
    const totalInstalls = state.authors.reduce((s, a) => s + a.installs, 0);
    setText("authCount", `${fmtNum(totalAuthors)} authors`);
    setText("authScripts", `${fmtNum(totalScripts)} scripts`);
    setText("authInstalls", `${fmtNum(totalInstalls)} installs`);
  }

  function bindControls() {
    const sortEl = document.getElementById("sortBy");
    const qEl = document.getElementById("authQ");
    sortEl.addEventListener("change", () => { state.sortBy = sortEl.value; render(); });
    qEl.addEventListener("input", () => { state.query = qEl.value.trim().toLowerCase(); render(); });
  }

  function render() {
    const sorted = [...state.authors]
      .filter((a) => !state.query || a.username.toLowerCase().includes(state.query))
      .sort(sorter(state.sortBy));

    if (!sorted.length) {
      document.getElementById("authorsTableWrap").innerHTML =
        `<div class="featured-empty">No authors match “${esc(state.query)}”.</div>`;
      return;
    }

    const rows = sorted.map((a, i) => row(a, i + 1)).join("");
    document.getElementById("authorsTableWrap").innerHTML = `
      <table class="authors-table">
        <thead>
          <tr>
            <th class="col-rank">#</th>
            <th class="col-author">Author</th>
            <th class="col-num">Scripts</th>
            <th class="col-num">Working</th>
            <th class="col-num">Installs</th>
            <th class="col-top">Top script</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  function row(a, rank) {
    const profile = a.id ? `https://www.torn.com/profiles.php?XID=${a.id}` : null;
    const nameCell = profile
      ? `<a href="${esc(profile)}" target="_blank" rel="noopener">${esc(a.username)}</a>`
      : esc(a.username);

    const rankCls = rank === 1 ? "rk gold" : rank === 2 ? "rk silver" : rank === 3 ? "rk bronze" : "rk";

    const breakdown = [];
    if (a.working) breakdown.push(`<span class="pill ok">${a.working} ok</span>`);
    if (a.stale) breakdown.push(`<span class="pill stale">${a.stale} stale</span>`);
    if (a.broken) breakdown.push(`<span class="pill broken">${a.broken} broken</span>`);
    if (a.urlLikely) breakdown.push(`<span class="pill url-likely">${a.urlLikely} link?</span>`);

    const top = a.topScript;
    const topCell = top
      ? `<a href="${esc(top.thread_url)}" target="_blank" rel="noopener" class="auth-top-link">${esc(top.title || "Untitled")}</a>`
      : `<span class="dim">—</span>`;

    return `
      <tr>
        <td class="col-rank"><span class="${rankCls}">${rank}</span></td>
        <td class="col-author">
          <div class="auth-name">${nameCell}</div>
          <div class="auth-pills">${breakdown.join("")}</div>
        </td>
        <td class="col-num">${fmtNum(a.scripts)}</td>
        <td class="col-num accent">${fmtNum(a.working)}</td>
        <td class="col-num">${fmtNum(a.installs)}</td>
        <td class="col-top">${topCell}</td>
      </tr>`;
  }

  function sorter(key) {
    switch (key) {
      case "working":  return (a, b) => b.working - a.working || b.installs - a.installs;
      case "scripts":  return (a, b) => b.scripts - a.scripts || b.installs - a.installs;
      case "name":     return (a, b) => a.username.localeCompare(b.username);
      case "installs":
      default:         return (a, b) => b.installs - a.installs || b.working - a.working;
    }
  }

  // ---------- Shared helpers (mirror landing.js) ----------
  function resolveStatus(r) {
    if (r.manual_status) return r.manual_status;
    const base = (r.health && r.health.status) || (r.primary_url ? "unknown" : "no-url");
    if (base === "no-url" && r.body_mentions_url) return "url-likely";
    return base;
  }

  function installCount(r) {
    const checks = (r.health && r.health.url_checks) || [];
    const primary = r.primary_url;
    if (!primary) return null;
    const c = checks.find((x) => x.url === primary.url);
    const md = c && c.metadata;
    if (md && md.source === "greasyfork") return md.total_installs;
    if (md && md.source === "github") return md.stars;
    return null;
  }

  function fmtNum(n) {
    if (n == null) return "—";
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
    if (n >= 1_000) return (n / 1_000).toFixed(n >= 10_000 ? 0 : 1) + "k";
    return String(n);
  }

  function setText(id, txt) {
    const el = document.getElementById(id);
    if (el) el.textContent = txt;
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  boot();
})();
