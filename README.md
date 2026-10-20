# chains-poller — THE engine (runs on Railway)

**What this is:** the one always-on process for Chains. Railway project "bubbly-truth", service "worker", Python 3.12,
auto-deploys on every push to `main`. Replaced the GitHub Actions copies on 2026-10-20.

**It owns, in Firebase `chains-fantasy`:**
- `/live` — the current event's current round, every 25 s during play (what Live Chains draws from).
- `/rounds/<event>-r<n>`, `/rounds_index/<event>` — round archives and the per-event final/locked flags.
- `/field` — registered field for the draft event, with ratings.
- `/draft/<t>`, `/notifications` — the draft referee: window opens 36 h before the event, deadline = first published
  round-1 tee time (local time zone), no turn clock, opt-in auto-pick only, commissioner fills the rest.
- Pick scores in `/league/keys/picks~46~<t>` — from PDGA's **official results** endpoint (never the live archive).
  Provisional for 48 h after the event, then locked for good. Rows with `scr` or `lock1/lock2` are never touched.
- `/health` — heartbeat every cycle: `at`, `ok`, `msg`, `version`, `last_finalize_at`, `last_error`.

**Run modes:** `python poller.py` (service), `python poller.py once`, `python poller.py finalize`, `python poller.py draft`.
`DRY_RUN=1` logs every write instead of doing it — use it before any change to scoring logic.

**History:** v1 (June) live feed only → Sept: server-side scoring + draft referee → **v2 (2026-10-20)**: official-results
scoring, lock-before-rescore, tee-time deadline, no turn clock, health node. The 2026 scoring bugs it fixes are listed in
CHAINS_SYSTEM.md §6.
