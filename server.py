"""Torn Script Registry — admin/API server (unauthenticated).

Run from anywhere:
    python projects/script-registry/server.py

Then open:
    http://localhost:8765/web/      — public registry
    http://localhost:8765/admin/    — admin console

API (all JSON, no auth):
    GET    /api/stats
    GET    /api/records?q=&status=&category=&tool=&sort=&limit=&offset=
    GET    /api/records/{thread_id}
    POST   /api/records                 body: {title, author_username, urls, ...}
    PATCH  /api/records/{thread_id}     body: {field: value, ...}
    DELETE /api/records/{thread_id}
    POST   /api/records/{thread_id}/health-check?force=1
    POST   /api/records/{thread_id}/retag
    POST   /api/actions/health-check-all   body: {only_kinds, force, limit, workers}
    POST   /api/actions/retag-all
    POST   /api/actions/export-csv
    POST   /api/actions/reload
    GET    /api/backups
    POST   /api/backups                                  body: {label?}
    POST   /api/backups/restore                          body: {name}
    DELETE /api/backups/{name}
    GET    /api/log?limit=50
"""

from __future__ import annotations

import json
import shutil
import sys
import re
import threading
import traceback
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
REGISTRY_PATH = DATA_DIR / "registry.json"
SITE_CONFIG_PATH = DATA_DIR / "site_config.json"
BACKUPS_DIR = DATA_DIR / "backups"
LOG_PATH = DATA_DIR / "admin_log.jsonl"
WEB_DIR = HERE / "web"
ADMIN_DIR = HERE / "admin"

# Make sibling modules importable
sys.path.insert(0, str(HERE))
import health_check  # noqa: E402
import tag_registry  # noqa: E402

BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

# ----------------------------------------------------------------------------
# Registry store (in-memory, with disk persistence + write lock)
# ----------------------------------------------------------------------------

_lock = threading.RLock()
_records: list[dict] = []
_site_config: dict = {"featured": [], "hero": {}}


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_registry() -> None:
    global _records
    with _lock:
        if REGISTRY_PATH.exists():
            _records = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        else:
            _records = []


def save_registry() -> None:
    with _lock:
        REGISTRY_PATH.write_text(
            json.dumps(_records, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def load_site_config() -> None:
    global _site_config
    with _lock:
        if SITE_CONFIG_PATH.exists():
            try:
                _site_config = json.loads(SITE_CONFIG_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                _site_config = {"featured": [], "hero": {}}
        else:
            _site_config = {"featured": [], "hero": {}}
        _site_config.setdefault("featured", [])
        _site_config.setdefault("hero", {})


def save_site_config() -> None:
    with _lock:
        SITE_CONFIG_PATH.write_text(
            json.dumps(_site_config, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def get_site_config() -> dict:
    """Return site config with featured records hydrated (lightweight subset)."""
    with _lock:
        cfg = {
            "featured_ids": list(_site_config.get("featured") or []),
            "hero": dict(_site_config.get("hero") or {}),
        }
        # Hydrate full records for featured ids (preserving order)
        by_id = {int(r.get("thread_id", 0)): r for r in _records}
        cfg["featured"] = [by_id[tid] for tid in cfg["featured_ids"] if tid in by_id]
    return cfg


def set_site_config(patch: dict) -> dict:
    """Patch site config. Accepts {featured: [thread_id, ...], hero: {...}}."""
    with _lock:
        if "featured" in patch:
            raw = patch["featured"] or []
            valid_ids = {int(r.get("thread_id", 0)) for r in _records}
            seen = set()
            cleaned = []
            for tid in raw:
                try:
                    t = int(tid)
                except (TypeError, ValueError):
                    continue
                if t in valid_ids and t not in seen:
                    cleaned.append(t)
                    seen.add(t)
            _site_config["featured"] = cleaned
        if "hero" in patch and isinstance(patch["hero"], dict):
            _site_config["hero"] = patch["hero"]
        save_site_config()
        log_event("site_config", featured_count=len(_site_config.get("featured") or []))
    return get_site_config()


def find_record(thread_id: int) -> dict | None:
    for r in _records:
        if int(r.get("thread_id", 0)) == thread_id:
            return r
    return None


def log_event(action: str, **detail) -> None:
    entry = {"ts": _iso_now(), "action": action, **detail}
    with _lock:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def auto_backup(label: str) -> str:
    """Snapshot registry.json before destructive ops. Returns filename."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40]
    name = f"registry-{ts}-{safe}.json"
    dst = BACKUPS_DIR / name
    if REGISTRY_PATH.exists():
        shutil.copy2(REGISTRY_PATH, dst)
    return name


# ----------------------------------------------------------------------------
# Stats
# ----------------------------------------------------------------------------

def compute_stats() -> dict:
    with _lock:
        total = len(_records)
        by_status: dict[str, int] = {}
        by_intent: dict[str, int] = {}
        by_tool: dict[str, int] = {}
        by_category: dict[str, int] = {}
        offered = 0
        last_health = None
        for r in _records:
            status = effective_status(r)
            by_status[status] = by_status.get(status, 0) + 1
            by_intent[r.get("intent", "discussion")] = by_intent.get(r.get("intent", "discussion"), 0) + 1
            by_tool[r.get("tool_type", "unknown")] = by_tool.get(r.get("tool_type", "unknown"), 0) + 1
            for t in (r.get("tags") or [])[:1]:
                by_category[t] = by_category.get(t, 0) + 1
            if r.get("intent") == "offering":
                offered += 1
            ch = (r.get("health") or {}).get("checked_at")
            if ch and (last_health is None or ch > last_health):
                last_health = ch
        return {
            "total": total,
            "offered": offered,
            "by_status": by_status,
            "by_intent": by_intent,
            "by_tool": by_tool,
            "by_category": by_category,
            "last_health_check": last_health,
            "backup_count": len(list(BACKUPS_DIR.glob("registry-*.json"))),
        }


# ----------------------------------------------------------------------------
# Mutators
# ----------------------------------------------------------------------------

# Whitelist of patchable top-level fields
PATCHABLE_FIELDS = {
    "title", "intent", "tool_type", "tags", "primary_url", "secondary_url",
    "urls", "url_count", "rating", "views", "posts", "is_locked", "is_sticky",
    "thread_url", "author", "manual_status", "manual_status_note",
}

# Valid status values for manual override. Empty string / None clears it.
# "url-likely" = no extracted URL but the post body mentions a known host
# (Torn API strips hyperlinks from thread content; the link text often survives).
VALID_MANUAL_STATUSES = {"ok", "stale", "broken", "unknown", "no-url", "url-likely"}

# Hostnames / keywords that strongly suggest the post links somewhere installable.
# Matched case-insensitively against the raw body text.
_URL_HINT_RE = re.compile(
    r"greasyfork|sleazyfork|openuserjs|github\.com|gist\.github|"
    r"docs\.google|sheets\.google|drive\.google|tornexchange|tornpda|torn-pda|"
    r"discord\.gg|discord\.com/invite|chrome\.google\.com/webstore|"
    r"chromewebstore\.google\.com|addons\.mozilla\.org|apps\.apple\.com|"
    r"play\.google\.com|bit\.ly|pastebin\.com",
    re.IGNORECASE,
)


def _read_body(r: dict) -> str:
    bf = r.get("body_file")
    if not bf:
        return ""
    p = HERE.parent.parent / "scripts" / bf
    if not p.exists():
        p = HERE.parent.parent / "guides" / bf
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def compute_body_url_hint(r: dict) -> bool:
    """True if the post body mentions a known install/source host."""
    body = _read_body(r)
    return bool(body) and bool(_URL_HINT_RE.search(body))


def backfill_body_url_hints() -> int:
    """Set r['body_mentions_url'] on every record that doesn't have it yet.
    Returns count of records updated."""
    n = 0
    with _lock:
        for r in _records:
            if "body_mentions_url" not in r:
                r["body_mentions_url"] = compute_body_url_hint(r)
                n += 1
        if n:
            save_registry()
    return n


def effective_status(r: dict) -> str:
    """Resolve a record's status, preferring manual override over health check.
    Promotes 'no-url' to 'url-likely' when the body mentions a known host."""
    ms = (r.get("manual_status") or "").strip()
    if ms in VALID_MANUAL_STATUSES:
        return ms
    base = (r.get("health") or {}).get("status") or (
        "unknown" if r.get("primary_url") else "no-url"
    )
    if base == "no-url" and r.get("body_mentions_url"):
        return "url-likely"
    return base


def patch_record(thread_id: int, patch: dict) -> dict:
    with _lock:
        r = find_record(thread_id)
        if r is None:
            raise KeyError(thread_id)
        edits = r.setdefault("_manual_edits", {})
        for k, v in patch.items():
            if k not in PATCHABLE_FIELDS:
                continue
            # Normalise manual_status: blank/None clears the override
            if k == "manual_status":
                v = (v or "").strip()
                if v and v not in VALID_MANUAL_STATUSES:
                    raise ValueError(f"invalid manual_status: {v!r}")
            edits[k] = _iso_now()
            r[k] = v
        if patch.get("urls") is not None:
            r["url_count"] = len(patch["urls"])
        save_registry()
        log_event("patch", thread_id=thread_id, fields=list(patch.keys()))
        return r


def delete_record(thread_id: int) -> bool:
    with _lock:
        global _records
        before = len(_records)
        _records = [r for r in _records if int(r.get("thread_id", 0)) != thread_id]
        if len(_records) == before:
            return False
        auto_backup(f"before-delete-{thread_id}")
        save_registry()
        log_event("delete", thread_id=thread_id)
        return True


def create_record(payload: dict) -> dict:
    """Create a new manual record. Auto-assigns a negative thread_id."""
    with _lock:
        # Negative IDs reserved for manually added (non-forum) entries
        existing_ids = [int(r.get("thread_id", 0)) for r in _records]
        min_neg = min([i for i in existing_ids if i < 0] + [0])
        new_id = min_neg - 1
        record = {
            "thread_id": new_id,
            "title": payload.get("title", "Untitled"),
            "author": payload.get("author") or {
                "id": None,
                "username": payload.get("author_username", "Anonymous"),
                "karma": None,
            },
            "forum_id": None,
            "first_post_time": None,
            "last_post_time": None,
            "first_post_iso": _iso_now(),
            "last_post_iso": _iso_now(),
            "posts": 0,
            "views": 0,
            "rating": 0,
            "is_locked": False,
            "is_sticky": False,
            "thread_url": payload.get("thread_url", ""),
            "body_file": None,
            "body_chars": 0,
            "url_count": len(payload.get("urls", [])),
            "urls": payload.get("urls", []),
            "primary_url": payload.get("primary_url"),
            "secondary_url": payload.get("secondary_url"),
            "intent": payload.get("intent", "offering"),
            "tool_type": payload.get("tool_type", "userscript"),
            "tags": payload.get("tags", []),
            "_added_manually": True,
            "_manual_edits": {"_created": _iso_now()},
        }
        _records.insert(0, record)
        save_registry()
        log_event("create", thread_id=new_id, title=record["title"])
        return record


def retag_record(thread_id: int) -> dict:
    """Re-run the rule-based tagger on a single record (without body text we
    only get title-level inference; that's still useful for offering toggles)."""
    with _lock:
        r = find_record(thread_id)
        if r is None:
            raise KeyError(thread_id)
        body = ""
        bf = r.get("body_file")
        if bf:
            body_path = HERE.parent.parent / "scripts" / bf
            if body_path.exists():
                try:
                    body = body_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    body = ""
        urls = r.get("urls") or []
        has_url = any(u.get("kind") in {"install", "extension", "spreadsheet", "source", "pda", "appstore", "website"} for u in urls)
        r["intent"] = tag_registry.detect_intent(r.get("title", ""), body, has_url)
        r["tool_type"] = tag_registry.detect_tool_type(r.get("title", ""), urls)
        r["tags"] = tag_registry.detect_tags(r.get("title", ""), body)
        r["body_mentions_url"] = bool(body) and bool(_URL_HINT_RE.search(body))
        save_registry()
        log_event("retag", thread_id=thread_id, tags=r["tags"])
        return r


def healthcheck_record(thread_id: int, force: bool = False) -> dict:
    with _lock:
        r = find_record(thread_id)
        if r is None:
            raise KeyError(thread_id)
        cache = health_check.load_cache()
        health_check.check_record(r, cache=cache, force=force)
        health_check.save_cache(cache)
        save_registry()
        log_event("healthcheck", thread_id=thread_id, status=r["health"].get("status"))
        return r


def healthcheck_all(only_kinds: list[str] | None, force: bool, limit: int | None, workers: int) -> dict:
    with _lock:
        records = _records
    only = set(only_kinds) if only_kinds else None
    summary = health_check.check_all_records(
        records, workers=workers, force=force, limit=limit, only_kinds=only
    )
    with _lock:
        save_registry()
    log_event("healthcheck_all", **summary)
    return summary


def retag_all() -> dict:
    """Re-run tagger on every record. Loads body files where available."""
    scripts_dir = HERE.parent.parent / "scripts"
    counts = {"updated": 0, "skipped": 0}
    with _lock:
        for r in _records:
            body = ""
            bf = r.get("body_file")
            if bf:
                body_path = scripts_dir / bf
                if body_path.exists():
                    try:
                        body = body_path.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        body = ""
            urls = r.get("urls") or []
            has_url = any(u.get("kind") in {"install", "extension", "spreadsheet", "source", "pda", "appstore", "website"} for u in urls)
            try:
                r["intent"] = tag_registry.detect_intent(r.get("title", ""), body, has_url)
                r["tool_type"] = tag_registry.detect_tool_type(r.get("title", ""), urls)
                r["tags"] = tag_registry.detect_tags(r.get("title", ""), body)
                r["body_mentions_url"] = bool(body) and bool(_URL_HINT_RE.search(body))
                counts["updated"] += 1
            except Exception:
                counts["skipped"] += 1
        save_registry()
    log_event("retag_all", **counts)
    return counts


def export_csv() -> int:
    with _lock:
        tag_registry.augment_csv(_records)
    log_event("export_csv", rows=len(_records))
    return len(_records)


# ----------------------------------------------------------------------------
# Backups
# ----------------------------------------------------------------------------

def list_backups() -> list[dict]:
    items = []
    for p in sorted(BACKUPS_DIR.glob("registry-*.json"), reverse=True):
        st = p.stat()
        items.append({
            "name": p.name,
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    return items


def create_backup(label: str = "manual") -> str:
    name = auto_backup(label or "manual")
    log_event("backup_create", name=name)
    return name


def restore_backup(name: str) -> dict:
    src = BACKUPS_DIR / name
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(name)
    # Snapshot current before overwriting
    auto_backup("before-restore")
    shutil.copy2(src, REGISTRY_PATH)
    load_registry()
    log_event("backup_restore", name=name)
    return {"restored": name, "records": len(_records)}


def delete_backup(name: str) -> bool:
    p = BACKUPS_DIR / name
    if not p.exists() or not p.is_file():
        return False
    p.unlink()
    log_event("backup_delete", name=name)
    return True


def tail_log(limit: int = 50) -> list[dict]:
    if not LOG_PATH.exists():
        return []
    lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return list(reversed(out))


# ----------------------------------------------------------------------------
# Query / filter / sort
# ----------------------------------------------------------------------------

def query_records(qs: dict) -> dict:
    q = (qs.get("q") or "").lower()
    status = set(filter(None, (qs.get("status") or "").split(",")))
    intent = set(filter(None, (qs.get("intent") or "").split(",")))
    category = set(filter(None, (qs.get("category") or "").split(",")))
    tool = set(filter(None, (qs.get("tool") or "").split(",")))
    sort = qs.get("sort") or "last_updated"
    limit = int(qs.get("limit") or 100)
    offset = int(qs.get("offset") or 0)

    with _lock:
        rows = list(_records)

    def row_status(r):
        return effective_status(r)

    def row_category(r):
        return (r.get("tags") or [None])[0]

    def row_last_updated_ts(r):
        h = (r.get("health") or {})
        s = h.get("last_updated") or r.get("last_post_iso") or ""
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0

    def row_installs(r):
        for c in (r.get("health") or {}).get("url_checks", []):
            md = c.get("metadata") or {}
            if md.get("source") == "greasyfork":
                return md.get("total_installs") or 0
        return 0

    if status:
        rows = [r for r in rows if row_status(r) in status]
    if intent:
        rows = [r for r in rows if r.get("intent") in intent]
    if category:
        rows = [r for r in rows if row_category(r) in category]
    if tool:
        rows = [r for r in rows if r.get("tool_type") in tool]
    if q:
        def blob(r):
            return " ".join(filter(None, [
                r.get("title", ""),
                (r.get("author") or {}).get("username", ""),
                " ".join(r.get("tags") or []),
                r.get("tool_type", ""),
                (r.get("primary_url") or {}).get("host", ""),
            ])).lower()
        rows = [r for r in rows if q in blob(r)]

    sort_fns = {
        "last_updated": (row_last_updated_ts, True),
        "installs": (row_installs, True),
        "rating": (lambda r: r.get("rating") or 0, True),
        "views": (lambda r: r.get("views") or 0, True),
        "posts": (lambda r: r.get("posts") or 0, True),
        "alpha": (lambda r: r.get("title", "").lower(), False),
    }
    fn, desc = sort_fns.get(sort, sort_fns["last_updated"])
    rows.sort(key=fn, reverse=desc)

    total = len(rows)
    page = rows[offset:offset + limit]
    return {"total": total, "offset": offset, "limit": limit, "records": page}


# ----------------------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------------------

CT_JSON = "application/json; charset=utf-8"
CT_HTML = "text/html; charset=utf-8"
CT_CSS = "text/css; charset=utf-8"
CT_JS = "application/javascript; charset=utf-8"
CT_TEXT = "text/plain; charset=utf-8"

EXT_CT = {
    ".html": CT_HTML, ".htm": CT_HTML, ".css": CT_CSS, ".js": CT_JS,
    ".json": CT_JSON, ".txt": CT_TEXT, ".svg": "image/svg+xml",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".ico": "image/x-icon", ".woff": "font/woff", ".woff2": "font/woff2",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "TornRegistryAdmin/0.1"

    # ----- helpers --------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # CORS for local dev (so opening admin/index.html via file:// would still work — we serve via http anyway)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, payload, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, CT_JSON)

    def _send_err(self, status: int, message: str, **extra):
        self._send_json({"error": message, **extra}, status=status)

    def _read_json(self) -> dict:
        ln = int(self.headers.get("Content-Length") or 0)
        if ln == 0:
            return {}
        raw = self.rfile.read(ln)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid JSON body: {e}")

    def _serve_file(self, path: Path):
        if not path.exists() or not path.is_file():
            self._send(404, b"Not Found", CT_TEXT)
            return
        ct = EXT_CT.get(path.suffix.lower(), "application/octet-stream")
        data = path.read_bytes()
        self._send(200, data, ct)

    # ----- routing --------------------------------------------------------
    def do_OPTIONS(self):
        self._send(204, b"", CT_TEXT)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self._route("GET")
        except Exception as e:
            traceback.print_exc()
            self._send_err(500, str(e))

    def do_POST(self):
        try:
            self._route("POST")
        except Exception as e:
            traceback.print_exc()
            self._send_err(500, str(e))

    def do_PATCH(self):
        try:
            self._route("PATCH")
        except Exception as e:
            traceback.print_exc()
            self._send_err(500, str(e))

    def do_PUT(self):
        try:
            self._route("PUT")
        except Exception as e:
            traceback.print_exc()
            self._send_err(500, str(e))

    def do_DELETE(self):
        try:
            self._route("DELETE")
        except Exception as e:
            traceback.print_exc()
            self._send_err(500, str(e))

    def _route(self, method: str):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        qs = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))

        # Root → redirect to /web/
        if path in ("", "/"):
            self.send_response(302)
            self.send_header("Location", "/web/")
            self.end_headers()
            return

        # API
        if path.startswith("/api/"):
            return self._route_api(method, path, qs)

        # Static folders
        if path == "/web" or path == "/admin":
            self.send_response(301)
            self.send_header("Location", path + "/")
            self.end_headers()
            return
        if path.startswith("/web/"):
            rel = path[len("/web/"):] or "index.html"
            return self._serve_file(WEB_DIR / rel)
        if path.startswith("/admin/"):
            rel = path[len("/admin/"):] or "index.html"
            return self._serve_file(ADMIN_DIR / rel)
        if path.startswith("/data/"):
            rel = path[len("/data/"):]
            return self._serve_file(DATA_DIR / rel)

        self._send(404, b"Not Found", CT_TEXT)

    # ----- API ------------------------------------------------------------
    def _route_api(self, method: str, path: str, qs: dict):
        parts = [p for p in path.split("/") if p]
        # parts[0] == "api"
        if len(parts) < 2:
            return self._send_err(404, "unknown endpoint")
        head = parts[1]

        if head == "stats" and method == "GET":
            return self._send_json(compute_stats())

        if head == "records":
            if len(parts) == 2:
                if method == "GET":
                    return self._send_json(query_records(qs))
                if method == "POST":
                    body = self._read_json()
                    rec = create_record(body)
                    return self._send_json(rec, status=201)
                return self._send_err(405, "method not allowed")
            # /records/{id}[/action]
            try:
                tid = int(parts[2])
            except ValueError:
                return self._send_err(400, "thread_id must be int")
            if len(parts) == 3:
                if method == "GET":
                    r = find_record(tid)
                    if r is None:
                        return self._send_err(404, "not found")
                    return self._send_json(r)
                if method == "PATCH":
                    body = self._read_json()
                    try:
                        return self._send_json(patch_record(tid, body))
                    except KeyError:
                        return self._send_err(404, "not found")
                    except ValueError as e:
                        return self._send_err(400, str(e))
                if method == "DELETE":
                    if not delete_record(tid):
                        return self._send_err(404, "not found")
                    return self._send_json({"deleted": tid})
            if len(parts) == 4 and method == "POST":
                action = parts[3]
                if action == "health-check":
                    force = qs.get("force") in ("1", "true", "yes")
                    try:
                        return self._send_json(healthcheck_record(tid, force=force))
                    except KeyError:
                        return self._send_err(404, "not found")
                if action == "retag":
                    try:
                        return self._send_json(retag_record(tid))
                    except KeyError:
                        return self._send_err(404, "not found")
            return self._send_err(404, "unknown record action")

        if head == "actions" and method == "POST":
            if len(parts) < 3:
                return self._send_err(404, "missing action")
            action = parts[2]
            body = self._read_json() if int(self.headers.get("Content-Length") or 0) > 0 else {}
            if action == "health-check-all":
                summary = healthcheck_all(
                    only_kinds=body.get("only_kinds"),
                    force=bool(body.get("force")),
                    limit=body.get("limit"),
                    workers=int(body.get("workers") or 8),
                )
                return self._send_json(summary)
            if action == "retag-all":
                return self._send_json(retag_all())
            if action == "export-csv":
                rows = export_csv()
                return self._send_json({"rows": rows, "path": str((DATA_DIR / "registry.csv").relative_to(HERE.parent.parent))})
            if action == "reload":
                load_registry()
                return self._send_json({"reloaded": True, "records": len(_records)})
            return self._send_err(404, "unknown action")

        if head == "backups":
            if method == "GET" and len(parts) == 2:
                return self._send_json({"backups": list_backups()})
            if method == "POST" and len(parts) == 2:
                body = self._read_json() if int(self.headers.get("Content-Length") or 0) > 0 else {}
                name = create_backup(body.get("label", "manual"))
                return self._send_json({"name": name}, status=201)
            if method == "POST" and len(parts) == 3 and parts[2] == "restore":
                body = self._read_json()
                try:
                    return self._send_json(restore_backup(body["name"]))
                except FileNotFoundError:
                    return self._send_err(404, "backup not found")
                except KeyError:
                    return self._send_err(400, "name required")
            if method == "DELETE" and len(parts) == 3:
                if not delete_backup(parts[2]):
                    return self._send_err(404, "backup not found")
                return self._send_json({"deleted": parts[2]})
            return self._send_err(405, "method not allowed")

        if head == "log" and method == "GET":
            limit = int(qs.get("limit") or 50)
            return self._send_json({"entries": tail_log(limit)})

        if head == "site-config":
            if method == "GET":
                return self._send_json(get_site_config())
            if method in ("PUT", "PATCH", "POST"):
                body = self._read_json()
                return self._send_json(set_site_config(body))
            return self._send_err(405, "method not allowed")

        return self._send_err(404, "unknown endpoint")

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    load_registry()
    load_site_config()
    backfilled = backfill_body_url_hints()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"  Public registry: http://{host}:{port}/web/")
    print(f"  Admin console:   http://{host}:{port}/admin/")
    print(f"  API base:        http://{host}:{port}/api/")
    print(f"  Records loaded:  {len(_records)}")
    if backfilled:
        print(f"  Body-URL hints:  scanned {backfilled} new records")
    print(f"  Backups dir:     {BACKUPS_DIR}")
    print("  Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        httpd.server_close()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Torn Script Registry — admin/API server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args()
    serve(args.host, args.port)
