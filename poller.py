#!/usr/bin/env python3
"""
Chains - live tournament poller (Railway always-on service).

Polls the PDGA live feed every ~25 seconds and writes scores to Firebase.
The current round goes to /live; every round (1..latest) is also archived to
/rounds/{eventId}-r{N} so the app's round tabs can show R1/R2/R3 even after they
finish. The app reads these in real time.

The current event is chosen AUTOMATICALLY from the season schedule
(data/season.json in chains-dgpt-data): whichever event is live today by date,
else the next upcoming one. EVENT_ID env var is only a fallback if the schedule
can't be loaded, so the live feed never breaks.
"""
import json, os, time, urllib.request
from datetime import datetime, timezone

SEASON_URL = os.environ.get(
    "SEASON_URL",
    "https://raw.githubusercontent.com/Bonnaroo/chains-dgpt-data/main/data/season.json",
)
EVENT_ID_FALLBACK = os.environ.get("EVENT_ID", "97339")
FIREBASE_BASE = os.environ.get(
    "FIREBASE_URL",
    "https://chains-fantasy-default-rtdb.firebaseio.com",
).rstrip("/")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "25"))
LIVE_API = "https://www.pdga.com/apps/tournament/live-api"
HEADERS = {"User-Agent": "Mozilla/5.0"}

def get(url, timeout=30):
    req = urllib.request.Request(url, headers=HEADERS)
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")

def put_firebase(path, data):
    url = f"{FIREBASE_BASE}/{path}.json"
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="PUT",
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30).read()

def current_event_id():
    """Event live today (or next upcoming) from season.json; EVENT_ID env is the fallback."""
    try:
        sched = json.loads(get(SEASON_URL))
        events = [e for e in sched.get("events", []) if e.get("start") and e.get("end")]
        today = datetime.now(timezone.utc).date().isoformat()
        live = [e for e in events if e["start"] <= today <= e["end"]]
        if live:
            return str(live[0]["event_id"])
        upcoming = sorted((e for e in events if e["start"] > today), key=lambda e: e["start"])
        if upcoming:
            return str(upcoming[0]["event_id"])
        if events:
            return str(sorted(events, key=lambda e: e["end"])[-1]["event_id"])
    except Exception as e:
        print(f"[schedule] could not load season.json ({e}); using EVENT_ID fallback")
    return EVENT_ID_FALLBACK

def fetch_event_meta(event_id):
    ev = json.loads(get(f"{LIVE_API}/live_results_fetch_event?TournID={event_id}&Division=MPO"))
    return ev.get("data", {})

def fetch_round(event_id, round_num, meta):
    rd = json.loads(get(f"{LIVE_API}/live_results_fetch_round?TournID={event_id}&Division=MPO&Round={round_num}"))
    rdata = rd.get("data", {})
    scores = rdata.get("scores", [])
    holes = [{"hole": h.get("Hole"), "par": h.get("Par"), "length": h.get("Length")}
             for h in rdata.get("holes", [])]
    players = []
    for p in scores:
        hs = p.get("HoleScores", [])
        pts = p.get("PlayerThrowStatus") or {}
        players.append({
            "name": p.get("Name"), "short": p.get("ShortName"),
            "pdga": p.get("PDGANum"), "place": p.get("RunningPlace"),
            "tied": p.get("Tied", False),
            "event_to_par": p.get("ToPar"), "round_to_par": p.get("RoundtoPar"),
            "thru": len([h for h in hs if h]), "hole_scores": hs,
            "status": p.get("RoundStatus"), "completed": p.get("Completed"),
            "card": p.get("CardNum"), "tee_time": p.get("TeeTime"),
            "cur_hole": pts.get("HoleOrdinal"), "cur_throw": pts.get("ThrowCount"),
            "cur_dist": pts.get("DistanceToTarget"), "cur_zone": pts.get("ZoneID"),
        })
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "event_id": event_id,
        "event_name": meta.get("Name", ""),
        "round": round_num,
        "latest_round": meta.get("LatestRound", 1),
        "highest_completed_round": meta.get("HighestCompletedRound", 0),
        "rounds": meta.get("Rounds", 3),
        "holes": holes, "player_count": len(players), "players": players,
    }

def main():
    print(f"Chains poller starting. Schedule-driven, every {POLL_SECONDS}s -> {FIREBASE_BASE}/live (+ /rounds archive)")
    consecutive_errors = 0
    archived = set()
    while True:
        try:
            event_id = current_event_id()
            meta = fetch_event_meta(event_id)
            latest = meta.get("LatestRound", 1)
            live = fetch_round(event_id, latest, meta)
            put_firebase("live", live)
            active = len([p for p in live["players"] if p["status"] == "I"])
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                  f"event {event_id} R{latest} {live['player_count']} players, {active} on course")
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            print(f"[error] {e} (#{consecutive_errors})")
            if consecutive_errors > 5:
                time.sleep(60)
            time.sleep(POLL_SECONDS)
            continue
        # Archive every round so the app's R1/R2/R3 tabs work (best-effort; never blocks /live).
        try:
            put_firebase(f"rounds/{event_id}-r{latest}", live)
            for n in range(1, latest):
                key = f"{event_id}-r{n}"
                if key not in archived:
                    put_firebase(f"rounds/{key}", fetch_round(event_id, n, meta))
                    archived.add(key)
        except Exception as e:
            print(f"[archive] {e}")
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
