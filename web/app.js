/* Torn Script Registry — front-end controller (vanilla JS) */

const REGISTRY_URL = "../data/registry.json";
const PAGE_SIZE = 60;

const STATUS_ORDER = ["ok", "stale", "broken", "unknown", "url-likely", "no-url"];
const STATUS_LABEL = {
  ok: "Working",
  stale: "Stale (1y+)",
  broken: "Broken",
  unknown: "Unknown",
  "url-likely": "URL likely (in post)",
  "no-url": "No install URL",
};

const state = {
  all: [],
  filtered: [],
  q: "",
  status: new Set(),
  category: new Set(),
  tool: new Set(),
  sort: "last_updated",
  page: 1,
};

// ----------- bootstrap -----------
fetch(REGISTRY_URL)
  .then((r) => {
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  })
  .then((data) => {
    // Hard filter: a script registry only contains actual scripts being offered.
    // Requests, services, and discussion threads are excluded entirely.
    state.all = data.map(normalize).filter((r) => r._intent === "offering");
    initFacets();
    bindUI();
    applyFilters();
    populateLastSweep();
  })
  .catch((err) => {
    document.getElementById("grid").innerHTML =
      `<div class="loading">Failed to load <code>${REGISTRY_URL}</code><br><br>` +
      `<small style="color:#888">${err}<br><br>Serve this folder with <code>python -m http.server</code> from <code>projects/script-registry/</code>.</small></div>`;
  });

// ----------- normalize -----------
function normalize(r) {
  const h = r.health || {};
  const primaryCheck = (h.url_checks || []).find(
    (c) => r.primary_url && c.url === r.primary_url.url
  );
  const md = (primaryCheck && primaryCheck.metadata) || null;

  let status = r.manual_status || h.status || (r.primary_url ? "unknown" : "no-url");
  if (!r.manual_status && status === "no-url" && r.body_mentions_url) status = "url-likely";
  const installs = md && md.source === "greasyfork" ? md.total_installs : null;
  const stars = md && md.source === "github" ? md.stars : null;
  const version = md && md.source === "greasyfork" ? md.version : null;
  const dailyInstalls = md && md.source === "greasyfork" ? md.daily_installs : null;
  const goodR = md && md.source === "greasyfork" ? md.good_ratings : null;
  const badR  = md && md.source === "greasyfork" ? md.bad_ratings : null;

  const lastUpdated = h.last_updated || r.last_post_iso || null;

  return {
    ...r,
    _status: status,
    _category: (r.tags && r.tags[0]) || null,
    _tags: r.tags || [],
    _tool: r.tool_type || "unknown",
    _intent: r.intent || "discussion",
    _installs: installs,
    _stars: stars,
    _version: version,
    _dailyInstalls: dailyInstalls,
    _goodR: goodR,
    _badR: badR,
    _lastUpdated: lastUpdated,
    _lastUpdatedTs: lastUpdated ? Date.parse(lastUpdated) : 0,
    _firstPostTs: r.first_post_iso ? Date.parse(r.first_post_iso) : 0,
    _searchBlob: [
      r.title,
      r.author && r.author.username,
      (r.tags || []).join(" "),
      r.tool_type,
      r.primary_url && r.primary_url.host_label,
    ].filter(Boolean).join(" ").toLowerCase(),
  };
}

// ----------- facets -----------
function initFacets() {
  // Status (fixed order)
  const statusCounts = countBy(state.all, "_status");
  const statusBox = document.getElementById("filterStatus");
  statusBox.innerHTML = STATUS_ORDER.map((s) => {
    const c = statusCounts[s] || 0;
    return `<div class="statusrow" data-status="${s}">
      <span class="sw" style="background:${statusColor(s)};box-shadow:0 0 6px ${statusColor(s)}"></span>
      <span class="lbl">${STATUS_LABEL[s]}</span>
      <span class="cnt">${c}</span>
    </div>`;
  }).join("");

  // Category chips (top tags)
  buildChips("filterCategory", "_category", "category");
  // Tool chips
  buildChips("filterTool", "_tool", "tool");

  // Rollup counts
  document.getElementById("cntOk").textContent = statusCounts.ok || 0;
  document.getElementById("cntStale").textContent = statusCounts.stale || 0;
  document.getElementById("cntBroken").textContent = statusCounts.broken || 0;
  document.getElementById("cntUnknown").textContent = statusCounts.unknown || 0;
}

function buildChips(elId, field, kind) {
  const counts = countBy(state.all, field);
  const entries = Object.entries(counts)
    .filter(([k]) => k && k !== "null")
    .sort((a, b) => b[1] - a[1]);
  const el = document.getElementById(elId);
  el.innerHTML = entries
    .map(
      ([k, c]) =>
        `<span class="chip" data-kind="${kind}" data-val="${escapeAttr(k)}">${escapeHtml(k)}<span class="cnt">${c}</span></span>`
    )
    .join("");
}

function countBy(arr, key) {
  const m = {};
  for (const r of arr) {
    const v = r[key];
    if (v == null) continue;
    m[v] = (m[v] || 0) + 1;
  }
  return m;
}

// ----------- bind -----------
function bindUI() {
  const q = document.getElementById("q");
  q.addEventListener("input", () => {
    state.q = q.value.trim().toLowerCase();
    state.page = 1;
    applyFilters();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement !== q) {
      e.preventDefault();
      q.focus();
    }
    if (e.key === "Escape" && document.activeElement === q) {
      q.value = ""; state.q = ""; applyFilters();
    }
  });

  document.getElementById("filterStatus").addEventListener("click", (e) => {
    const row = e.target.closest(".statusrow");
    if (!row) return;
    toggleSet(state.status, row.dataset.status);
    row.classList.toggle("active");
    state.page = 1;
    applyFilters();
  });

  document.querySelectorAll(".chips").forEach((box) => {
    box.addEventListener("click", (e) => {
      const chip = e.target.closest(".chip");
      if (!chip) return;
      const kind = chip.dataset.kind;
      const val = chip.dataset.val;
      const set =
        kind === "category" ? state.category :
        kind === "tool" ? state.tool : null;
      if (!set) return;
      toggleSet(set, val);
      chip.classList.toggle("active");
      state.page = 1;
      applyFilters();
    });
  });

  document.getElementById("sortBy").addEventListener("change", (e) => {
    state.sort = e.target.value;
    applyFilters();
  });

  document.getElementById("resetBtn").addEventListener("click", () => {
    state.q = ""; document.getElementById("q").value = "";
    state.status.clear(); state.category.clear(); state.tool.clear();
    document.querySelectorAll(".chip.active, .statusrow.active").forEach((el) => el.classList.remove("active"));
    state.page = 1;
    applyFilters();
  });

  document.getElementById("grid").addEventListener("click", (e) => {
    const more = e.target.closest("[data-action='detail']");
    if (more) {
      const card = more.closest(".card");
      card.classList.toggle("open");
    }
  });
}

function toggleSet(set, val) {
  if (set.has(val)) set.delete(val);
  else set.add(val);
}

// ----------- filter / sort -----------
function applyFilters() {
  const q = state.q;
  let out = state.all.filter((r) => {
    if (state.status.size && !state.status.has(r._status)) return false;
    if (state.category.size && !state.category.has(r._category)) return false;
    if (state.tool.size && !state.tool.has(r._tool)) return false;
    if (q && !r._searchBlob.includes(q)) return false;
    return true;
  });

  out.sort(sorter(state.sort));
  state.filtered = out;
  render();
}

function sorter(key) {
  switch (key) {
    case "installs": return (a, b) => (b._installs || 0) - (a._installs || 0);
    case "rating": return (a, b) => (b.rating || 0) - (a.rating || 0);
    case "views": return (a, b) => (b.views || 0) - (a.views || 0);
    case "posts": return (a, b) => (b.posts || 0) - (a.posts || 0);
    case "first_post": return (a, b) => b._firstPostTs - a._firstPostTs;
    case "alpha": return (a, b) => a.title.localeCompare(b.title);
    case "last_updated":
    default: return (a, b) => b._lastUpdatedTs - a._lastUpdatedTs;
  }
}

// ----------- render -----------
function render() {
  const grid = document.getElementById("grid");
  const total = state.filtered.length;
  document.getElementById("resultCount").textContent =
    total === state.all.length ? `${total} scripts` : `${total} of ${state.all.length}`;

  const start = (state.page - 1) * PAGE_SIZE;
  const slice = state.filtered.slice(start, start + PAGE_SIZE);

  document.getElementById("resultMeta").textContent =
    total === 0 ? "" : `showing ${start + 1}–${start + slice.length}`;

  if (slice.length === 0) {
    grid.innerHTML = `<div class="loading">No scripts match your filters.</div>`;
  } else {
    grid.innerHTML = slice.map(cardHtml).join("");
  }
  renderPager(total);
}

function cardHtml(r) {
  const st = r._status;
  const stBadge = `<span class="badge ${st}">${st === "no-url" ? "no url" : st === "url-likely" ? "url likely" : st}</span>`;
  const primary = r.primary_url;
  const secondary = r.secondary_url;
  const author = r.author || {};
  const authorUrl = author.id ? `https://www.torn.com/profiles.php?XID=${author.id}` : null;

  const tags = (r._tags || []).map(
    (t, i) => `<span class="tag${i === 0 ? " primary" : ""}">${escapeHtml(t)}</span>`
  ).join("");

  // Stats: 3 cells. Choose contextually.
  const statCells = [];
  if (r._installs != null) {
    statCells.push(statCell("Installs", fmtNum(r._installs), barWidth(r._installs, 5000)));
  } else if (r._stars != null) {
    statCells.push(statCell("GitHub ★", fmtNum(r._stars), barWidth(r._stars, 200)));
  } else {
    statCells.push(statCell("Forum Rating", `+${r.rating || 0}`, null, true));
  }
  if (r._version) {
    statCells.push(statCell("Version", "v" + r._version));
  } else {
    statCells.push(statCell("Posts", fmtNum(r.posts || 0)));
  }
  statCells.push(statCell("Updated", fmtAgo(r._lastUpdatedTs)));

  // Actions
  const acts = [];
  if (primary) {
    const label = primary.kind === "install" ? "Install"
                : primary.kind === "source" ? "Source"
                : primary.kind === "spreadsheet" ? "Open Sheet"
                : primary.kind === "extension" ? "Extension"
                : primary.kind === "appstore" ? "App Store"
                : primary.kind === "pda" ? "Open in PDA"
                : "Open";
    acts.push(`<a class="act primary" href="${escapeAttr(primary.url)}" target="_blank" rel="noopener">${label} ↗</a>`);
  } else {
    acts.push(`<button class="act disabled" disabled>No URL</button>`);
  }
  if (secondary) {
    acts.push(`<a class="act" href="${escapeAttr(secondary.url)}" target="_blank" rel="noopener">${escapeHtml(secondary.host_label || secondary.kind)} ↗</a>`);
  }
  acts.push(`<a class="act" href="${escapeAttr(r.thread_url)}" target="_blank" rel="noopener">Thread ↗</a>`);
  acts.push(`<button class="act" data-action="detail">Details</button>`);

  return `
    <article class="card s-${st}">
      <div class="card-head">
        <h3 class="card-title"><a href="${escapeAttr(r.thread_url)}" target="_blank" rel="noopener">${escapeHtml(r.title)}</a></h3>
        ${stBadge}
      </div>
      <div class="card-meta">
        <span>by ${authorUrl
          ? `<a class="author-link" href="${escapeAttr(authorUrl)}" target="_blank" rel="noopener"><b>${escapeHtml(author.username || "?")}</b></a>`
          : `<b>${escapeHtml(author.username || "?")}</b>`}</span>
        <span class="sep">·</span>
        <span><b>${escapeHtml(r._tool)}</b></span>
        ${primary ? `<span class="sep">·</span><span>${escapeHtml(primary.host_label)}</span>` : ""}
        <span class="sep">·</span>
        <span>${fmtNum(r.views || 0)} views</span>
      </div>
      ${tags ? `<div class="card-tags">${tags}</div>` : ""}
      <div class="stats">${statCells.join("")}</div>
      <div class="card-actions">${acts.join("")}</div>
      <div class="card-detail">${detailHtml(r)}</div>
    </article>
  `;
}

function statCell(k, v, barPct, dim) {
  const bar = barPct != null ? `<div class="bar"><span style="width:${barPct}%"></span></div>` : "";
  return `<div class="stat"><span class="k">${k}</span><span class="v${dim ? " dim" : ""}">${v}</span>${bar}</div>`;
}

function barWidth(val, max) {
  if (!val) return 0;
  return Math.min(100, Math.round((val / max) * 100));
}

function detailHtml(r) {
  const rows = [];
  rows.push(["Thread ID", r.thread_id]);
  rows.push(["Posts / Views", `${r.posts} / ${r.views}`]);
  rows.push(["Forum Rating", `+${r.rating || 0}`]);
  rows.push(["Intent", r._intent]);
  if (r._tags && r._tags.length > 1) rows.push(["All tags", r._tags.join(", ")]);
  if (r.first_post_iso) rows.push(["First post", r.first_post_iso.replace("T", " ").replace("Z", " UTC")]);
  if (r._lastUpdated) rows.push(["Last updated", r._lastUpdated.replace("T", " ").replace(/\..+Z$/, " UTC").replace("Z", " UTC")]);
  if (r._installs != null) rows.push(["Greasy Fork installs", `${fmtNum(r._installs)} (${r._dailyInstalls || 0}/day)`]);
  if (r._goodR != null && (r._goodR || r._badR)) rows.push(["GF ratings", `👍 ${r._goodR} / 👎 ${r._badR}`]);
  if (r._stars != null) rows.push(["GitHub stars", r._stars]);
  for (const u of r.urls || []) {
    rows.push([`Link (${u.kind})`, `<a href="${escapeAttr(u.url)}" target="_blank" rel="noopener">${escapeHtml(u.url)}</a>`]);
  }
  if (r.health && r.health.checked_at) {
    rows.push(["Health checked", r.health.checked_at.replace("T", " ").replace("Z", " UTC")]);
  }
  return `<dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>`;
}

// ----------- pager -----------
function renderPager(total) {
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const el = document.getElementById("pager");
  if (pages <= 1) { el.innerHTML = ""; return; }
  el.innerHTML = `
    <button id="pgPrev" ${state.page === 1 ? "disabled" : ""}>‹ Prev</button>
    <span>Page ${state.page} / ${pages}</span>
    <button id="pgNext" ${state.page === pages ? "disabled" : ""}>Next ›</button>
  `;
  document.getElementById("pgPrev").onclick = () => { state.page--; render(); window.scrollTo({top:0,behavior:"smooth"}); };
  document.getElementById("pgNext").onclick = () => { state.page++; render(); window.scrollTo({top:0,behavior:"smooth"}); };
}

// ----------- helpers -----------
function statusColor(s) {
  return ({
    ok: "#7fbe2c", stale: "#e2a824", broken: "#d24a3c",
    unknown: "#6e6e6e", "url-likely": "#c08a2a", "no-url": "#4a4a4a",
  })[s] || "#6e6e6e";
}

function fmtNum(n) {
  if (n == null) return "—";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(n >= 10_000 ? 0 : 1) + "k";
  return String(n);
}

function fmtAgo(ts) {
  if (!ts) return "—";
  const now = Date.now();
  const diff = Math.max(0, now - ts);
  const day = 86400_000;
  if (diff < day) return "today";
  if (diff < 2 * day) return "1d ago";
  if (diff < 30 * day) return Math.floor(diff / day) + "d ago";
  if (diff < 365 * day) return Math.floor(diff / (30 * day)) + "mo ago";
  return Math.floor(diff / (365 * day)) + "y ago";
}

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function escapeAttr(s) { return escapeHtml(s); }

function populateLastSweep() {
  const checked = state.all
    .map((r) => r.health && r.health.checked_at)
    .filter(Boolean)
    .sort()
    .pop();
  document.getElementById("lastSweep").textContent =
    checked ? checked.replace("T", " ").replace("Z", " UTC") : "never";
}
