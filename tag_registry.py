"""Tag and classify entries in the script registry.

Reads:  data/registry.json (built by build_registry.py)
Writes: data/registry.json (in place) with three new fields per record:
    - intent:    "offering" | "request" | "service" | "discussion"
    - tool_type: "userscript" | "extension" | "spreadsheet" | "website"
                 | "discord-bot" | "pda-script" | "mobile-app"
                 | "python-script" | "discussion" | "unknown"
    - tags:      list[str]  (functional categories — multi-label, ranked)

Also rewrites data/registry.csv with these columns:
    - primary_kind   = URL kind  (install / website / spreadsheet / discord / …)
    - secondary_kind = functional category (combat / faction / oc / bazaar /
                       market / casino / racing / event / qol / travel /
                       company / chat / progression / api-dev / crimes)
    - intent, tool_type, tags

All classification is deterministic, rule-based, and uses the title + body
text + previously-extracted URL kinds.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
REGISTRY_PATH = HERE / "data" / "registry.json"
CSV_PATH = HERE / "data" / "registry.csv"


def _wb(*words: str) -> re.Pattern:
    """Compile a case-insensitive word-boundary regex matching ANY of words."""
    parts = "|".join(re.escape(w) for w in words)
    return re.compile(rf"(?<![A-Za-z0-9])(?:{parts})(?![A-Za-z0-9])", re.IGNORECASE)


# --- Intent ------------------------------------------------------------------
# Order matters: tested top-to-bottom; first match wins.

# Title-level cues that scream "I am offering / shipping a tool".
OFFERING_TITLE_PREFIXES = re.compile(
    r"^\s*\[(script|userscript|site|website|web|tool|app|bot|discord\s*bot|"
    r"sheet|spreadsheet|google\s*sheet|extension|chrome\s*extension|guide|"
    r"library|api|service|tool/discord\s*bot\s*access|sheet/script|"
    r"spreadsheet\s*\+\s*script|script\s*\+\s*pda|pc\s*script|"
    r"script\s*\+\s*ai|tampermonkey|userscript\s*/?\s*tornpda|"
    r"chrome\s*extension|userscript\s*\+\s*tornpda|s|new\s*script|"
    r"release|free|new|tools|sheets|webapp|site/discord\s*bot|"
    r"new\s*tool)\]",
    re.IGNORECASE,
)
SERVICE_PATTERNS = _wb(
    "for hire", "selling custom", "custom script", "custom scripts",
    "i will build", "i can help", "commission", "scripts for sale",
    "writing service", "scripting service", "scripting services",
    "made to order", "negotiable", "buying custom", "wtb",
)
SERVICE_TITLE_PREFIXES = re.compile(
    r"^\s*\[(service|commission|for\s*hire|s|b|paid|hire|wtb|hiring)\]",
    re.IGNORECASE,
)
REQUEST_PATTERNS = _wb(
    "looking for", "anyone got", "is there a script", "is there any script",
    "does anyone have", "does anyone know", "need a script", "need someone",
    "request", "buying", "in need of", "i need", "i'm looking",
    "any way to", "where can i find", "help me find", "how do i",
    "where do i find", "anyone make", "want a script", "want a tool",
    "iso", "i want a", "i need a", "looking to buy", "any scripts for",
    "any scripter", "i'm searching", "im looking", "any tools",
    "any chance", "is there", "anyone willing", "wtb",
)
REQUEST_TITLE_PREFIXES = re.compile(
    r"^\s*\[(request|req|r|help|question|q|iso|looking|need|wtb|b|buying|"
    r"r/question|help/request|s/help|sos|h)\]",
    re.IGNORECASE,
)
DISCUSSION_KEYWORDS = _wb(
    "rules", "allowed", "legal", "permitted", "is this against",
    "is it ok", "psa", "warning", "announcement",
)


def detect_intent(title: str, body: str, has_distribution_url: bool) -> str:
    t = title or ""
    body_head = (body or "")[:4000]

    # Title prefixes are strong signals.
    if SERVICE_TITLE_PREFIXES.search(t) and not OFFERING_TITLE_PREFIXES.search(t):
        # Distinguish [B] (buying = request) from [S] (selling = service)
        m = re.match(r"^\s*\[([^\]]+)\]", t)
        if m:
            tag = m.group(1).strip().lower()
            if tag in {"b", "buying", "wtb", "hire", "hiring"}:
                return "request"
            if tag in {"s", "service", "commission", "for hire", "paid"}:
                return "service"
    if REQUEST_TITLE_PREFIXES.search(t):
        return "request"
    if OFFERING_TITLE_PREFIXES.search(t):
        return "offering"

    # Body-level cues.
    if SERVICE_PATTERNS.search(t) or SERVICE_PATTERNS.search(body_head):
        # Service usually advertises with a price/hire phrasing
        if not REQUEST_PATTERNS.search(t):
            return "service"

    if REQUEST_PATTERNS.search(t):
        return "request"

    # Has a real distribution URL + non-request title -> probably offering.
    if has_distribution_url:
        return "offering"

    # Pure question/discussion fallback
    if t.rstrip().endswith("?") or DISCUSSION_KEYWORDS.search(t):
        return "discussion"
    if REQUEST_PATTERNS.search(body_head):
        return "request"

    return "discussion"


# --- Tool type ---------------------------------------------------------------

TOOL_TYPE_TITLE = [
    ("spreadsheet", _wb("spreadsheet", "sheet", "google sheet",
                         "google sheets", "excel", "gsheet", "gsheets")),
    ("extension", _wb("extension", "chrome extension", "firefox extension",
                       "browser extension", "webstore", "addon", "add-on")),
    ("discord-bot", _wb("discord bot", "discord-bot", "bot")),
    ("pda-script", _wb("pda", "tornpda")),
    ("mobile-app", _wb("ios", "android", "iphone", "apple watch")),
    ("python-script", _wb("python")),
    ("website", _wb("site", "website", "webapp", "web app", "web tool",
                     "tool", "app")),
    ("userscript", _wb("script", "userscript", "tampermonkey",
                        "violentmonkey", "greasyfork", "greasy fork")),
]


def detect_tool_type(title: str, urls: list[dict]) -> str:
    """Infer the kind of artifact being shared. URLs win over title cues."""
    kinds = {u["kind"] for u in urls}
    hosts = {u["host"] for u in urls}

    # URL kinds are the most reliable signal.
    if "spreadsheet" in kinds:
        return "spreadsheet"
    if "extension" in kinds:
        return "extension"
    if "install" in kinds:
        # Could be userscript OR PDA-installed userscript — title decides.
        if re.search(r"\b(pda|tornpda)\b", title, re.IGNORECASE):
            return "pda-script"
        return "userscript"
    if "appstore" in kinds or "pda" in kinds:
        return "mobile-app"
    if "source" in kinds and re.search(r"\b(python)\b", title, re.IGNORECASE):
        return "python-script"
    # Discord invite + body about a bot -> discord-bot
    if "discord" in kinds and re.search(r"\bbot\b", title, re.IGNORECASE):
        return "discord-bot"

    # Title-only fallback.
    for ttype, rx in TOOL_TYPE_TITLE:
        if rx.search(title):
            return ttype

    # Has any non-media/non-torn URL -> website
    non_meta = kinds - {"media", "torn"}
    if non_meta:
        return "website"

    return "unknown"


# --- Tags (functional categories) -------------------------------------------
#
# Tags describe WHAT GAME FUNCTION a tool helps with, not what it is built
# from. A single tool can wear multiple tags (e.g. a chain-timer also tagged
# `faction`). Keep the list small and broad — fine-grained sub-categories
# can be derived later from the same body text.

# Each rule: (tag_name, regex against title + body head)
TAG_RULES: list[tuple[str, re.Pattern]] = [

    # combat — PvP, attacks, war, hosp/jail/revive, intel/spying
    ("combat", _wb(
        "attack", "attacking", "attacker", "fight", "fighting",
        "hit", "hitter", "hits", "target", "targets", "loadout",
        "weapon", "weapons", "armor", "armour", "damage", "dps",
        "ff scouter", "ff-scouter", "fair fight", "fairfight",
        "spy", "spies", "stalker", "stalk", "battle stats",
        "battlestats", "execute", "execution",
        "mug", "mugging", "mugger", "muggers", "anti-mug",
        "chain", "chains", "chaining", "chainlist", "kill streak",
        "killstreak",
        "ranked war", "ranked-war", "rw", "war payout", "warroom",
        "war room", "war timer", "war calculator", "warring",
        "revive", "revives", "reviver", "revival",
        "hospital", "hosp", "hospitalize", "hospitalized",
        "jail", "jailbust", "jail bust",
    )),

    # faction — anything faction-internal that isn't war (perks, respect,
    # newsletter, loans, balance, presets, applicants, recruitment, armory)
    ("faction", _wb(
        "faction", "factions", "faction management", "perks",
        "perk branch", "respect", "respect leaderboard",
        "faction activity", "faction balance", "loan tracker",
        "loans", "presets", "newsletter", "applicants",
        "recruitment", "armory", "armoury", "donations",
    )),

    # OC — organized crimes (kept separate per README)
    ("oc", _wb(
        "oc", "oc 2.0", "oc2", "organized crime", "organized crimes",
        "oc planner", "oc tracker", "oc payout",
    )),

    # crimes — Crimes 2.0 individual crimes
    ("crimes", _wb(
        "shoplifting", "shoplift", "hustling", "hustle",
        "cracking", "scamming", "scam", "scammer",
        "card skimming", "card skim", "skimming", "skimmer",
        "arson", "disposal", "graffiti", "burglary", "burgle",
        "search for cash", "sfc", "pickpocket", "pickpocketing",
        "bootleg", "bootlegging", "crimes 2.0", "crimes2",
    )),

    # bazaar
    ("bazaar", _wb("bazaar", "bazaars")),

    # market — item market + points market + auction + trading + stocks + bank
    ("market", _wb(
        "item market", "marketplace", "market sniper",
        "market scanner", "market notification", "listings",
        "snipe", "sniper", "snipper",
        "auction house", "auctions", "tradeposts",
        "trade", "trader", "trading", "flip", "flipper",
        "flipping", "arbitrage",
        "points market", "point market", "point shop",
        "points shop", "points refill",
        "stock", "stocks", "stock market", "dividend", "snowball",
        "bank", "banker", "banking", "vault", "investment",
        "investments",
    )),

    # casino — all gambling games
    ("casino", _wb(
        "casino", "gambling", "spin the wheel", "lucky",
        "poker", "flopmaster",
        "blackjack", "roulette",
        "slots", "slot machine",
        "keno",
        "high low", "high-low", "highlow",
        "russian roulette", "russian-roulette",
        "lottery", "bookie", "bookies",
    )),

    # racing
    ("racing", _wb(
        "race", "races", "racing", "raceway", "racetrack",
        "race tracks", "racing skill",
    )),

    # event — seasonal / special events
    ("event", _wb(
        "event", "events",
        "halloween", "spooky", "haunted",
        "christmas", "christmas town", "santa", "eggnog",
        "easter", "egg hunt",
        "awareness", "awareness week",
        "elimination", "elim",
        "dog tag", "dog tags", "dogtag", "dogtags",
        "valentine", "valentines", "valentine's",
        "anniversary",
    )),

    # qol — UI tweaks, themes, hotkeys, hide/remove buttons, notifications
    ("qol", _wb(
        "hide", "remove", "scaling", "scale", "theme", "themes",
        "color", "colour", "colorblind", "color blind",
        "color code", "color coded", "background", "backdrop",
        "font", "icon", "icons", "navigation", "sidebar",
        "hotkey", "hotkeys", "keybind", "keybinds",
        "shortcut", "shortcuts", "radial", "menu", "qol",
        "quality of life", "tab", "tabs", "focus", "zoom",
        "ctzoom", "preview", "tooltip", "tooltips",
        "notification", "notifications", "alert", "alerts",
        "alarm", "reminder", "reminders", "notify",
    )),

    # travel — flights, foreign countries, abroad shopping
    ("travel", _wb(
        "travel", "traveling", "flight", "flying", "fly",
        "abroad", "foreign", "overseas", "valigia",
        "argentina", "canada", "cayman", "china", "hawaii",
        "japan", "mexico", "south africa", "switzerland", "uae",
        "united kingdom", "uk shopping",
    )),

    # company — companies, employees, jobs, salary, hiring
    ("company", _wb(
        "company", "companies", "employee", "employees",
        "salary", "salaries", "vacancies", "vacant", "position",
        "director", "directors", "hire", "hires", "hiring",
        "recruit", "recruiting", "job", "jobs", "job search",
        "job perks", "workstat", "workstats", "work stats",
    )),

    # chat — chat & forum tooling
    ("chat", _wb(
        "chat", "chats", "chat 2.0", "chat 3.0", "messenger",
        "forum", "forums", "forum thread", "thread generator",
        "forum signature", "signature", "bbcode",
    )),

    # progression — gym, drugs, books/education, merits, plushies/flowers
    ("progression", _wb(
        "gym", "training", "ratio", "baldr", "hank",
        "happy jump", "stat enhancer", "stat gain",
        "build tracker", "gym caps",
        "xanax", "drug", "drugs", "addiction", "vicodin",
        "cooldown manager", "booster cooldown",
        "books", "book", "education", "education courses",
        "merit", "merits", "honor", "honor bar",
        "plushie", "plushies", "flower", "flowers",
        "museum",
        "inventory", "item icon", "item value",
    )),

    # api-dev — libraries, dev tools, scripting tutorials, debugging tooling
    ("api-dev", _wb(
        "api wrapper", "api library", "torn api library",
        "swagger", "endpoint", "sdk", "csp",
        "developer mode", "dev mode",
        "scripting rules", "scripting tutorial",
        "writing userscripts", "writing a userscript",
        "tampermonkey settings", "tampermonkey config",
        "how to install", "install guide",
        "data wrapper", "torn data",
    )),
]


def detect_tags(title: str, body: str, max_body: int = 2500) -> list[str]:
    """Return matching tags ranked by relevance (most relevant first).

    Scoring: each keyword hit in the title is worth 3 points, each hit in
    the body head is worth 1. Ties are broken by the TAG_RULES order.
    """
    title_s = title or ""
    body_s = (body or "")[:max_body]
    scored: list[tuple[int, int, str]] = []  # (-score, rule_index, tag)
    for idx, (name, rx) in enumerate(TAG_RULES):
        t_hits = len(rx.findall(title_s))
        b_hits = len(rx.findall(body_s))
        score = t_hits * 3 + b_hits
        if score:
            scored.append((-score, idx, name))
    scored.sort()
    return [name for _, _, name in scored]


# --- CSV writer --------------------------------------------------------------

CSV_FIELDS = [
    "thread_id", "title", "author_username", "author_id",
    "first_post_iso", "last_post_iso", "posts", "views", "rating",
    "is_locked", "is_sticky",
    "url_count",
    "primary_kind", "primary_host", "primary_url",
    "secondary_kind",       # = top functional category (combat/faction/...)
    "intent", "tool_type",
    "tags",                 # full ranked list, semicolon-joined
    "thread_url",
]


def augment_csv(records: list[dict]) -> None:
    with CSV_PATH.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in records:
            p = r.get("primary_url") or {}
            author = r.get("author") or {}
            tags = r.get("tags") or []
            w.writerow({
                "thread_id": r["thread_id"],
                "title": r["title"],
                "author_username": author.get("username"),
                "author_id": author.get("id"),
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
                "secondary_kind": tags[0] if tags else None,
                "intent": r.get("intent"),
                "tool_type": r.get("tool_type"),
                "tags": ";".join(tags),
                "thread_url": r["thread_url"],
            })


# --- Main --------------------------------------------------------------------

def main() -> int:
    records = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    print(f"Loaded {len(records)} records from {REGISTRY_PATH.name}")

    # We need body text again for tagging. Read on demand.
    scripts_dir = HERE.parent.parent / "scripts"

    intent_counts: Counter[str] = Counter()
    tool_counts: Counter[str] = Counter()
    tag_counts: Counter[str] = Counter()
    no_tag = 0

    for r in records:
        title = r.get("title", "")
        urls = r.get("urls") or []
        body_file = r.get("body_file")
        body = ""
        if body_file:
            p = scripts_dir / body_file
            if p.exists():
                try:
                    body = p.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    body = p.read_text(encoding="utf-8", errors="replace")

        has_dist = any(
            u["kind"] in ("install", "extension", "spreadsheet", "source",
                          "pda", "appstore")
            for u in urls
        )
        intent = detect_intent(title, body, has_dist)
        tool_type = detect_tool_type(title, urls)
        # If a thread is clearly a request, "tool_type" is meaningless.
        if intent == "request":
            tool_type = "request"
        elif intent == "service":
            # Keep the inferred tool_type if any, else "service"
            if tool_type == "unknown":
                tool_type = "service"
        elif intent == "discussion":
            if tool_type == "unknown":
                tool_type = "discussion"

        tags = detect_tags(title, body)

        r["intent"] = intent
        r["tool_type"] = tool_type
        r["tags"] = tags

        intent_counts[intent] += 1
        tool_counts[tool_type] += 1
        if not tags:
            no_tag += 1
        for tg in tags:
            tag_counts[tg] += 1

    REGISTRY_PATH.write_text(
        json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Rewrote {REGISTRY_PATH.relative_to(HERE.parent.parent)}")

    # Re-emit the CSV so intent / tool_type / tags are visible there too.
    # We add three columns at the end; the rest mirror build_registry.py.
    augment_csv(records)
    print(f"Rewrote {CSV_PATH.relative_to(HERE.parent.parent)} "
          f"(with intent / tool_type / tags columns)")

    print("\nIntent distribution:")
    for k, n in intent_counts.most_common():
        print(f"  {k:12s} {n}")

    print("\nTool-type distribution:")
    for k, n in tool_counts.most_common():
        print(f"  {k:14s} {n}")

    print(f"\nThreads with NO topical tag: {no_tag}")
    print("\nTop 30 tags:")
    for tag, n in tag_counts.most_common(30):
        print(f"  {n:4d}  {tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
