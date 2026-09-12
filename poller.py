#!/usr/bin/env python3
"""
Chains - live tournament poller (Railway always-on service; also runnable
once-per-call from GitHub Actions via run_once()).

Polls the PDGA live feed every ~25 seconds and writes scores to Firebase.
- Current round -> /live  (with clean rounds_list + event_final flag).
- Every real round -> /rounds/{eventId}-r{N}  (so the app's round tabs work).
- Every COMPLETED past event is backfilled once into /rounds + /rounds_index
  (so the app can look back at any tournament, round by round).
- NEW 2026-09-12: once an event is final, the league's PICK SCORES for that
  event are filled in here, server-side (see finalize_picks). The app used to do
  this only in a browser, so if nobody opened the app - or one member never
  picked - the event never scored and the whole app stayed pinned to it.

The current event is chosen AUTOMATICALLY from the season schedule
(data/season.json in chains-dgpt-data) by start_date/end_date.

ROUND NUMBERING NOTE: PDGA does NOT number rounds 1..N. A Major reports
qualifying rounds 1,2,3 and then numbers the Finals "12" and a Playoff "13".
So round numbers are unreliable for "is it over." We publish a clean
rounds_list (real rounds + human labels) and decide an event is FINAL from the
schedule end_date + every player completed - never from a round number.
"""
import json, os, re, time, unicodedata, urllib.request
from datetime import datetime, timezone, timedelta

SEASON_URL = os.environ.get(
    "SEASON_URL",
    "https://raw.githubusercontent.com/Bonnaroo/chains-dgpt-data/main/data/season.json",
)
PLAYERS_URL = os.environ.get(
    "PLAYERS_URL",
    "https://raw.githubusercontent.com/Bonnaroo/chains-dgpt-data/main/data/mpo_players.json",
)
EVENT_ID_FALLBACK = os.environ.get("EVENT_ID", "97339")
FIREBASE_BASE = os.environ.get(
    "FIREBASE_URL",
    "https://chains-fantasy-default-rtdb.firebaseio.com",
).rstrip("/")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "25"))
LIVE_API = "https://www.pdga.com/apps/tournament/live-api"
HEADERS = {"User-Agent": "Mozilla/5.0"}

# Firebase auth: set FB_AUTH on Railway when the database is locked down.
# Empty (the default) is a no-op, so this is safe to deploy now and activate later.
FB_AUTH = os.environ.get("FB_AUTH", "")
def _auth():
    return f"?auth={FB_AUTH}" if FB_AUTH else ""

# Pick finalization knobs (mirror the app's autofinalize.js / engine.js).
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"          # log what would be written, write nothing
SKIP_SLOTS = {7}                                          # T7 unresolved per data contract (app skips it too)
DNF_PENALTY = 1                                           # missing/withdrawn pick = worst finisher + 1
FINALIZE_EVERY = int(os.environ.get("FINALIZE_EVERY", "300"))   # seconds between pick-finalize sweeps
GRACE_DAYS = 1   # keep polling an event this long past end_date until it is truly final (late/West-coast finishes)

def get(url, timeout=30):
    req = urllib.request.Request(url, headers=HEADERS)
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")

def put_firebase(path, data):
    if DRY_RUN:
        print(f"[dry-run] PUT {path} ({len(json.dumps(data))} bytes)")
        return
    url = f"{FIREBASE_BASE}/{path}.json{_auth()}"
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="PUT",
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30).read()

def get_firebase(path):
    """Read a Firebase path; None on miss/err (used for idempotent backfill checks)."""
    try:
        raw = get(f"{FIREBASE_BASE}/{path}.json{_auth()}")
        return json.loads(raw)
    except Exception:
        return None

def _sd(e): return e.get("start_date") or e.get("start")
def _ed(e): return e.get("end_date") or e.get("end")

def load_events():
    sched = json.loads(get(SEASON_URL))
    return [e for e in sched.get("events", []) if _sd(e)]

def _plus_days(iso, n):
    return (datetime.strptime(iso, "%Y-%m-%d").date() + timedelta(days=n)).isoformat()

def current_event():
    """Return the event RECORD live today (or next upcoming) from season.json.

    2026-09-12: an event stays "current" for GRACE_DAYS past its end_date until
    its rounds_index says final. Final rounds routinely end after 00:00 UTC
    (any US-evening finish), and the old rule switched to the next event at
    UTC midnight, freezing the last round's archive mid-play."""
    try:
        events = load_events()
        today = datetime.now(timezone.utc).date().isoformat()
        live = [e for e in events if _sd(e) <= today <= (_ed(e) or _sd(e))]
        if live:
            return live[0]
        # grace window: recently-ended but not yet indexed final
        for e in sorted(events, key=lambda e: _ed(e) or _sd(e), reverse=True):
            end = _ed(e) or _sd(e)
            if end < today <= _plus_days(end, GRACE_DAYS):
                idx = get_firebase(f"rounds_index/{e['event_id']}")
                if not (idx and idx.get("event_final") is True):
                    return e
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
    """EVERY scheduled round, in order, with labels + archive keys.

    2026-08-26 FIX: this used to skip any round with n > latest ("real rounds
    only"), so round_count was "rounds played so far" and the app read
    "Round 2 of 2" during a 5-round major. PDGA's RoundsList carries the full
    schedule up front - for Worlds that's 1,2,3,4 plus Finals numbered 12 - so
    the whole list is returned and each entry is flagged `played`. The app
    builds its round tabs from this list and already tolerates a tab whose
    archive isn't written yet (loadPastRound catches and marks it "none")."""
    rl = meta.get("RoundsList", {}) or {}
    try:
        nums = sorted(int(k) for k in rl.keys())
    except Exception:
        nums = list(range(1, int(meta.get("Rounds", 3)) + 1))
    if not nums:
        nums = list(range(1, int(meta.get("Rounds", 3)) + 1))
    out = []
    for n in nums:
        info = rl.get(str(n), {}) or {}
        out.append({
            "n": n,
            "label": info.get("Label", f"Round {n}"),
            "abbr": info.get("LabelAbbreviated", str(n)),
            "key": f"{event_id}-r{n}",
            "played": n <= latest,
        })
    if not out:
        out = [{"n": latest, "label": round_label(meta, latest),
                "abbr": str(latest), "key": f"{event_id}-r{latest}", "played": True}]
    return out

def played_rounds(rounds_list):
    """Only the rounds that were actually played. This is what goes into
    rounds_index: its LAST entry is the round pick scores are read from, so an
    unplayed scheduled Playoff/Finals must never appear there."""
    pl = [r for r in rounds_list if r.get("played", True)]
    return pl or rounds_list[-1:]

def fetch_event_meta(event_id):
    ev = json.loads(get(f"{LIVE_API}/live_results_fetch_event?TournID={event_id}&Division=MPO"))
    return ev.get("data", {})

def fetch_round(event_id, round_num, meta):
    rd = json.loads(get(f"{LIVE_API}/live_results_fetch_round?TournID={event_id}&Division=MPO&Round={round_num}"))
    rdata = rd.get("data", {})
    # 2026-08-26 FIX: `data` is a dict for a single-pool event but a LIST of pool
    # objects when the field is split across courses (Worlds 2026: pool A on
    # Black Locust, pool B on Toboggan). Assuming a dict crashed every cycle of
    # a multi-pool major, so /live froze on the last single-pool event.
    pools = rdata if isinstance(rdata, list) else [rdata]
    scores, pool_meta = [], []
    for pl in pools:
        if not isinstance(pl, dict):
            continue
        pname = pl.get("pool")
        course = None
        for L in (pl.get("layouts") or []):
            if isinstance(L, dict) and L.get("Name"):
                course = L.get("Name"); break
        # Carry the layout's units through. PDGA reports these courses in FEET;
        # the app defaults a hole with no `unit` to metres and then converts
        # again, so a 528 ft par 3 rendered as "528 m / 1732 ft".
        raw_units = None
        par_total = length_total = None
        for L in (pl.get("layouts") or []):
            if isinstance(L, dict) and L.get("Units"):
                raw_units = L.get("Units")
                par_total = L.get("Par")
                length_total = L.get("Length")
                break
        unit = "m" if str(raw_units or "").strip().lower().startswith("m") else "ft"
        pholes = [{"hole": h.get("Hole"), "par": h.get("Par"),
                   "length": h.get("Length"), "unit": unit}
                  for h in (pl.get("holes") or [])]
        pool_meta.append({"pool": pname, "course": course, "unit": unit,
                          "par_total": par_total, "length_total": length_total,
                          "holes": pholes})
        for s in (pl.get("scores") or []):
            s["_pool"] = pname
            scores.append(s)
    # `holes` keeps the first pool's layout for backward compatibility.
    holes = pool_meta[0]["holes"] if pool_meta else []
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
            "pool": p.get("_pool"),
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
        "holes": holes, "pools": pool_meta,
        "player_count": len(players), "players": players,
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

def write_rounds_index(event_id, event_name, rounds_list, rounds):
    put_firebase(f"rounds_index/{event_id}", {
        "event_id": event_id, "event_name": event_name,
        "rounds_list": played_rounds(rounds_list), "rounds": rounds,
        "event_final": True,
        "finalized_at": datetime.now(timezone.utc).isoformat(),
    })

def backfill_next_completed_event(today):
    """Archive ONE not-yet-indexed completed event per call, so each poll cycle stays
    light (never a long blocking sweep). Per-round resumable + resilient: skips rounds
    already saved and skips a round that errors. Returns True if it touched an event.

    The LAST played round is always re-fetched before the index is written, because
    its existing archive may be a mid-round snapshot from when /live moved on."""
    try:
        events = load_events()
    except Exception as e:
        print(f"[backfill] could not load schedule: {e}")
        return False
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
            pl = played_rounds(rl)
            if not pl or not fetch_round(eid, pl[-1]["n"], meta).get("players"):
                print(f"[backfill] event {eid} has no results yet; will retry")
                return True
            last_key = pl[-1]["key"]
            for r in pl:
                rkey = r["key"]
                if rkey != last_key and get_firebase(f"rounds/{rkey}") is not None:
                    continue               # resume: already archived (last round always refreshed)
                try:
                    put_firebase(f"rounds/{rkey}", fetch_round(eid, r["n"], meta))
                except Exception as e:
                    print(f"[backfill] {rkey} skipped: {e}")
            write_rounds_index(eid, meta.get("Name", ""), rl, meta.get("Rounds", 3))
            print(f"[backfill] archived event {eid} ({len(pl)} rounds)")
        except Exception as e:
            print(f"[backfill] event {eid} failed: {e}")
        return True                        # one event per call -> light cycles

def finalize_previous_round(event_id, meta, rounds_list, latest, live_payload):
    """If the round before `latest` (per rounds_list order) is archived with
    players still incomplete, re-fetch it and overwrite the archive. Cheap: one
    read per cycle, and one extra PDGA fetch only while the archive is short."""
    order = [r["n"] for r in rounds_list]
    if latest not in order or order.index(latest) == 0:
        return
    prev = order[order.index(latest) - 1]
    key = f"rounds/{event_id}-r{prev}"
    cur = get_firebase(key)
    players = (cur or {}).get("players") or []
    if players and all(p.get("completed") == 1 for p in players):
        return                              # already final - nothing to do
    rd = fetch_round(event_id, prev, meta)
    for k in ("rounds_list", "current_round", "current_round_label",
              "round_count", "round_index", "event_final"):
        if k in live_payload:
            rd[k] = live_payload[k]
    put_firebase(key, rd)
    done = sum(1 for p in rd["players"] if p.get("completed") == 1)
    print(f"[finalize] {key} refreshed ({done}/{len(rd['players'])} completed)")

# ---------------------------------------------------------------------------
# Pick-score finalization (server-side twin of the app's autofinalize.js)
# ---------------------------------------------------------------------------
# League data lives at /league (the app's sync snapshot: {app, fmt:2, rev, keys}).
# keys are "virtual keys" with "." encoded as "~46~": picks for tournament t are
# /league/keys/picks~46~{t} = {"r": <epoch ms>, "v": "<JSON array of rows>"}.
# Row: {slot, m, p1, s1, p2, s2, p1At, p2At}. The app merges by newest r and
# field-level (a populated value wins over null), so writing a row with the
# same names + filled scores is safe and idempotent.
#
# Rules (identical to the app):
#   - only events whose rounds_index says event_final
#   - never touch a score that is already set; never touch names
#   - score = the pro's event_to_par in the LAST PLAYED round's archive
#   - a named pick with no result (withdrew / not in field) = worst finisher + 1
#   - an EMPTY slot stays null (the app's scoring applies its own penalty)
#   - a single-pick week (settings.onepick_{t}) only scores p1
#   - T7 is skipped, as in the app

def _norm(s):
    s = unicodedata.normalize("NFD", str(s or "")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z ]", "", s).strip()
    return re.sub(r"\s+", " ", s)

def _fl(s):
    w = _norm(s).split()
    return (w[0] + " " + w[-1]) if len(w) >= 2 else _norm(s)

def _enc(vk):
    return vk.replace(".", "~46~")

def load_name_map():
    """name -> pdga from data/mpo_players.json (same list the app embeds)."""
    m = {}
    try:
        arr = json.loads(get(PLAYERS_URL))
        for p in arr if isinstance(arr, list) else arr.get("players", []):
            if p.get("pdga") is not None and p.get("name"):
                m[_norm(p["name"])] = str(p["pdga"])
                m[_fl(p["name"])] = str(p["pdga"])
    except Exception as e:
        print(f"[picks] name map unavailable ({e}); matching by name only")
    return m

def league_settings():
    rec = get_firebase(f"league/keys/{_enc('k.chains_dgpt_2026_settings_v1')}")
    try:
        return json.loads(rec["v"]) if rec and rec.get("v") else {}
    except Exception:
        return {}

def slot_for_event(events, event_id):
    for e in events:
        if str(e.get("event_id")) == str(event_id) and e.get("t") is not None:
            return int(e["t"])
    return None

def score_picks(picks, players, name_map, one_pick):
    """Return (filled_rows, changed). Only null scores on named picks are filled."""
    by_pdga, by_fl, worst = {}, {}, None
    for p in players:
        if p.get("pdga") is not None:
            by_pdga[str(p["pdga"])] = p
        if p.get("name"):
            by_fl[_fl(p["name"])] = p
        etp = p.get("event_to_par")
        if isinstance(etp, (int, float)) and (worst is None or etp > worst):
            worst = etp
    if worst is None:
        return picks, False
    dnf = int(worst) + DNF_PENALTY

    def score_for(name):
        if not name:
            return None
        pd = name_map.get(_norm(name)) or name_map.get(_fl(name))
        row = (pd and by_pdga.get(pd)) or by_fl.get(_fl(name))
        if row and isinstance(row.get("event_to_par"), (int, float)):
            return int(row["event_to_par"])
        return dnf   # in field but no result, or not found -> DNF rule

    out, changed = [], False
    for row in picks:
        r = dict(row) if isinstance(row, dict) else row
        if not isinstance(r, dict):
            out.append(r); continue
        if r.get("p1") and r.get("s1") is None:
            r["s1"] = score_for(r["p1"]); changed = True
        if not one_pick and r.get("p2") and r.get("s2") is None:
            r["s2"] = score_for(r["p2"]); changed = True
        out.append(r)
    return out, changed

def finalize_picks(events=None):
    """Sweep every final event; fill any missing pick scores. Returns count written."""
    events = events or load_events()
    idx = get_firebase("rounds_index") or {}
    settings = league_settings()
    name_map = None
    written = 0
    for eid, meta in idx.items():
        if not (isinstance(meta, dict) and meta.get("event_final") is True):
            continue
        t = slot_for_event(events, eid)
        if t is None or t in SKIP_SLOTS:
            continue
        rec = get_firebase(f"league/keys/{_enc('picks.' + str(t))}")
        if not rec or not rec.get("v"):
            continue
        try:
            picks = json.loads(rec["v"])
        except Exception:
            continue
        if not isinstance(picks, list) or not picks:
            continue
        one = bool(settings.get(f"onepick_{t}"))
        needs = any(isinstance(r, dict) and ((r.get("p1") and r.get("s1") is None) or
                    (not one and r.get("p2") and r.get("s2") is None)) for r in picks)
        if not needs:
            continue
        rl = played_rounds(meta.get("rounds_list") or [])
        if not rl:
            continue
        last = get_firebase(f"rounds/{rl[-1]['key']}")
        players = (last or {}).get("players") or []
        if not players or any(p.get("status") == "I" for p in players):
            print(f"[picks] T{t} ({eid}): last round not settled yet; skipping")
            continue
        if name_map is None:
            name_map = load_name_map()
        filled, changed = score_picks(picks, players, name_map, one)
        if not changed:
            continue
        now = int(time.time() * 1000)
        summary = ", ".join(f"{r.get('m')}:{r.get('s1')}/{r.get('s2')}" for r in filled if isinstance(r, dict))
        print(f"[picks] T{t} ({eid}) scored -> {summary}")
        put_firebase(f"league/keys/{_enc('picks.' + str(t))}",
                     {"r": now, "v": json.dumps(filled, separators=(",", ":"))})
        put_firebase("league/rev", now)
        written += 1
    return written

# ---------------------------------------------------------------------------

def run_once():
    """One poll cycle: read the schedule, fetch the live round, push to
    Firebase /live (+ archive the current round). Safe to call from cron/CI.
    Returns a short status string. Raises on hard failure."""
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
    try:
        put_firebase(f"rounds/{event_id}-r{latest}", live)
    except Exception as e:
        print(f"[archive] {e}")
    # 2026-08-29 FIX: a finished round's archive is only ever the LAST snapshot
    # taken while it was the live round. With sparse polling that snapshot could
    # be hours before the round ended (Worlds R1 archived with 52 players still
    # on the course, R2 with 80), so the app's past-round tabs showed partial
    # scores forever. Once the live round moves on, re-fetch the previous
    # scheduled round until every player is marked completed, then leave it.
    try:
        finalize_previous_round(event_id, meta, rounds_list, latest, live)
    except Exception as e:
        print(f"[finalize] {e}")
    # Index the event the moment it is final (the live snapshot just written IS
    # the final last-round archive), so pick scoring can run right away.
    try:
        if live.get("event_final") and not get_firebase(f"rounds_index/{event_id}"):
            write_rounds_index(event_id, live.get("event_name", ""), rounds_list, live.get("rounds", 3))
            print(f"[index] event {event_id} marked final")
    except Exception as e:
        print(f"[index] {e}")

    active = len([p for p in live["players"] if p["status"] == "I"])
    return (f"event {event_id} {live['current_round_label']} "
            f"({live['round_index']}/{live['round_count']}) "
            f"{live['player_count']} players, {active} on course"
            + (" [FINAL]" if live["event_final"] else ""))

def main():
    print(f"Chains poller starting. Schedule-driven, every {POLL_SECONDS}s -> {FIREBASE_BASE}/live (+ /rounds archive, pick scoring)")
    consecutive_errors = 0
    archived = set()
    last_finalize = 0
    while True:
        try:
            today = datetime.now(timezone.utc).date().isoformat()
            msg = run_once()
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}")
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            print(f"[error] {e} (#{consecutive_errors})")
            if consecutive_errors > 5:
                time.sleep(60)
            time.sleep(POLL_SECONDS)
            continue

        # Gentle backfill: archive at most ONE completed past event per cycle, so the
        # live view is never blocked by a long sweep (idempotent + resumable).
        try:
            backfill_next_completed_event(today)
        except Exception as e:
            print(f"[backfill] {e}")

        # Pick scoring sweep (cheap; every FINALIZE_EVERY seconds).
        if time.time() - last_finalize >= FINALIZE_EVERY:
            last_finalize = time.time()
            try:
                n = finalize_picks()
                if n:
                    print(f"[picks] wrote scores for {n} event(s)")
            except Exception as e:
                print(f"[picks] {e}")

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        print(run_once())
    elif len(sys.argv) > 1 and sys.argv[1] == "finalize":
        print(f"finalized {finalize_picks()} event(s)")
    else:
        main()
