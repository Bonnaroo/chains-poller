#!/usr/bin/env python3
"""
Chains - live tournament poller (Railway always-on service).

Polls the PDGA live feed every ~25 seconds and writes the live scores to
Firebase under /live. The app reads /live in real time. No git, no
collisions, near-real-time updates.

Only polls hard during tournament days/hours; idles otherwise to be gentle.
Set EVENT_ID via environment variable (or it defaults to current event).
"""
import json, os, time, urllib.request
from datetime import datetime, timezone

EVENT_ID = os.environ.get("EVENT_ID", "97339")
FIREBASE_BASE = os.environ.get(
    "FIREBASE_URL",
    "https://chains-fantasy-default-rtdb.firebaseio.com"
).rstrip("/")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "25"))
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

def fetch_live(event_id):
    base = "https://www.pdga.com/apps/tournament/live-api"
    ev = json.loads(get(f"{base}/live_results_fetch_event?TournID={event_id}&Division=MPO"))
    data = ev.get("data", {})
    latest = data.get("LatestRound", 1)
    rd = json.loads(get(f"{base}/live_results_fetch_round?TournID={event_id}&Division=MPO&Round={latest}"))
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
        "event_name": data.get("Name", ""),
        "latest_round": latest,
        "highest_completed_round": data.get("HighestCompletedRound", 0),
        "rounds": data.get("Rounds", 3),
        "holes": holes, "player_count": len(players), "players": players,
    }

def main():
    print(f"Chains poller starting. Event {EVENT_ID}, every {POLL_SECONDS}s -> {FIREBASE_BASE}/live")
    consecutive_errors = 0
    while True:
        try:
            live = fetch_live(EVENT_ID)
            put_firebase("live", live)
            active = len([p for p in live["players"] if p["status"] == "I"])
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                  f"R{live['latest_round']} {live['player_count']} players, {active} on course")
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            print(f"[error] {e} (#{consecutive_errors})")
            # back off if errors pile up
            if consecutive_errors > 5:
                time.sleep(60)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
