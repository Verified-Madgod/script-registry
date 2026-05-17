"""Build the initial Script Registry seed catalog.

Reads:
- ../../scripts_thread_ids.json  (thread metadata: id, title, author, dates, ...)
- ../../scripts/*.txt             (thread bodies, filename = "Title [id].txt")

Writes:
- data/registry.json              (array of registry records)
- data/registry.csv               (flat CSV for quick inspection)

A "registry record" is the seed-stage entity: identity + extracted/classified
URLs. Health checks, screenshots, and tags come later.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).parent
ROOT = HERE.parent.parent  # workspace root
SCRIPTS_DIR = ROOT / "scripts"
THREAD_META_FILE = ROOT / "scripts_thread_ids.json"
DATA_DIR = HERE / "data"
OUT_JSON = DATA_DIR / "registry.json"
OUT_CSV = DATA_DIR / "registry.csv"

# --- URL extraction ----------------------------------------------------------

# A reasonably permissive URL matcher. We trim trailing punctuation afterwards.
URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+", re.IGNORECASE)
# Trailing chars that are almost never part of a real URL in prose.
_TRAILING_TRIM = ".,;:!?)]}>\"'"


def extract_urls(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for raw in URL_RE.findall(text):
        u = raw
        while u and u[-1] in _TRAILING_TRIM:
            u = u[:-1]
        # Common: trailing ")" from "(see https://x.com/y)" already stripped;
        # but URLs can legitimately end with ")" (wikipedia-style). Accept loss.
        if len(u) < 10:
            continue
        seen.setdefault(u, None)
    return list(seen.keys())


# --- URL classification ------------------------------------------------------

# Host suffix -> (kind, label). Order matters: longer/more specific first.
HOST_KINDS: list[tuple[str, str, str]] = [
    # userscript install / source
    ("greasyfork.org", "install", "Greasy Fork"),
    ("sleazyfork.org", "install", "Sleazy Fork"),
    ("openuserjs.org", "install", "OpenUserJS"),
    # source / repo
    ("gist.github.com", "source", "GitHub Gist"),
    ("raw.githubusercontent.com", "source", "GitHub (raw)"),
    ("github.com", "source", "GitHub"),
    ("gitlab.com", "source", "GitLab"),
    ("bitbucket.org", "source", "Bitbucket"),
    ("codeberg.org", "source", "Codeberg"),
    # spreadsheets / docs
    ("docs.google.com", "spreadsheet", "Google Docs/Sheets"),
    ("sheets.google.com", "spreadsheet", "Google Sheets"),
    ("drive.google.com", "spreadsheet", "Google Drive"),
    # browser extension stores
    ("chromewebstore.google.com", "extension", "Chrome Web Store"),
    ("chrome.google.com", "extension", "Chrome Web Store"),
    ("addons.mozilla.org", "extension", "Firefox Add-ons"),
    ("microsoftedge.microsoft.com", "extension", "Edge Add-ons"),
    ("addons.opera.com", "extension", "Opera Add-ons"),
    # discord
    ("discord.gg", "discord", "Discord invite"),
    ("discord.com", "discord", "Discord"),
    ("discordapp.com", "discord", "Discord"),
    # media / screenshots
    ("imgur.com", "media", "Imgur"),
    ("i.imgur.com", "media", "Imgur"),
    ("prnt.sc", "media", "Lightshot"),
    ("gyazo.com", "media", "Gyazo"),
    ("youtube.com", "media", "YouTube"),
    ("youtu.be", "media", "YouTube"),
    ("streamable.com", "media", "Streamable"),
    # torn self-references (kept but flagged separately)
    ("torn.com", "torn", "Torn"),
    ("www.torn.com", "torn", "Torn"),
    # PDA app
    ("tornpda.com", "pda", "Torn PDA"),
    ("apps.apple.com", "appstore", "App Store"),
    ("play.google.com", "appstore", "Play Store"),
]


def classify_url(url: str) -> tuple[str, str, str]:
    """Return (kind, host_label, host). Falls back to ('website', host, host)."""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        host = ""
    host = host.lower()
    for suffix, kind, label in HOST_KINDS:
        if host == suffix or host.endswith("." + suffix):
            return kind, label, host
    return "website", host or "(unknown)", host


# Kinds we treat as "the actual thing being distributed". When picking the
# best primary URL for the registry card we prefer earlier entries.
PRIMARY_PREFERENCE = [
    "install",       # Greasy Fork install page beats raw repo
    "extension",     # Web store install
    "spreadsheet",   # Google Sheets template
    "source",        # GitHub repo / gist
    "pda",
    "appstore",
    "website",
    "discord",
    "media",
    "torn",
]


def pick_primary(urls: list[dict]) -> dict | None:
    if not urls:
        return None
    by_kind: dict[str, dict] = {}
    for u in urls:
        by_kind.setdefault(u["kind"], u)
    for kind in PRIMARY_PREFERENCE:
        if kind in by_kind:
            return by_kind[kind]
    return urls[0]


def pick_secondary(urls: list[dict], primary: dict | None) -> dict | None:
    """Return the next-best URL after `primary`.

    Preference order:
      1. A URL of a DIFFERENT kind than primary (e.g. install + source).
      2. Failing that, the next URL of any kind (e.g. two Greasy Fork links).

    Returns None only when there is no second URL at all.
    """
    if not urls or primary is None:
        return None
    primary_url = primary.get("url")
    primary_kind = primary.get("kind")

    # 1) Prefer a different-kind URL, walked in preference order.
    diff_by_kind: dict[str, dict] = {}
    for u in urls:
        if u["kind"] == primary_kind:
            continue
        diff_by_kind.setdefault(u["kind"], u)
    for kind in PRIMARY_PREFERENCE:
        if kind in diff_by_kind:
            return diff_by_kind[kind]

    # 2) Fallback: any other URL (same kind is fine), skipping the primary one.
    for u in urls:
        if u.get("url") != primary_url:
            return u
    return None


# --- Helpers -----------------------------------------------------------------

def iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_body_file(thread_id: int) -> Path | None:
    # Filename pattern from the scraper: "Title [id].txt"
    hits = list(SCRIPTS_DIR.glob(f"*[[]{thread_id}[]].txt"))
    return hits[0] if hits else None


def read_body(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


# --- Build -------------------------------------------------------------------

def build() -> list[dict]:
    threads = json.loads(THREAD_META_FILE.read_text(encoding="utf-8"))
    print(f"Loaded {len(threads)} thread metadata records")

    records: list[dict] = []
    missing_body = 0
    url_kind_counts: Counter[str] = Counter()
    host_counts: Counter[str] = Counter()

    for t in threads:
        tid = t["id"]
        body_path = find_body_file(tid)
        body = read_body(body_path) if body_path else ""
        if not body_path:
            missing_body += 1

        raw_urls = extract_urls(body)
        url_records = []
        for u in raw_urls:
            kind, label, host = classify_url(u)
            url_kind_counts[kind] += 1
            host_counts[host] += 1
            url_records.append({
                "url": u,
                "kind": kind,
                "host_label": label,
                "host": host,
            })

        primary = pick_primary(url_records)
        secondary = pick_secondary(url_records, primary)

        author = t.get("author") or {}
        record = {
            "thread_id": tid,
            "title": t.get("title", "").strip(),
            "author": {
                "id": author.get("id"),
                "username": author.get("username"),
                "karma": author.get("karma"),
            },
            "forum_id": t.get("forum_id"),
            "first_post_time": t.get("first_post_time"),
            "last_post_time": t.get("last_post_time"),
            "first_post_iso": iso(t.get("first_post_time")),
            "last_post_iso": iso(t.get("last_post_time")),
            "posts": t.get("posts"),
            "views": t.get("views"),
            "rating": t.get("rating"),
            "is_locked": t.get("is_locked"),
            "is_sticky": t.get("is_sticky"),
            "thread_url": f"https://www.torn.com/forums.php#/p=threads&f={t.get('forum_id')}&t={tid}",
            "body_file": body_path.name if body_path else None,
            "body_chars": len(body),
            "url_count": len(url_records),
            "urls": url_records,
            "primary_url": primary,
            "secondary_url": secondary,
        }
        records.append(record)

    print(f"Threads without body file: {missing_body}")
    print(f"Total URLs extracted: {sum(url_kind_counts.values())}")
    print("By kind:")
    for kind, n in url_kind_counts.most_common():
        print(f"  {kind:12s} {n}")
    print("Top 20 hosts:")
    for host, n in host_counts.most_common(20):
        print(f"  {n:5d}  {host}")

    # Coverage stats
    with_install = sum(
        1 for r in records
        if any(u["kind"] in ("install", "extension") for u in r["urls"])
    )
    with_source = sum(
        1 for r in records if any(u["kind"] == "source" for u in r["urls"])
    )
    with_any_distribution = sum(
        1 for r in records
        if any(u["kind"] in ("install", "extension", "spreadsheet", "source",
                              "pda", "appstore") for u in r["urls"])
    )
    with_no_url = sum(1 for r in records if not r["urls"])
    print()
    print(f"Threads with install/extension URL: {with_install}")
    print(f"Threads with source URL:            {with_source}")
    print(f"Threads with any distribution URL:  {with_any_distribution}")
    print(f"Threads with NO URLs at all:        {with_no_url}")

    return records


def write_outputs(records: list[dict]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\nWrote {OUT_JSON.relative_to(ROOT)} ({len(records)} records)")

    # Flat CSV: one row per record, with the primary URL fields denormalized.
    fieldnames = [
        "thread_id", "title", "author_username", "author_id",
        "first_post_iso", "last_post_iso", "posts", "views", "rating",
        "is_locked", "is_sticky",
        "url_count",
        "primary_kind", "primary_host", "primary_url",
        "secondary_kind", "secondary_host", "secondary_url",
        "thread_url",
    ]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in records:
            p = r.get("primary_url") or {}
            s = r.get("secondary_url") or {}
            w.writerow({
                "thread_id": r["thread_id"],
                "title": r["title"],
                "author_username": (r["author"] or {}).get("username"),
                "author_id": (r["author"] or {}).get("id"),
                "first_post_iso": r["first_post_iso"],
                "last_post_iso": r["last_post_iso"],
                "posts": r["posts"],
                "views": r["views"],
                "rating": r["rating"],
                "is_locked": r["is_locked"],
                "is_sticky": r["is_sticky"],
                "url_count": r["url_count"],
                "primary_kind": p.get("kind"),
                "primary_host": p.get("host"),
                "primary_url": p.get("url"),
                "secondary_kind": s.get("kind"),
                "secondary_host": s.get("host"),
                "secondary_url": s.get("url"),
                "thread_url": r["thread_url"],
            })
    print(f"Wrote {OUT_CSV.relative_to(ROOT)}")


if __name__ == "__main__":
    records = build()
    write_outputs(records)
