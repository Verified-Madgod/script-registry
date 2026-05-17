# Script Registry & Health Dashboard  *(active)*

A browsable, categorized, **automatically health-checked** index of every
community userscript / tool / spreadsheet / Discord bot from the Tools &
Userscripts subforum.

## Problem
- The T&U subforum is the only "directory" for ~2500 community tools.
- Scripts die silently when Torn updates — there is no canonical list of
  what still works.
- Hundreds of forum threads literally titled *"Is X still working?"*,
  *"Looking for a script that…"*, *"Where do I find…"* confirm the gap.
- No tags, no screenshots, no working/broken status, no last-updated date.

## What we build
1. **Seed catalog**: parse the 1000 scraped threads in `scripts/` →
   extract title, author, thread id, OP date, install/source URLs
   (Greasyfork, GitHub, Tampermonkey, Google Sheets, websites).
2. **Categorization**: rule-based + LLM-assisted tags
   (combat, faction, OC, bazaar, market, casino, racing, event, QoL, …).
3. **Health checker**: nightly job that:
   - HEAD-checks install/source URLs.
   - For Greasyfork/GitHub, reads last-update timestamp.
   - Optional: load script in a headless browser against a test page,
     verify it injects without throwing.
4. **Front-end**: static site with faceted search (category, status,
   author, last-updated), per-script page with screenshot, install button,
   working/broken badge, related threads.
5. **Submission flow**: PR-based or simple form → reviewer queue.
