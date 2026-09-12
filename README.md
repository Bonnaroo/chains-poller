# Chains Live Poller (Railway)

Always-on service that polls PDGA live scores every ~25 seconds and writes
them to Firebase. The app reads Firebase in real time.

What it writes
- /live — the current round of the current event (chosen automatically from
  data/season.json in chains-dgpt-data by start/end date; no manual event id).
- /rounds/{eventId}-r{N} — every round of every event (archive).
- /rounds_index/{eventId} — written once an event is truly final.
- /league/keys/picks~46~{t} — pick SCORES for a final event (2026-09-12).
  The league's fantasy scoring no longer depends on someone opening the app:
  once an event is final, every named pick gets its score here, using the
  same rules as the app (last played round, missing result = worst + 1).

Files
- poller.py — the service (also `python poller.py once` for one cycle,
  `python poller.py finalize` for a pick-scoring sweep; DRY_RUN=1 to log only).
  The same file is mirrored as poller_once.py in chains-dgpt-data for the
  GitHub Actions fallback poller — keep them identical.
- Procfile — tells Railway to run it as a worker
- requirements.txt — (empty; uses Python stdlib only)
- runtime.txt — Python version

Environment variables (Railway dashboard)
- POLL_SECONDS — how often to poll (default 25)
- FINALIZE_EVERY — seconds between pick-scoring sweeps (default 300)
- FIREBASE_URL — (optional) defaults to the chains-fantasy database
- FB_AUTH — (optional) database secret, only once the DB is locked down
- DRY_RUN — set to 1 to log every write instead of performing it
