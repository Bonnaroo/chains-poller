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
        if r.get("scr"):
            out.append(r); continue   # scratched row: last place, 1 point, never scored
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
        needs = any(isinstance(r, dict) and not r.get("scr") and ((r.get("p1") and r.get("s1") is None) or
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
# Draft referee (2026-09-12) — the always-on process owns the draft clock.
# ---------------------------------------------------------------------------
# Why: picks used to depend on whoever had the app open. Nothing enforced turn
# order or time, the pool could include players who weren't playing, and the
# old in-browser autopick wrote picks nobody made. The referee below runs in
# this process against one source of truth (Firebase) and the app just shows it.
#
# Rules (league decision, Will, 2026-09-12):
#   - Window: opens start_date - DRAFT_OPEN_DAYS_BEFORE days at 00:00Z (Mon 8pm ET
#     for a Thursday event) and the hard deadline is start_date 00:00Z minus
#     DRAFT_DEADLINE_HOURS_BEFORE (Wed 8am ET). Both derive from the schedule, so a
#     Wednesday event shifts automatically. A draft never opens before the
#     previous event has ended.
#   - Snake order: last place in the previous scored event picks first; pick-2
#     round is reversed. (Same ranking as the app: place, total, season points.)
#   - Turn clock = time left to the deadline ÷ picks still to be made (floor
#     MIN_TURN_SECONDS). Everyone starts equal; the clock grows as people pick fast.
#   - Time out => the member drops one spot (swaps with the next pick in line).
#     Timing out in the very last spot => that pick is auto-picked. At the hard
#     deadline every empty slot is auto-picked.
#   - Auto-pick = highest-rated player in the registered field not yet taken.
#     Members can opt in per draft (/draft_prefs/{member}.autopick).
#   - Pool = the live registered PDGA field (with current ratings), refreshed
#     every FIELD_REFRESH_DRAFT seconds while a draft is open. A picked player
#     who leaves the field is flagged (w1/w2) and, once the deadline has passed
#     or the member is on auto-pick, replaced with the best available.
#
# Firebase:
#   /field                 registered field for the draft event (app pool)
#   /draft/{t}             the draft record (order, seq, clock, log)
#   /draft_prefs/{member}  {autopick: bool}
#   /notifications/{id}    same feed the app uses (on-the-clock, skipped, ...)
#   /league/keys/picks~46~{t}  the picks themselves (app sync format)

DRAFT_OPEN_DAYS_BEFORE = int(os.environ.get("DRAFT_OPEN_DAYS_BEFORE", "2"))
DRAFT_DEADLINE_HOURS_BEFORE = int(os.environ.get("DRAFT_DEADLINE_HOURS_BEFORE", "12"))
MIN_TURN_SECONDS = int(os.environ.get("MIN_TURN_SECONDS", str(15 * 60)))
FIELD_REFRESH_DRAFT = int(os.environ.get("FIELD_REFRESH_DRAFT", "600"))
FIELD_REFRESH_IDLE = int(os.environ.get("FIELD_REFRESH_IDLE", "3600"))
FEAT_API = "https://www.pdga.com/api/v1/feat"
ONE_PICK_SEED = {11}          # tournaments the app seeds as single-pick weeks
DEFAULT_MEMBERS = [{"id": "cory", "name": "Cory"}, {"id": "will", "name": "Will"}, {"id": "kyle", "name": "Kyle"},
                   {"id": "shanna", "name": "Shanna"}, {"id": "gabe", "name": "Gabe"}, {"id": "kadey", "name": "Kadey"}]

_field_cache = {"event_id": None, "fetched": 0, "data": None}
_name_map_cache = {"at": 0, "by_pdga": {}, "by_name": {}}

def _now_ms(): return int(time.time() * 1000)
def _iso(dt): return dt.astimezone(timezone.utc).isoformat()
def _parse_iso(s):
    try: return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception: return None
def _day0(iso_date):
    """start_date 'YYYY-MM-DD' -> that date at 00:00Z (the app's event-start instant)."""
    return datetime.strptime(iso_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

def league_key(vk):
    rec = get_firebase(f"league/keys/{_enc(vk)}")
    try:
        return json.loads(rec["v"]) if rec and rec.get("v") is not None else None
    except Exception:
        return None

def write_league_key(vk, value):
    now = _now_ms()
    put_firebase(f"league/keys/{_enc(vk)}", {"r": now, "v": json.dumps(value, separators=(",", ":"))})
    put_firebase("league/rev", now)

def league_members():
    m = league_key("k.chains_dgpt_2026_members_v1")
    out = [x for x in (m or []) if isinstance(x, dict) and x.get("id")]
    return out if len(out) >= 2 else DEFAULT_MEMBERS

def name_maps():
    """pdga -> app display name, and normalized name -> pdga (from mpo_players.json)."""
    if time.time() - _name_map_cache["at"] > 6 * 3600:
        by_pdga, by_name = {}, {}
        try:
            arr = json.loads(get(PLAYERS_URL))
            for p in arr if isinstance(arr, list) else arr.get("players", []):
                if p.get("pdga") is not None and p.get("name"):
                    by_pdga[str(p["pdga"])] = p["name"]
                    by_name[_norm(p["name"])] = str(p["pdga"]); by_name[_fl(p["name"])] = str(p["pdga"])
            _name_map_cache.update(at=time.time(), by_pdga=by_pdga, by_name=by_name)
        except Exception as e:
            print(f"[draft] name map unavailable ({e})")
    return _name_map_cache["by_pdga"], _name_map_cache["by_name"]

# ---- scoring port (only what draft order needs; mirrors engine.js) ---------
def score_event(picks, one_pick):
    """-> list of {m,total,place,points} or None if the event isn't scored yet."""
    if not picks: return None
    live = [r for r in picks if isinstance(r, dict) and not r.get("scr")]
    def has_real(r): return r.get("s1") is not None or (not one_pick and r.get("s2") is not None)
    if not live or not all(has_real(r) for r in live): return None
    vals = [s for r in live for s in ([r.get("s1")] if one_pick else [r.get("s1"), r.get("s2")]) if isinstance(s, (int, float))]
    if not vals: return None
    worst = max(vals)
    def sc(s): return s if isinstance(s, (int, float)) else worst + DNF_PENALTY
    rows = [{"m": r["m"], "total": sc(r.get("s1")) + (0 if one_pick else sc(r.get("s2")))} for r in live]
    rows.sort(key=lambda r: r["total"])
    n = len(picks)
    place, prev = {}, None
    for i, r in enumerate(rows):
        place[r["m"]] = place[prev["m"]] if prev and prev["total"] == r["total"] else i + 1
        prev = r
    out = [{"m": r["m"], "total": r["total"], "place": place[r["m"]], "points": max(n - place[r["m"]] + 1, 1)} for r in rows]
    out += [{"m": r["m"], "total": None, "place": n, "points": 1} for r in picks if isinstance(r, dict) and r.get("scr")]
    return out

def one_pick_for(t, settings):
    v = settings.get(f"onepick_{t}")
    return bool(v) if v is not None else (t in ONE_PICK_SEED)

def draft_order_for(t, events, members, settings):
    """Members in pick-1 order for event t (worst previous finish first)."""
    season_pts, prev_res = {}, None
    for e in sorted(events, key=lambda e: int(e.get("t") or 0)):
        et = int(e.get("t") or 0)
        if not et or et >= t: continue
        res = score_event(league_key(f"picks.{et}"), one_pick_for(et, settings))
        if not res: continue
        for r in res: season_pts[r["m"]] = season_pts.get(r["m"], 0) + r["points"]
        prev_res = res
    ids = [m["id"] for m in members]
    names = {m["id"]: m.get("name") or m["id"] for m in members}
    if prev_res:
        rows = sorted(prev_res, key=lambda r: (-r["place"], -(r["total"] if r["total"] is not None else 10**6),
                                                season_pts.get(r["m"], 0), names.get(r["m"], r["m"])))
        order = [r["m"] for r in rows if r["m"] in ids]
        return order + [i for i in ids if i not in order]
    return sorted(ids, key=lambda i: season_pts.get(i, 0))   # season opener: trailing member first

# ---- field ---------------------------------------------------------------
def fetch_field(event_id):
    """Registered MPO field with current PDGA ratings, in the app's field.json shape."""
    d = json.loads(get(f"{FEAT_API}/live-tournaments/{event_id}/event-division-results/MPO"))
    by_pdga, _ = name_maps()
    players = []
    for x in d.get("results", []):
        l = x.get("liveResult", {}) or {}
        fn, ln = (l.get("firstName") or "").strip(), (l.get("lastName") or "").strip()
        pdga = l.get("pdgaNum") or l.get("pdgaNumber")
        if not ln or any(w in (fn + " " + ln) for w in ("Exemption", "Qualifier", "Monday", "DGPT", "Event")):
            continue
        rating = ((x.get("ratingHistory") or {}).get("rating"))
        players.append({"firstName": fn, "lastName": ln, "pdgaNumber": pdga, "place": l.get("place", 0),
                        "rating": rating, "name": by_pdga.get(str(pdga)) or f"{fn} {ln}".strip()})
    return players

def refresh_field(event, draft_open):
    """Keep /field current for `event`; returns the player list (cached between refreshes)."""
    eid = str(event["event_id"])
    ttl = FIELD_REFRESH_DRAFT if draft_open else FIELD_REFRESH_IDLE
    if _field_cache["event_id"] == eid and time.time() - _field_cache["fetched"] < ttl and _field_cache["data"]:
        return _field_cache["data"]
    try:
        players = fetch_field(eid)
    except Exception as e:
        print(f"[field] fetch failed for {eid}: {e}")
        return _field_cache["data"] if _field_cache["event_id"] == eid else None
    if not players:
        print(f"[field] {eid}: empty field from PDGA; keeping previous")
        return _field_cache["data"] if _field_cache["event_id"] == eid else None
    prev = _field_cache["data"] if _field_cache["event_id"] == eid else None
    if prev and len(players) < 0.7 * len(prev):
        print(f"[field] {eid}: field shrank {len(prev)} -> {len(players)}; ignoring this pull")
        _field_cache["fetched"] = time.time()
        return prev
    _field_cache.update(event_id=eid, fetched=time.time(), data=players)
    put_firebase("field", {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "event_tag": f"T{event.get('t')}", "event_id": int(eid) if eid.isdigit() else eid,
        "event_name": event.get("name"), "player_count": len(players),
        "source": "poller/pdga-live", "players": players,
        "note": "Registered MPO field with current PDGA ratings. This is the draftable pool - nothing else is pickable.",
    })
    return players

# ---- draft record ---------------------------------------------------------
def draft_window(event, events):
    start = _day0(_sd(event))
    opens = start - timedelta(days=DRAFT_OPEN_DAYS_BEFORE)
    deadline = start - timedelta(hours=DRAFT_DEADLINE_HOURS_BEFORE)
    # never open while the previous event is still being played
    prev = [e for e in events if _ed(e) and _ed(e) < _sd(event)]
    if prev:
        prev_end = _day0(max(_ed(e) for e in prev)) + timedelta(days=1)   # end_date is inclusive
        if opens < prev_end: opens = prev_end
    if deadline <= opens + timedelta(hours=6):
        deadline = opens + timedelta(hours=6)
    return opens, deadline

def draft_events(events, now):
    """Events whose draft window is relevant right now (opens-30min .. deadline+2h)."""
    out = []
    for e in events:
        if not (_sd(e) and e.get("t") is not None): continue
        opens, deadline = draft_window(e, events)
        if opens - timedelta(minutes=30) <= now <= deadline + timedelta(hours=2):
            out.append((e, opens, deadline))
    return out

def blank_rows(order):
    return [{"slot": i + 1, "m": m, "p1": None, "s1": None, "p2": None, "s2": None, "p1At": None, "p2At": None}
            for i, m in enumerate(order)]

def new_record(t, event, opens, deadline, order, one_pick):
    seq = [{"m": m, "pick": 1} for m in order]
    if not one_pick:
        seq += [{"m": m, "pick": 2} for m in reversed(order)]
    for i, s in enumerate(seq):
        s.update(i=i, status="pending", started_at=None, deadline_at=None, done_at=None, player=None)
    return {"t": t, "event_id": str(event["event_id"]), "event_name": event.get("name"),
            "opens_at": _iso(opens), "deadline_at": _iso(deadline), "status": "open", "one_pick": one_pick,
            "order": order, "seq": seq, "cur": 0, "skips": {}, "log": [], "created_at": _iso(datetime.now(timezone.utc)),
            "rules": {"open_days_before": DRAFT_OPEN_DAYS_BEFORE, "deadline_hours_before": DRAFT_DEADLINE_HOURS_BEFORE,
                      "min_turn_seconds": MIN_TURN_SECONDS}}

def notify(nid, audience, title, body, link="picks", ntype="draft"):
    if get_firebase(f"notifications/{nid}") is not None:
        return
    put_firebase(f"notifications/{nid}", {"audience": audience, "title": title, "body": body,
                                         "created_at": datetime.now(timezone.utc).isoformat(), "link": link, "type": ntype})

def _log(rec, msg, **kw):
    rec.setdefault("log", []).append(dict(at=datetime.now(timezone.utc).isoformat(), msg=msg, **kw))
    rec["log"] = rec["log"][-80:]
    print(f"[draft] T{rec.get('t')}: {msg}")

def best_available(field, rows, one_pick):
    taken = set()
    for r in rows:
        for p in ("p1",) if one_pick else ("p1", "p2"):
            if r.get(p): taken.add(_fl(r[p]))
    pool = sorted((p for p in field if p.get("name") and _fl(p["name"]) not in taken),
                  key=lambda p: (-(p.get("rating") or 0), p["name"]))
    return pool[0] if pool else None

def picks_locked(event_id):
    live = get_firebase("live") or {}
    if str(live.get("event_id")) != str(event_id): return False
    if (live.get("highest_completed_round") or 0) >= 1: return True
    return any((p.get("thru") or 0) > 0 or p.get("status") == "I" for p in (live.get("players") or []))

def draft_tick(events=None, now=None):
    """Advance every relevant draft by one step of wall-clock. Cheap when idle."""
    events = events or load_events()
    now = now or datetime.now(timezone.utc)
    active = draft_events(events, now)
    if not active:
        # between drafts: keep /field pointed at the next event (hourly) so the app's
        # Registered list and the pool are never stale
        today = now.date().isoformat()
        upcoming = sorted((e for e in events if _sd(e) and _sd(e) >= today and e.get("t") is not None), key=_sd)
        if upcoming:
            try: refresh_field(upcoming[0], False)
            except Exception as e: print(f"[field] {e}")
        return 0
    settings = league_settings()
    members = league_members()
    names = {m["id"]: m.get("name") or m["id"] for m in members}
    touched = 0
    put_firebase("draft_status", {"at": _iso(now), "events": [int(e["t"]) for e, _, _ in active], "referee": "poller"})
    for event, opens, deadline in active:
        t = int(event["t"]); eid = str(event["event_id"])
        rec = get_firebase(f"draft/{t}")
        if rec and rec.get("demo"):
            rec = None   # a UI demo record is never a real draft - start fresh
        if rec and rec.get("status") == "closed":
            # still watch the field for withdrawals until the first throw
            field = refresh_field(event, False)
            if field and not picks_locked(eid):
                rows = league_key(f"picks.{t}") or []
                if rows and _handle_withdrawals(rec, rows, field, one_pick_for(t, settings), settings, names, force=True):
                    write_league_key(f"picks.{t}", rows); put_firebase(f"draft/{t}", rec)
            continue
        if now < opens:
            refresh_field(event, False)
            continue
        one = one_pick_for(t, settings)
        field = refresh_field(event, True) or []
        if not rec:
            order = draft_order_for(t, events, members, settings)
            rec = new_record(t, event, opens, deadline, order, one)
            _log(rec, "draft opened; order " + " > ".join(names.get(m, m) for m in order))
            notify(f"draft-{eid}-open", order, "Draft is open \U0001F94F",
                   f"Picks for {event.get('name')} are open. Deadline {deadline.strftime('%a %H:%M')} UTC. "
                   f"Order: {', '.join(names.get(m, m) for m in order)}.")
        rows = league_key(f"picks.{t}")
        seeded = False
        if not rows:
            rows = blank_rows(rec["order"]); seeded = True
        by_m = {r.get("m"): r for r in rows if isinstance(r, dict)}
        for m in rec["order"]:
            if m not in by_m:
                r = {"slot": len(rows) + 1, "m": m, "p1": None, "s1": None, "p2": None, "s2": None, "p1At": None, "p2At": None}
                rows.append(r); by_m[m] = r
        prefs = get_firebase("draft_prefs") or {}
        picks_changed = _handle_withdrawals(rec, rows, field, one, settings, names, force=(now >= deadline), prefs=prefs, now=now) or seeded
        rec_changed = picks_changed
        seq = rec["seq"]; cur = int(rec.get("cur") or 0)

        def remaining_steps(i):
            return sum(1 for s in seq[i:] if s["status"] in ("pending", "on_clock"))

        def do_autopick(step, how):
            nonlocal picks_changed
            row = by_m[step["m"]]; key = f"p{step['pick']}"
            best = best_available(field, rows, one)
            if not best:
                _log(rec, f"no available player to auto-pick for {names.get(step['m'])}", m=step["m"]); return False
            row[key] = best["name"]; row[key + "At"] = _now_ms()
            step.update(status=how, player=best["name"], done_at=_iso(now))
            picks_changed = True
            _log(rec, f"{names.get(step['m'])} pick {step['pick']}: {best['name']} ({how}, rating {best.get('rating')})", m=step["m"])
            notify(f"draft-{eid}-{step['m']}-p{step['pick']}-{how}", [step["m"]], "Auto-picked for you",
                   f"{best['name']} was picked for you ({'clock ran out' if how == 'timeout' else 'auto-pick'}) for {event.get('name')}.")
            return True

        guard = 0
        while cur < len(seq) and guard < 50:
            guard += 1
            step = seq[cur]; m = step["m"]; key = f"p{step['pick']}"
            row = by_m[m]
            if row.get(key):
                if step["status"] in ("pending", "on_clock"):
                    step.update(status="done", player=row[key], done_at=_iso(now)); rec_changed = True
                    _log(rec, f"{names.get(m)} picked {row[key]} (pick {step['pick']})", m=m)
                cur += 1; continue
            if now >= deadline:
                if not do_autopick(step, "deadline"): step.update(status="empty")
                rec_changed = True; cur += 1; continue
            if (prefs.get(m) or {}).get("autopick") and field:
                if do_autopick(step, "auto"): rec_changed = True; cur += 1; continue
            if step["status"] == "pending":
                share = max(MIN_TURN_SECONDS, (deadline - now).total_seconds() / max(1, remaining_steps(cur)))
                step.update(status="on_clock", started_at=_iso(now), deadline_at=_iso(now + timedelta(seconds=share)))
                rec_changed = True
                _log(rec, f"{names.get(m)} on the clock for pick {step['pick']} ({int(share // 60)} min)", m=m)
                notify(f"draft-{eid}-{m}-p{step['pick']}-clock-{step.get('i')}-{cur}", [m], "You're on the clock \U0001F94F",
                       f"Your pick {step['pick']} for {event.get('name')}: {int(share // 60)} minutes before you drop a spot.")
                break
            dl = _parse_iso(step.get("deadline_at"))
            if dl and now >= dl:
                rec["skips"][m] = int(rec["skips"].get(m, 0)) + 1
                # drop one spot = swap with the next pick that belongs to SOMEONE ELSE.
                # Nobody left behind you => you're in the last spot: your remaining
                # picks are auto-picked (best available), per league rule.
                j = next((k for k in range(cur + 1, len(seq)) if seq[k]["m"] != m), None)
                if j is None:
                    _log(rec, f"{names.get(m)} timed out in the last spot; auto-picking the rest", m=m)
                    for k in range(cur, len(seq)):
                        if seq[k]["m"] == m and not by_m[m].get(f"p{seq[k]['pick']}"):
                            do_autopick(seq[k], "timeout")
                    rec_changed = True; cur += 1; continue
                nxt = seq[j]
                seq[cur], seq[j] = nxt, step
                step.update(status="pending", started_at=None, deadline_at=None)
                nxt.update(status="pending", started_at=None, deadline_at=None)
                rec_changed = True
                _log(rec, f"{names.get(m)} timed out and drops a spot; {names.get(nxt['m'])} moves up", m=m)
                notify(f"draft-{eid}-{m}-skip-{rec['skips'][m]}", [m], "You missed your pick",
                       f"Your clock ran out for {event.get('name')}. You dropped one spot; {names.get(nxt['m'])} is up now.")
                continue
            break
        # hard deadline: nothing may stay empty (covers a slot cleared after its turn)
        if now >= deadline and field:
            for r in rows:
                if not isinstance(r, dict) or r.get("scr"): continue
                for n in (1,) if one else (1, 2):
                    if not r.get(f"p{n}"):
                        best = best_available(field, rows, one)
                        if not best: break
                        r[f"p{n}"] = best["name"]; r[f"p{n}At"] = _now_ms(); picks_changed = True
                        _log(rec, f"{names.get(r['m'])} pick {n} empty at deadline -> {best['name']}", m=r["m"])
        for i, s in enumerate(seq): s["i"] = i
        rec["cur"] = cur
        if cur >= len(seq) and rec.get("status") != "closed":
            rec["status"] = "closed"; rec["closed_at"] = _iso(now); rec_changed = True
            _log(rec, "draft complete")
            notify(f"draft-{eid}-complete", rec["order"], "Draft complete", f"All picks are in for {event.get('name')}.")
        if picks_changed:
            write_league_key(f"picks.{t}", rows)
        if rec_changed:
            put_firebase(f"draft/{t}", rec); touched += 1
    return touched

def _handle_withdrawals(rec, rows, field, one, settings, names, force, prefs=None, now=None):
    """Flag or replace picks whose player is no longer in the registered field.
    force=True (deadline passed / draft closed): replace immediately."""
    if not field or len(field) < 20:
        return False
    now = now or datetime.now(timezone.utc)
    in_field = {_fl(p["name"]) for p in field if p.get("name")}
    _, by_name = name_maps()
    field_pdga = {str(p.get("pdgaNumber")) for p in field}
    changed = False
    eid = rec.get("event_id")
    for r in rows:
        if not isinstance(r, dict) or r.get("scr"): continue
        for n in (1,) if one else (1, 2):
            p, w = f"p{n}", f"w{n}"
            name = r.get(p)
            if not name:
                continue
            pd = by_name.get(_norm(name)) or by_name.get(_fl(name))
            present = (pd in field_pdga) if pd else (_fl(name) in in_field)
            if present:
                if r.get(w): r.pop(w, None); changed = True
                continue
            auto = bool(((prefs or {}).get(r["m"]) or {}).get("autopick"))
            if force or auto:
                best = best_available(field, rows, one)
                if best:
                    _log(rec, f"{names.get(r['m'])}: {name} is out of the field -> replaced with {best['name']}", m=r["m"])
                    r[p] = best["name"]; r[p + "At"] = _now_ms(); r.pop(w, None); changed = True
                    notify(f"draft-{eid}-{r['m']}-{p}-wd-{_fl(name).replace(' ', '')}", [r["m"]], "Pick replaced",
                           f"{name} is no longer in the {rec.get('event_name')} field. You now have {best['name']}.")
            elif not r.get(w):
                r[w] = 1; changed = True
                _log(rec, f"{names.get(r['m'])}: {name} is out of the field - flagged for re-pick", m=r["m"])
                notify(f"draft-{eid}-{r['m']}-{p}-wdflag-{_fl(name).replace(' ', '')}", [r["m"]], "Your pick withdrew",
                       f"{name} is no longer in the {rec.get('event_name')} field. Pick someone else before the deadline or you'll get the best available.")
    return changed

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

        # Draft referee: clocks, skips, auto-picks, field refresh (no-op outside a draft window).
        try:
            draft_tick()
        except Exception as e:
            print(f"[draft] {e}")

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
    elif len(sys.argv) > 1 and sys.argv[1] == "draft":
        print(f"draft tick touched {draft_tick()} draft(s)")
    else:
        main()
