/* Torn Script Registry — landing page controller */

const REGISTRY_URL = "../data/registry.json";
const CONFIG_URL = "/api/site-config";

(async function boot() {
  try {
    const [registry, cfg] = await Promise.all([
      fetch(REGISTRY_URL).then((r) => {
        if (!r.ok) throw new Error("registry HTTP " + r.status);
        return r.json();
      }),
      fetch(CONFIG_URL)
        .then((r) => (r.ok ? r.json() : { featured: [], hero: {} }))
        .catch(() => ({ featured: [], hero: {} })),
    ]);
    renderHero(cfg.hero || {}, registry);
    renderFeatured(cfg.featured || []);
    renderStats(registry);
  } catch (err) {
    document.getElementById("featuredGrid").innerHTML =
      `<div class="loading">Failed to load: <code>${err.message}</code></div>`;
  }
})();

function renderHero(hero, registry) {
  if (hero && hero.tagline) {
    document.getElementById("heroTagline").textContent = hero.tagline;
  }
  if (hero && hero.headline) {
    document.getElementById("heroHeadline").textContent = hero.headline;
  }
  // Quick "what's inside" sub-stats
  const offered = registry.filter((r) => r.intent === "offering").length;
  document.getElementById("heroSubCount").textContent =
    `${offered.toLocaleString()} scripts catalogued`;
}

function renderStats(registry) {
  const offered = registry.filter((r) => r.intent === "offering");
  const ok = offered.filter((r) => (r.manual_status || (r.health && r.health.status)) === "ok").length;
  const tools = new Set(offered.map((r) => r.tool_type || "unknown"));
  const authors = new Set(offered.map((r) => r.author && r.author.username).filter(Boolean));
  const set = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
  };
  set("statTotal", offered.length.toLocaleString());
  set("statWorking", ok.toLocaleString());
  set("statTools", tools.size.toLocaleString());
  set("statAuthors", authors.size.toLocaleString());
}

function renderFeatured(featured) {
  const grid = document.getElementById("featuredGrid");
  const sec = document.getElementById("featuredSection");
  if (!featured || featured.length === 0) {
    sec.classList.add("empty");
    grid.innerHTML =
      `<div class="featured-empty">
         <p>No scripts have been featured yet.</p>
         <p class="muted">Highlight community favourites here by visiting the admin console.</p>
       </div>`;
    return;
  }
  sec.classList.remove("empty");
  grid.innerHTML = featured.map(featuredCard).join("");
}

function featuredCard(r) {
  const status = resolveStatus(r);
  const author = (r.author && r.author.username) || "Unknown";
  const authorUrl = r.author && r.author.id
    ? `https://www.torn.com/profiles.php?XID=${r.author.id}`
    : null;
  const primary = r.primary_url;
  const tool = r.tool_type || "tool";
  const tag = (r.tags && r.tags[0]) || tool;

  const installs = installCount(r);
  const installLabel = installs != null
    ? `${fmtNum(installs)} installs`
    : `+${r.rating || 0} rating`;

  const installBtn = primary
    ? `<a class="feat-install" href="${esc(primary.url)}" target="_blank" rel="noopener">Install ↗</a>`
    : `<a class="feat-install ghost" href="${esc(r.thread_url)}" target="_blank" rel="noopener">View thread ↗</a>`;

  return `
    <article class="feat-card s-${status}">
      <div class="feat-ribbon">★ Featured</div>
      <div class="feat-tag">${esc(tag)}</div>
      <h3 class="feat-title">
        <a href="${esc(r.thread_url)}" target="_blank" rel="noopener">${esc(r.title)}</a>
      </h3>
      <div class="feat-author">
        by ${authorUrl
          ? `<a href="${esc(authorUrl)}" target="_blank" rel="noopener">${esc(author)}</a>`
          : esc(author)}
        <span class="dot">·</span>
        <span class="feat-tool">${esc(tool)}</span>
      </div>
      <div class="feat-meta">
        <span class="feat-stat"><b>${installLabel}</b></span>
        <span class="feat-stat dim">${fmtNum(r.views || 0)} views</span>
        <span class="feat-stat dim">${fmtNum(r.posts || 0)} posts</span>
      </div>
      <div class="feat-actions">
        ${installBtn}
        <a class="feat-thread" href="${esc(r.thread_url)}" target="_blank" rel="noopener">Thread</a>
      </div>
    </article>
  `;
}

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

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
