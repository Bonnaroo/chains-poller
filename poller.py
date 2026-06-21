#!/usr/bin/env python3
"""
Chains - live tournament poller (Railway always-on service).

Polls the PDGA live feed every ~25 seconds and writes scores to Firebase.
- Current round -> /live  (with clean rounds_list + event_final flag).
- Every real round -> /rounds/{eventId}-r{N}  (so the app's round tabs work).
- Every COMPLETED past event is backfilled once into /rounds + /rounds_index
  (so the app can look back at any tournament, round by round).

The current event is chosen AUTOMATICALLY from the season schedule
(data/season.json in chains-dgpt-data) by start_date/end_date.

ROUND NUMBERING NOTE: PDGA does NOT number rounds 1..N. A Major reports
qualifying rounds 1,2,3 and then numbers the Finals "12" and a Playoff "13".
So round numbers are unreliable for "is it over." We publish a clean
rounds_list (real rounds + human labels) and decide an event is FINAL from the
schedule end_date + every player completed - never from a round number.
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

def get_firebase(path):
    """Read a Firebase path; None on miss/err (used for idempotent backfill checks)."""
    try:
        raw = get(f"{FIREBASE_BASE}/{path}.json")
        return json.loads(raw)
    except Exception:
        return None

def _sd(e): return e.get("start_date") or e.get("start")
def _ed(e): return e.get("end_date") or e.get("end")

def load_events():
    sched = json.loads(get(SEASON_URL))
    return [e for e in sched.get("events", []) if _sd(e)]

def current_event():
    """Return the event RECORD live today (or next upcoming) from season.json."""
    try:
        events = load_events()
        today = datetime.now(timezone.utc).date().isoformat()
        live = [e for e in events if _sd(e) <= today <= (_ed(e) or _sd(e))]
        if live:
            return live[0]
        upcoming = sorted((e for e in events if _sd(e) > today), key=_sd)
        if upcoming:
            return upcoming[0]
        if events:
            return sorted(events, key=lambda e: _ed(e) or _sd(e))[-1]
    except Exception as e:
        print(f"[schedule] could not load season.json ({e}); using EVENT_ID fallback")
    return {"event_id": EVENT_ID_FALLBACK}

def round_label(meta, n):
    info = (meta.get("RoundsList", {}) or {}).get(str(n), {}) or {}
    return info.get("Label", f"Round {n}")

def build_rounds_list(meta, event_id, latest):
    """Real rounds only (n <= latest), in order, with labels + archive keys."""
    rl = meta.get("RoundsList", {}) or {}
    try:
        nums = sorted(int(k) for k in rl.keys())
    except Exception:
        nums = list(range(1, int(meta.get("Rounds", 3)) + 1))
    out = []
    for n in nums:
        if n > latest:
            continue
        info = rl.get(str(n), {}) or {}
        out.append({
            "n": n,
            "label": info.get("Label", f"Round {n}"),
            "abbr": info.get("LabelAbbreviated", str(n)),
            "key": f"{event_id}-r{n}",
        })
    if not out:
        out = [{"n": latest, "label": round_label(meta, latest),
                "abbr": str(latest), "key": f"{event_id}-r{latest}"}]
    return out

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
        "round_label": round_label(meta, round_num),
        "latest_round": meta.get("LatestRound", 1),
        "highest_completed_round": meta.get("HighestCompletedRound", 0),
        "rounds": meta.get("Rounds", 3),
        "holes": holes, "player_count": len(players), "players": players,
    }

def is_final(end_date, today, latest, highest_completed, players):
    """Truly-final signal: end_date reached AND no one still on the course AND the
    latest round is fully complete. Never trusts a round number alone."""
    if not end_date or today < end_date:
        return False
    if not players:
        return False
    if any(p.get("status") == "I" for p in players):
        return False
    return highest_completed >= latest

def backfill_completed_events(today):
    """One-time, idempotent: archive every past event's rounds + write a rounds_index
    so the app can review any finished tournament. Skips events already archived."""
    try:
        events = load_events()
    except Exception as e:
        print(f"[backfill] could not load schedule: {e}")
        return
    for rec in events:
        end = _ed(rec)
        if not end or end >= today:        # only fully-finished events
            continue
        eid = str(rec["event_id"])
        if get_firebase(f"rounds_index/{eid}"):   # already done
            continue
        try:
            meta = fetch_event_meta(eid)
            latest = meta.get("LatestRound", 1)
            rl = build_rounds_list(meta, eid, latest)
            for r in rl:
                put_firebase(f"rounds/{eid}-r{r['n']}", fetch_round(eid, r["n"], meta))
            put_firebase(f"rounds_index/{eid}", {
                "event_id": eid, "event_name": meta.get("Name", ""),
                "rounds_list": rl, "rounds": meta.get("Rounds", 3),
                "event_final": True,
                "finalized_at": datetime.now(timezone.utc).isoformat(),
            })
            print(f"[backfill] archived event {eid} ({len(rl)} rounds)")
        except Exception as e:
            print(f"[backfill] event {eid} failed: {e}")

def main():
    print(f"Chains poller starting. Schedule-driven, every {POLL_SECONDS}s -> {FIREBASE_BASE}/live (+ /rounds archive)")
    consecutive_errors = 0
    archived = set()
    did_backfill = False
    while True:
        try:
            today = datetime.now(timezone.utc).date().isoformat()
            rec = current_event()
            event_id = str(rec["event_id"])
            meta = fetch_event_meta(event_id)
            latest = meta.get("LatestRound", 1)
            rounds_list = build_rounds_list(meta, event_id, latest)

            live = fetch_round(event_id, latest, meta)
            live["rounds_list"] = rounds_list
            live["current_round"] = latest
            live["current_round_label"] = round_label(meta, latest)
            live["round_count"] = len(rounds_list)
            live["round_index"] = next((i + 1 for i, r in enumerate(rounds_list)
                                        if r["n"] == latest), len(rounds_list))
            live["event_final"] = is_final(_ed(rec), today, latest,
                                           live["highest_completed_round"], live["players"])
            put_firebase("live", live)

            active = len([p for p in live["players"] if p["status"] == "I"])
            flag = " [FINAL]" if live["event_final"] else ""
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                  f"event {event_id} {live['current_round_label']} "
                  f"({live['round_index']}/{live['round_count']}) "
                  f"{live['player_count']} players, {active} on course{flag}")
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            print(f"[error] {e} (#{consecutive_errors})")
            if consecutive_errors > 5:
                time.sleep(60)
            time.sleep(POLL_SECONDS)
            continue

        # Archive the live event's rounds (current keeps updating; finished ones once).
        try:
            put_firebase(f"rounds/{event_id}-r{latest}", live)
            for r in rounds_list:
                if r["n"] == latest:
                    continue
                key = r["key"]
                if key not in archived:
                    put_firebase(f"rounds/{key}", fetch_round(event_id, r["n"], meta))
                    archived.add(key)
            if live.get("event_final") and not get_firebase(f"rounds_index/{event_id}"):
                put_firebase(f"rounds_index/{event_id}", {
                    "event_id": event_id, "event_name": live.get("event_name", ""),
                    "rounds_list": rounds_list, "rounds": live.get("rounds", 3),
                    "event_final": True,
                    "finalized_at": datetime.now(timezone.utc).isoformat(),
                })
        except Exception as e:
            print(f"[archive] {e}")

        # One-time backfill of all completed past events (idempotent; after the first
        # fresh /live write so the live view is never delayed by it).
        if not did_backfill:
            try:
                backfill_completed_events(today)
            except Exception as e:
                print(f"[backfill] {e}")
            did_backfill = True

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
