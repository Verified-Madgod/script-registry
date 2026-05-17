"""Health-check every distribution URL in the script registry.

Reads:
  data/registry.json   (records with `urls`, `primary_url`, ...)

Writes:
  data/health_cache.json   per-URL check results (keyed by URL, reusable)
  data/registry.json       in-place: adds a `health` rollup per record

A `health` rollup on a record looks like:
    {
      "checked_at":      "2026-05-16T18:30:00Z",
      "status":          "ok" | "broken" | "stale" | "unknown" | "no-url",
      "primary_status":  "ok" | "broken" | ... | None,
      "last_updated":    "2025-11-01T...Z" | None,
      "url_checks": [
        {
          "url": "...",
          "kind": "install",
          "host": "greasyfork.org",
          "http_status": 200,
          "ok": true,
          "final_url": "...",          # after redirects
          "elapsed_ms": 318,
          "last_updated": "2025-11-01T...Z",  # if derivable
          "error": null,
          "checked_at": "2026-05-16T18:30:00Z"
        },
        ...
      ]
    }

The single public entry point you can call from anywhere is:

    from health_check import check_record, check_all_records

    rollup = check_record(record)            # one record
    check_all_records(records, workers=8)    # batch (in place)

Stdlib only. Concurrent HTTPS via ThreadPoolExecutor. Per-URL cache keeps
re-runs cheap — pass force=True to ignore the cache.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse, quote

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
REGISTRY_PATH = DATA_DIR / "registry.json"
CACHE_PATH = DATA_DIR / "health_cache.json"

USER_AGENT = (
    "TornScriptRegistry-HealthCheck/0.1 "
    "(+https://github.com/torn-research; contact via forum)"
)

# Kinds we consider worth health-checking. Media/torn self-links/discord
# invites aren't useful indicators of whether a script still works.
CHECKABLE_KINDS = {"install", "source", "spreadsheet", "extension",
                    "pda", "appstore", "website"}

# A URL whose "last update" is older than this is flagged stale.
STALE_AFTER_DAYS = 365

# Network knobs.
REQUEST_TIMEOUT = 12   # seconds per HTTP request
CACHE_TTL_HOURS = 24   # treat cached results younger than this as fresh


# --- HTTP helpers ------------------------------------------------------------

def _safe_url(url: str) -> str:
    """Percent-encode any non-ASCII chars in path/query/fragment, IDNA-encode
    the hostname. urllib refuses to send raw non-ASCII bytes in the request
    line, so we normalise here.
    """
    try:
        p = urlparse(url)
    except ValueError:
        return url
    try:
        host = p.hostname.encode("idna").decode("ascii") if p.hostname else ""
    except (UnicodeError, AttributeError):
        host = p.hostname or ""
    netloc = host
    if p.port:
        netloc = f"{host}:{p.port}"
    if p.username:
        userinfo = quote(p.username, safe="")
        if p.password:
            userinfo += ":" + quote(p.password, safe="")
        netloc = f"{userinfo}@{netloc}"
    path = quote(p.path, safe="/%:@!$&'()*+,;=~-._")
    query = quote(p.query, safe="=&%:@!$'()*+,;/?~-._")
    fragment = quote(p.fragment, safe="=&%:@!$'()*+,;/?~-._")
    return urlunparse((p.scheme, netloc, path, p.params, query, fragment))


def _request(url: str, method: str = "HEAD") -> tuple[int, dict, str]:
    """Return (status_code, headers_dict_lowercased, final_url).

    Follows redirects (urllib's default behavior). Raises urllib errors.
    """
    req = urllib.request.Request(
        _safe_url(url), method=method,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        },
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        headers = {k.lower(): v for k, v in resp.headers.items()}
        return resp.status, headers, resp.geturl()


def http_check(url: str) -> dict:
    """HEAD a URL, falling back to GET when HEAD is unsupported.

    Returns a dict with: http_status, ok, final_url, elapsed_ms,
    last_modified (from Last-Modified header if present), error.
    """
    started = time.monotonic()
    result: dict = {
        "http_status": None,
        "ok": False,
        "final_url": None,
        "elapsed_ms": None,
        "last_modified": None,
        "error": None,
    }
    for method in ("HEAD", "GET"):
        try:
            status, headers, final_url = _request(url, method=method)
        except urllib.error.HTTPError as e:
            if e.code == 405 and method == "HEAD":
                # Method not allowed — retry as GET
                continue
            result["http_status"] = e.code
            result["final_url"] = url
            result["error"] = f"HTTP {e.code}"
            break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            result["error"] = f"{type(e).__name__}: {e}"
            break
        else:
            result["http_status"] = status
            result["ok"] = 200 <= status < 400
            result["final_url"] = final_url
            lm = headers.get("last-modified")
            if lm:
                try:
                    dt = parsedate_to_datetime(lm)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    result["last_modified"] = dt.astimezone(timezone.utc) \
                        .strftime("%Y-%m-%dT%H:%M:%SZ")
                except Exception:  # noqa: BLE001
                    pass
            break
    result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return result


# --- Greasy Fork metadata ----------------------------------------------------

_GF_ID_RE = re.compile(
    r"greasyfork\.org/(?:[a-z]{2}(?:-[a-z]+)?/)?scripts/(\d+)",
    re.IGNORECASE,
)
_SF_ID_RE = re.compile(
    r"sleazyfork\.org/(?:[a-z]{2}(?:-[a-z]+)?/)?scripts/(\d+)",
    re.IGNORECASE,
)


def fetch_greasyfork_meta(url: str) -> dict | None:
    """Read script metadata from Greasy Fork's public JSON endpoint.

    Returns {last_updated, install_count, daily_installs, total_installs,
    name, version} or None if URL isn't a Greasy Fork script page.
    """
    m = _GF_ID_RE.search(url) or _SF_ID_RE.search(url)
    if not m:
        return None
    script_id = m.group(1)
    base = "sleazyfork.org" if "sleazyfork" in url.lower() else "greasyfork.org"
    api_url = f"https://{base}/scripts/{script_id}.json"
    try:
        status, _, _ = _request(api_url, method="GET")
    except (urllib.error.URLError, OSError):
        return None
    try:
        req = urllib.request.Request(
            api_url, headers={"User-Agent": USER_AGENT,
                              "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.load(resp)
    except Exception:  # noqa: BLE001
        return None
    return {
        "source": "greasyfork",
        "last_updated": data.get("code_updated_at") or data.get("updated_at"),
        "name": data.get("name"),
        "version": data.get("version"),
        "total_installs": data.get("total_installs"),
        "daily_installs": data.get("daily_installs"),
        "good_ratings": data.get("good_ratings"),
        "bad_ratings": data.get("bad_ratings"),
        "deleted": bool(data.get("deleted")),
    }


# --- GitHub metadata ---------------------------------------------------------

_GH_REPO_RE = re.compile(
    r"github\.com/([^/\s?#]+)/([^/\s?#]+?)(?:\.git)?(?:[/?#].*)?$",
    re.IGNORECASE,
)
_GH_RAW_RE = re.compile(
    r"raw\.githubusercontent\.com/([^/\s]+)/([^/\s]+)/",
    re.IGNORECASE,
)


def fetch_github_meta(url: str) -> dict | None:
    """Read repo metadata from the public GitHub API (no auth).

    Note: anonymous GitHub API is capped at 60 requests/hour per IP.
    Returns {last_updated, stars, archived, ...} or None if not a GitHub URL.
    """
    m = _GH_REPO_RE.search(url) or _GH_RAW_RE.search(url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    repo = repo.split("#")[0].split("?")[0]
    if not owner or not repo or owner.lower() in {"raw", "blob"}:
        return None
    api_url = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        req = urllib.request.Request(
            api_url, headers={"User-Agent": USER_AGENT,
                              "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        return {
            "source": "github",
            "error": f"HTTP {e.code}",
            "owner": owner,
            "repo": repo,
        }
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return {
        "source": "github",
        "owner": owner,
        "repo": repo,
        "last_updated": data.get("pushed_at") or data.get("updated_at"),
        "stars": data.get("stargazers_count"),
        "forks": data.get("forks_count"),
        "open_issues": data.get("open_issues_count"),
        "archived": data.get("archived"),
        "default_branch": data.get("default_branch"),
        "description": data.get("description"),
    }


# --- Per-URL check (cached) --------------------------------------------------

def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def check_url(url: str, kind: str, host: str,
              cache: dict | None = None, force: bool = False) -> dict:
    """Check a single URL. Returns the dict that will be stored in the cache."""
    if cache is not None and not force and url in cache:
        cached = cache[url]
        seen = _parse_iso(cached.get("checked_at"))
        if seen and (datetime.now(timezone.utc) - seen).total_seconds() \
                < CACHE_TTL_HOURS * 3600:
            return cached

    http = http_check(url)
    meta: dict | None = None
    if kind == "install":
        meta = fetch_greasyfork_meta(url)
    elif kind == "source":
        meta = fetch_github_meta(url)

    # last_updated: prefer metadata, then HTTP Last-Modified header.
    last_updated = None
    if meta and meta.get("last_updated"):
        last_updated = meta["last_updated"]
    elif http.get("last_modified"):
        last_updated = http["last_modified"]

    record = {
        "url": url,
        "kind": kind,
        "host": host,
        "http_status": http["http_status"],
        "ok": http["ok"],
        "final_url": http["final_url"],
        "elapsed_ms": http["elapsed_ms"],
        "last_updated": last_updated,
        "metadata": meta,
        "error": http["error"],
        "checked_at": _iso_now(),
    }
    if cache is not None:
        cache[url] = record
    return record


# --- Record rollup -----------------------------------------------------------

def _is_stale(iso_ts: str | None) -> bool:
    dt = _parse_iso(iso_ts)
    if dt is None:
        return False
    age_days = (datetime.now(timezone.utc) - dt).days
    return age_days > STALE_AFTER_DAYS


def check_record(record: dict, cache: dict | None = None,
                 force: bool = False) -> dict:
    """Health-check a single registry record. Returns the `health` rollup
    AND mutates `record["health"]` in place."""
    urls = record.get("urls") or []
    primary = record.get("primary_url") or None
    primary_url = primary.get("url") if primary else None

    checks: list[dict] = []
    for u in urls:
        if u["kind"] not in CHECKABLE_KINDS:
            continue
        checks.append(check_url(u["url"], u["kind"], u["host"],
                                cache=cache, force=force))

    if not checks:
        rollup = {
            "checked_at": _iso_now(),
            "status": "no-url",
            "primary_status": None,
            "last_updated": None,
            "url_checks": [],
        }
        record["health"] = rollup
        return rollup

    primary_check = next(
        (c for c in checks if c["url"] == primary_url), checks[0]
    )

    # latest last_updated across all checks (when available)
    latest = None
    for c in checks:
        dt = _parse_iso(c.get("last_updated"))
        if dt and (latest is None or dt > latest):
            latest = dt
    latest_iso = latest.strftime("%Y-%m-%dT%H:%M:%SZ") if latest else None

    # Status rules:
    #   broken  = primary URL HTTP error or unreachable
    #   stale   = primary OK, but last_updated > STALE_AFTER_DAYS
    #   ok      = primary OK and either fresh or unknown age
    #   unknown = primary check failed for non-HTTP reason (DNS/timeout)
    if primary_check["http_status"] is None:
        primary_status = "unknown"
    elif primary_check["ok"]:
        if _is_stale(primary_check.get("last_updated")):
            primary_status = "stale"
        else:
            primary_status = "ok"
    else:
        primary_status = "broken"

    rollup = {
        "checked_at": _iso_now(),
        "status": primary_status,
        "primary_status": primary_status,
        "last_updated": latest_iso,
        "url_checks": checks,
    }
    record["health"] = rollup
    return rollup


# --- Batch -------------------------------------------------------------------

def load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_cache(cache: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps(cache, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def check_all_records(records: list[dict], workers: int = 8,
                      force: bool = False, limit: int | None = None,
                      only_kinds: set[str] | None = None) -> dict:
    """Check every record in `records` in parallel.

    Mutates each record in place to add `record["health"]`.
    Returns a summary dict: {ok, broken, stale, unknown, no-url, total}.
    """
    cache = load_cache()

    # Filter what we actually plan to check (so progress count is meaningful).
    target_records = []
    for r in records:
        urls = r.get("urls") or []
        kinds = {u["kind"] for u in urls}
        if only_kinds is not None and not (kinds & only_kinds):
            continue
        target_records.append(r)
    if limit:
        target_records = target_records[:limit]

    print(f"Checking {len(target_records)} records "
          f"(workers={workers}, force={force})")

    done = 0
    summary = {"ok": 0, "broken": 0, "stale": 0,
               "unknown": 0, "no-url": 0, "total": 0}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(check_record, r, cache, force): r
            for r in target_records
        }
        for fut in as_completed(futures):
            r = futures[fut]
            try:
                rollup = fut.result()
            except Exception as e:  # noqa: BLE001
                rollup = {"status": "unknown", "error": str(e)}
                r["health"] = rollup
            done += 1
            status = rollup.get("status", "unknown")
            summary[status] = summary.get(status, 0) + 1
            summary["total"] += 1
            if done % 25 == 0 or done == len(target_records):
                print(f"  [{done:>4}/{len(target_records)}]  "
                      f"ok={summary['ok']} broken={summary['broken']} "
                      f"stale={summary['stale']} unknown={summary['unknown']} "
                      f"no-url={summary['no-url']}")

    save_cache(cache)
    return summary


# --- CLI ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--limit", type=int, default=None,
                   help="check only the first N records (after filtering)")
    p.add_argument("--workers", type=int, default=8,
                   help="parallel worker threads (default 8)")
    p.add_argument("--force", action="store_true",
                   help="ignore the URL cache; re-check everything")
    p.add_argument("--only-kinds", default=None,
                   help="comma-sep list of URL kinds to require, e.g. "
                        "'install,source'. Records with no URL of these "
                        "kinds are skipped entirely.")
    args = p.parse_args(argv)

    only_kinds = (set(k.strip() for k in args.only_kinds.split(","))
                  if args.only_kinds else None)

    records = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    print(f"Loaded {len(records)} records from {REGISTRY_PATH.name}")

    summary = check_all_records(
        records, workers=args.workers, force=args.force,
        limit=args.limit, only_kinds=only_kinds,
    )

    REGISTRY_PATH.write_text(
        json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\nWrote {REGISTRY_PATH.relative_to(HERE.parent.parent)}")
    print(f"Wrote {CACHE_PATH.relative_to(HERE.parent.parent)}")
    print("\nSummary:")
    for k in ("ok", "stale", "broken", "unknown", "no-url", "total"):
        print(f"  {k:8s} {summary.get(k, 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
