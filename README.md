# Chains Live Poller (Railway)

Always-on service that polls PDGA live scores every ~25 seconds and writes
them to Firebase /live. The app reads /live in real time.

## Files
- poller.py — the service
- Procfile — tells Railway to run it as a worker
- requirements.txt — (empty; uses Python stdlib only)
- runtime.txt — Python version

## Environment variables (set in Railway dashboard)
- EVENT_ID — current PDGA event (default 97339 = European Open)
- POLL_SECONDS — how often to poll (default 25)
- FIREBASE_URL — (optional) defaults to the chains-fantasy database

## To change events
Update the EVENT_ID variable in Railway when a new tournament starts.
