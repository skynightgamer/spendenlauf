from fastapi import FastAPI, HTTPException, Response, Cookie, Depends, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from typing import Optional, List
import sqlite3
import os
import json
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

# .env loader
def load_env():
    """Read key=value pairs from a sibling .env file into os.environ."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

load_env()

# ── Configuration ──────────────────────────────────────────────
CHECKPOINT_COUNT = 5          # Stations around the loop
COOLDOWN_SECONDS = 180        # 3 min per-station cooldown
LAP_DISTANCE_KM  = 2.0        # Loop length in km
DONATION_GOAL    = 0.0        # Fundraising target in €; 0 hides the dashboard goal bar
# Pace is based on "moving time": gaps between scans longer than this are
# treated as a rest (the runner left the loop for the field) and excluded.
# Normal loop segments are well under this; a rest excursion is far longer.
MOVING_GAP_MAX_SECONDS = 300  # 5 min
DATABASE          = "spendenlauf.db"

# A station is considered "dark" if nothing (heartbeat or scan) has arrived from
# it within this window. Stations heartbeat every ~20s, so this tolerates a
# couple of missed beats / a brief reconnect before flagging.
STATION_OFFLINE_SECONDS = 75

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SESSION_TTL    = timedelta(hours=12)
COOKIE_SECURE  = os.environ.get("COOKIE_SECURE", "true").strip().lower() != "false"

# Pre-shared key the ESP32 stations send on every ingestion call. Without it,
# /api/scan and the beacon whitelist are open to anyone who knows the URL, so a
# spoofed POST can inflate any runner's laps/donations.
STATION_API_KEY = os.environ.get("STATION_API_KEY", "")

# Default station placement: a loop around the Schyrenbad / Sachsenstraße, München-Au.
DEFAULT_STATIONS = [
    (1, "Station 1", 48.11707, 11.56193),
    (2, "Station 2", 48.11374, 11.56096),
    (3, "Station 3", 48.11161, 11.56136),
    (4, "Station 4", 48.11051, 11.56398),
    (5, "Station 5", 48.11214, 11.56304),
]

# In-memory session store: token → expiry. Cleared on server restart.
SESSIONS: dict[str, datetime] = {}

# App setup
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown hook"""
    init_db()
    sync_stations(get_config("checkpoint_count"))
    print("✔ Database initialised")
    print("✔ Dashboard at http://localhost:8000")
    yield

# Interactive API docs are disabled in production. re-enable by passing docs_url="/docs"
app = FastAPI(
    title="Spendenlauf Tracker",
    docs_url=None, redoc_url=None, openapi_url=None,
    lifespan=lifespan,
)
# Dashboard, admin panel and ESP32 stations all talk to this server directly
# (same-origin or non-browser clients), so no cross-origin access needed.

# Database helpers
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS runners (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            vorname         TEXT    NOT NULL,
            nachname        TEXT    NOT NULL,
            bib_number      INTEGER UNIQUE NOT NULL,
            donation_per_km REAL    DEFAULT 0.0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS beacons (
            bib_number  INTEGER PRIMARY KEY,
            beacon_mac  TEXT UNIQUE NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS scan_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id  INTEGER NOT NULL,
            beacon_mac  TEXT    NOT NULL,
            timestamp   TEXT    NOT NULL,
            rssi        INTEGER NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS runner_progress (
            runner_id       INTEGER PRIMARY KEY,
            next_checkpoint INTEGER DEFAULT 1,
            laps_completed  INTEGER DEFAULT 0,
            last_station_id INTEGER,
            last_seen_time  TEXT,
            FOREIGN KEY (runner_id) REFERENCES runners(id)
        )
    """)

    # Pace computation walks every scan for a beacon ordered by time.
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_scan_beacon "
        "ON scan_events (beacon_mac, timestamp)"
    )

    c.execute("""
        CREATE TABLE IF NOT EXISTS cooldown_tracker (
            runner_id      INTEGER NOT NULL,
            station_id     INTEGER NOT NULL,
            last_scan_time TEXT    NOT NULL,
            PRIMARY KEY (runner_id, station_id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS stations (
            id        INTEGER PRIMARY KEY,
            name      TEXT NOT NULL,
            latitude  REAL,
            longitude REAL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # Liveness per station. Updated on every heartbeat and every scan so the
    # dashboard can show, at a glance, which checkpoints have gone dark — a
    # station with no runner nearby still heartbeats, so silence means trouble.
    c.execute("""
        CREATE TABLE IF NOT EXISTS station_status (
            station_id INTEGER PRIMARY KEY,
            last_seen  TEXT    NOT NULL,
            queued     INTEGER DEFAULT 0,
            source     TEXT
        )
    """)

    # Archived runs. When a run ends (or a new one is started over un-ended data)
    # a full snapshot — final stats, leaderboard, teams, the event times — is
    # frozen here as JSON so past results survive the wipe that a new run does.
    c.execute("""
        CREATE TABLE IF NOT EXISTS past_runs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            label      TEXT    NOT NULL,
            started_at TEXT,
            ended_at   TEXT,
            created_at TEXT    NOT NULL,
            data       TEXT    NOT NULL
        )
    """)

    # Seed one row per checkpoint with default coordinates near the Schyrenbad.
    for sid, name, lat, lng in DEFAULT_STATIONS:
        c.execute(
            "INSERT OR IGNORE INTO stations (id, name, latitude, longitude) "
            "VALUES (?, ?, ?, ?)",
            (sid, name, lat, lng),
        )

    # Migration: optional team/class a runner belongs to, for the team leaderboard.
    cols = [r[1] for r in c.execute("PRAGMA table_info(runners)")]
    if "team" not in cols:
        c.execute("ALTER TABLE runners ADD COLUMN team TEXT NOT NULL DEFAULT ''")

    conn.commit()
    conn.close()


def sync_stations(n: int) -> None:
    """Make the stations table hold exactly checkpoints 1..n.

    Adds any missing checkpoint (default name, NULL coords so the map spreads it
    near centre until placed) and drops any station beyond n. Keeps the table —
    which drives the map markers and the liveness strip — in step with the
    configurable checkpoint_count.
    """
    conn = get_db()
    c = conn.cursor()
    for sid in range(1, n + 1):
        c.execute(
            "INSERT OR IGNORE INTO stations (id, name) VALUES (?, ?)",
            (sid, f"Station {sid}"),
        )
    c.execute("DELETE FROM stations       WHERE id > ?",         (n,))
    c.execute("DELETE FROM station_status WHERE station_id > ?", (n,))
    conn.commit()
    conn.close()


# Settings helpers
def get_setting(key: str, default=None):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return json.loads(row["value"]) if row else default


def set_setting(key: str, value) -> None:
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, json.dumps(value)),
    )
    conn.commit()
    conn.close()


def touch_station(c: sqlite3.Cursor, station_id: int,
                  queued: Optional[int], source: str) -> None:
    """Mark a station alive *now* using an already-open cursor (caller commits).

    `queued` is the station's flash-buffer depth; pass None to leave the stored
    value untouched (e.g. a scan, which doesn't carry buffer depth).
    """
    now_iso = datetime.now().isoformat()
    c.execute(
        "INSERT INTO station_status (station_id, last_seen, queued, source) "
        "VALUES (?, ?, COALESCE(?, 0), ?) "
        "ON CONFLICT(station_id) DO UPDATE SET "
        "    last_seen = excluded.last_seen, "
        "    queued    = COALESCE(?, station_status.queued), "
        "    source    = excluded.source",
        (station_id, now_iso, queued, source, queued),
    )


# Runtime-tunable settings, editable from the admin panel.
# key -> (default, caster, min, max). Stored in `settings` as "cfg_<key>"; the
# module constants above are the defaults when nothing has been saved yet.
CONFIG_SPEC = {
    "checkpoint_count":       (CHECKPOINT_COUNT,       int,   2,   20),
    "cooldown_seconds":       (COOLDOWN_SECONDS,       int,   0,   3600),
    "lap_distance_km":        (LAP_DISTANCE_KM,        float, 0.1, 100.0),
    "moving_gap_max_seconds": (MOVING_GAP_MAX_SECONDS, int,   30,  3600),
    "donation_goal":          (DONATION_GOAL,          float, 0.0, 1_000_000.0),
}


def get_config(key: str):
    """Current value of a tunable setting (saved override, else the default)."""
    default, cast, _, _ = CONFIG_SPEC[key]
    val = get_setting("cfg_" + key, None)
    if val is None:
        return default
    try:
        return cast(val)
    except (TypeError, ValueError):
        return default


# Admin authentication (cookie session)
def create_session() -> str:
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = datetime.now(timezone.utc) + SESSION_TTL
    return token

def session_valid(token: Optional[str]) -> bool:
    if not token:
        return False
    expiry = SESSIONS.get(token)
    if not expiry:
        return False
    if datetime.now(timezone.utc) > expiry:
        SESSIONS.pop(token, None)
        return False
    return True

def require_admin(session: Optional[str] = Cookie(default=None)) -> bool:
    if not session_valid(session):
        raise HTTPException(401, detail="Not authenticated")
    return True


def require_station(x_api_key: Optional[str] = Header(default=None)) -> bool:
    """Guard station-only endpoints with the pre-shared STATION_API_KEY."""
    if not STATION_API_KEY:
        # Fail closed: an unset key would otherwise leave ingestion wide open.
        raise HTTPException(503, detail="STATION_API_KEY not configured in .env")
    if not x_api_key or not secrets.compare_digest(x_api_key, STATION_API_KEY):
        raise HTTPException(401, detail="Invalid or missing station key")
    return True


# Pydantic models
class ScanEvent(BaseModel):
    station_id: int                          # 1–5
    beacon_mac: str                          # e.g. "AA:BB:CC:DD:EE:01"
    rssi:       int = Field(ge=-100, le=0)    # plausible BLE range; rejects fabricated values
    # Seconds elapsed between the detection and this POST. Live scans send 0 (or
    # omit it); a scan that was buffered on the station's flash during a Wi-Fi /
    # server outage sends how old it is, so the server can reconstruct the real
    # detection time instead of stamping the (much later) moment it drained the
    # buffer. Capped at 24h so a forged value can only backdate within the event.
    age_s:      int = Field(default=0, ge=0, le=86400)


class Heartbeat(BaseModel):
    station_id: int
    queued:     int = Field(default=0, ge=0)   # scans waiting in the station's flash buffer

class RunnerCreate(BaseModel):
    vorname:         str
    nachname:        str
    bib_number:      int
    donation_per_km: float = 0.0
    team:            str   = ""

class RunnerUpdate(BaseModel):
    vorname:         Optional[str]   = None
    nachname:        Optional[str]   = None
    bib_number:      Optional[int]   = None
    donation_per_km: Optional[float] = None
    team:            Optional[str]   = None

class BeaconCreate(BaseModel):
    bib_number: int
    beacon_mac: str

class BeaconUpdate(BaseModel):
    bib_number: Optional[int] = None
    beacon_mac: Optional[str] = None

class AdminLogin(BaseModel):
    username: str
    password: str

class StationUpdate(BaseModel):
    id:        int
    name:      Optional[str]   = None
    latitude:  Optional[float] = None
    longitude: Optional[float] = None

class RouteUpdate(BaseModel):
    route:          List[List[float]]        # ordered [lat, lng] points
    map_view:       Optional[dict]          = None  # {"center": [lat,lng], "zoom": int}
    segments:       Optional[List[float]]   = None  # leg distances in m: 1→2,2→3,…,N→1
    lap_distance_m: Optional[float]         = None  # full lap length in metres

class ConfigUpdate(BaseModel):
    checkpoint_count:       Optional[int]   = None
    cooldown_seconds:       Optional[int]   = None
    lap_distance_km:        Optional[float] = None
    moving_gap_max_seconds: Optional[int]   = None
    donation_goal:          Optional[float] = None
    # ISO datetimes (e.g. "2026-06-24T16:00"); "" clears them. event_start is the
    # official opening: scans before it are ignored. event_end drives the
    # dashboard projection of the final totals.
    event_start:            Optional[str]   = None
    event_end:              Optional[str]   = None


# Admin auth endpoints
@app.post("/api/login")
def login(creds: AdminLogin, response: Response):
    if not ADMIN_PASSWORD:
        raise HTTPException(500, detail="ADMIN_PASSWORD not configured in .env")
    ok = (
        secrets.compare_digest(creds.username, ADMIN_USERNAME)
        and secrets.compare_digest(creds.password, ADMIN_PASSWORD)
    )
    if not ok:
        raise HTTPException(401, detail="Falscher Benutzername oder Passwort")
    token = create_session()
    response.set_cookie(
        "session", token,
        httponly=True, samesite="strict", secure=COOKIE_SECURE,
        max_age=int(SESSION_TTL.total_seconds()),
    )
    return {"status": "ok"}


@app.post("/api/logout")
def logout(response: Response, session: Optional[str] = Cookie(default=None)):
    if session:
        SESSIONS.pop(session, None)
    response.delete_cookie(
        "session", httponly=True, samesite="strict", secure=COOKIE_SECURE,
    )
    return {"status": "ok"}


@app.get("/api/auth")
def auth_status(session: Optional[str] = Cookie(default=None)):
    return {"authenticated": session_valid(session)}


# Stations & route
@app.get("/api/stations")
def get_stations():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, latitude, longitude FROM stations ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.put("/api/stations")
def update_stations(stations: List[StationUpdate], _: bool = Depends(require_admin)):
    conn = get_db()
    for s in stations:
        conn.execute(
            "UPDATE stations SET name = COALESCE(?, name), "
            "latitude = ?, longitude = ? WHERE id = ?",
            (s.name, s.latitude, s.longitude, s.id),
        )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.get("/api/route")
def get_route():
    return {
        "route":          get_setting("route", []),
        "map_view":       get_setting("map_view", None),
        "segments":       get_setting("segments", None),
        "lap_distance_m": get_setting("lap_distance_m", None),
    }


@app.put("/api/route")
def update_route(data: RouteUpdate, _: bool = Depends(require_admin)):
    set_setting("route", data.route)
    if data.map_view is not None:
        set_setting("map_view", data.map_view)
    if data.segments is not None:
        set_setting("segments", data.segments)
    if data.lap_distance_m is not None:
        set_setting("lap_distance_m", data.lap_distance_m)
    return {"status": "ok"}


def config_payload() -> dict:
    """All tunable settings plus the (non-numeric) event start/end times."""
    cfg = {k: get_config(k) for k in CONFIG_SPEC}
    cfg["event_start"]   = get_setting("event_start", None)
    cfg["event_end"]     = get_setting("event_end", None)
    cfg["event_stopped"] = get_setting("event_stopped", None)
    return cfg


@app.get("/api/config")
def get_config_all():
    return config_payload()


@app.put("/api/config")
def update_config(data: ConfigUpdate, _: bool = Depends(require_admin)):
    fields = data.model_dump(exclude_unset=True)

    # event_start / event_end are datetime strings, not numeric tunables — handle
    # them apart from the CONFIG_SPEC loop. An empty string clears the setting.
    for dkey in ("event_start", "event_end"):
        if dkey not in fields:
            continue
        val = fields.pop(dkey)
        if val:
            try:
                datetime.fromisoformat(val)
            except (TypeError, ValueError):
                raise HTTPException(400, detail=f"{dkey}: ungültiges Datum")
            set_setting(dkey, val)
        else:
            set_setting(dkey, None)

    for key, val in fields.items():
        if val is None:
            continue
        _, cast, lo, hi = CONFIG_SPEC[key]
        try:
            v = cast(val)
        except (TypeError, ValueError):
            raise HTTPException(400, detail=f"{key}: ungültiger Wert")
        if not (lo <= v <= hi):
            raise HTTPException(
                400, detail=f"{key}: muss zwischen {lo} und {hi} liegen")
        set_setting("cfg_" + key, v)

    # Keep the stations table (map markers + liveness strip) in step with the
    # configured checkpoint count when it changed.
    if "checkpoint_count" in fields and fields["checkpoint_count"] is not None:
        sync_stations(get_config("checkpoint_count"))
    return {"status": "ok", "config": config_payload()}


# Scan ingestion + lap reconstruction
@app.post("/api/scan")
def ingest_scan(scan: ScanEvent, _: bool = Depends(require_station)):
    """
    Accept a scan event from an authenticated ESP32 station.

    The server stamps every event with its own receive time, so a leaked or
    forged timestamp can't be spaced to slip past the per-station cooldown and
    pace can't be faked. Stations sit on the same LAN, so receive time ≈ the
    true detection time.

    Lap logic (lenient). A lap is the full loop 1 → 2 → … → N → 1, so it is
    counted when the runner crosses the start line (station 1) again, not when
    they reach the last station. This keeps lap count and the distance estimate
    (which measures progress from station 1) consistent.
      - Track next_checkpoint per runner. After station 1 it points at 2, and
        advances cyclically 2 → 3 → … → N → 1.
      - Scan matches next_checkpoint → advance.
      - Scan is *ahead* of next_checkpoint → accept (skip gap), advance.
      - Re-crossing station 1 after any forward progress → lap complete.
      - If we were expecting station 1 but the runner reappears further along,
        they crossed the line undetected → still count the lap.
      - Out-of-order / behind / re-scan at the start → ignored.
    """
    conn = get_db()
    c = conn.cursor()
    mac = scan.beacon_mac.upper()

    # Authoritative server-side timestamp (naive local time). For a buffered
    # scan the station reports how many seconds ago it was actually detected, so
    # we subtract that to recover the true detection time. Live scans send
    # age_s == 0, leaving this at "now". age_s is bounded by the model, so the
    # most a station can do is backdate within the event window.
    scan_time = datetime.now() - timedelta(seconds=scan.age_s)
    ts_iso = scan_time.isoformat()

    # Reject a station_id outside the configured range (1..checkpoint_count). A
    # misflashed board (STATION_ID=0) or a stale one left on an id we've since
    # dropped would otherwise be stored and corrupt the modular lap math.
    checkpoints = get_config("checkpoint_count")
    if not (1 <= scan.station_id <= checkpoints):
        conn.commit(); conn.close()
        return {"status": "ignored", "reason": "invalid_station"}

    # A scan proves the station is alive; refresh its liveness (with its buffer
    # depth) even when this particular event is later ignored as a duplicate.
    touch_station(c, scan.station_id, queued=None, source="scan")

    # Official start gate: before the run is opened, scans are setup/hand-out/test
    # noise. Keep the station marked alive (above) but don't record or count them.
    event_start = get_setting("event_start", None)
    if not event_start:
        conn.commit(); conn.close()
        return {"status": "ignored", "reason": "not_started"}
    try:
        if scan_time < datetime.fromisoformat(event_start):
            conn.commit(); conn.close()
            return {"status": "ignored", "reason": "not_started"}
    except ValueError:
        # Fail closed if a legacy/corrupt start value cannot be parsed. Setup
        # scans must never become run data merely because the start is invalid.
        conn.commit(); conn.close()
        return {"status": "ignored", "reason": "not_started"}

    # End gate: once the run has been stopped it's over — scans detected after
    # the stop time are stragglers/teardown and don't count.
    event_stopped = get_setting("event_stopped", None)
    if event_stopped:
        try:
            if scan_time >= datetime.fromisoformat(event_stopped):
                conn.commit(); conn.close()
                return {"status": "ignored", "reason": "ended"}
        except ValueError:
            pass

    # store raw event (useful for post-hoc analysis)
    c.execute(
        "INSERT INTO scan_events (station_id, beacon_mac, timestamp, rssi) "
        "VALUES (?, ?, ?, ?)",
        (scan.station_id, mac, ts_iso, scan.rssi),
    )

    # identify runner (beacon MAC -> bib number -> runner)
    c.execute(
        "SELECT r.id FROM runners r "
        "JOIN beacons b ON r.bib_number = b.bib_number "
        "WHERE b.beacon_mac = ?",
        (mac,),
    )
    row = c.fetchone()
    if not row:
        conn.commit(); conn.close()
        return {"status": "ignored", "reason": "unknown_beacon"}
    runner_id = row["id"]

    # cooldown check (against the authoritative server clock)
    c.execute(
        "SELECT last_scan_time FROM cooldown_tracker "
        "WHERE runner_id = ? AND station_id = ?",
        (runner_id, scan.station_id),
    )
    cd = c.fetchone()
    if cd:
        last = datetime.fromisoformat(cd["last_scan_time"])
        if (scan_time - last).total_seconds() < get_config("cooldown_seconds"):
            conn.commit(); conn.close()
            return {"status": "ignored", "reason": "cooldown"}

    # update cooldown timestamp
    c.execute(
        "INSERT OR REPLACE INTO cooldown_tracker "
        "(runner_id, station_id, last_scan_time) VALUES (?, ?, ?)",
        (runner_id, scan.station_id, ts_iso),
    )

    # progress / lap reconstruction
    c.execute(
        "SELECT * FROM runner_progress WHERE runner_id = ?", (runner_id,)
    )
    prog = c.fetchone()

    if not prog:
        # first-ever scan for this runner: start tracking from where they were
        # first seen. next_checkpoint points one past the detected station.
        checkpoints = get_config("checkpoint_count")
        laps = 0
        nxt = (scan.station_id % checkpoints) + 1
        event = "started" if scan.station_id == 1 else "waiting_for_start"
        c.execute(
            "INSERT INTO runner_progress "
            "(runner_id, next_checkpoint, laps_completed, "
            " last_station_id, last_seen_time) "
            "VALUES (?, ?, ?, ?, ?)",
            (runner_id, nxt, laps, scan.station_id, ts_iso),
        )
        result = {"status": "ok", "event": event, "laps": laps}
    else:
        nxt  = prog["next_checkpoint"]
        laps = prog["laps_completed"]
        checkpoints = get_config("checkpoint_count")
        sid = scan.station_id

        if sid == 1:
            # Crossing the start/finish line.
            if nxt == 2:
                # At the start with no progress since the gun or the last lap —
                # a re-scan, not a new lap.
                event = "out_of_order"
            else:
                laps += 1; nxt = 2
                event = "lap_complete"
        elif nxt == 1:
            # We expected them back at the start line but they were detected
            # further along → they crossed it undetected. Count the lap and
            # resume from where they actually are. (sid == checkpoints would be
            # a re-scan of the last station, so it does not count.)
            if sid < checkpoints:
                laps += 1; nxt = (sid % checkpoints) + 1
                event = "lap_complete_skipped"
            else:
                event = "out_of_order"
        elif sid == nxt:
            # expected checkpoint
            nxt = (nxt % checkpoints) + 1
            event = "checkpoint"
        elif sid > nxt:
            # skipped one or more (lenient)
            nxt = (sid % checkpoints) + 1
            event = "checkpoint_skipped"
        else:
            event = "out_of_order"

        c.execute(
            "UPDATE runner_progress "
            "SET next_checkpoint=?, laps_completed=?, "
            "    last_station_id=?, last_seen_time=? "
            "WHERE runner_id=?",
            (nxt, laps, scan.station_id, ts_iso, runner_id),
        )
        result = {"status": "ok", "event": event, "laps": laps}

    conn.commit(); conn.close()
    return result


# Station liveness (heartbeat + status)
@app.post("/api/station/heartbeat")
def station_heartbeat(hb: Heartbeat, _: bool = Depends(require_station)):
    """Lightweight 'I'm alive' ping from a station, sent on a fixed interval
    even when no runner is nearby. Carries the station's flash-buffer depth so
    a growing backlog (server reachable but DB struggling, say) is visible."""
    conn = get_db()
    touch_station(conn.cursor(), hb.station_id, queued=hb.queued, source="heartbeat")
    conn.commit(); conn.close()
    return {"status": "ok"}


@app.get("/api/stations/status")
def stations_status():
    """Per-station liveness for the dashboard: one row per configured station,
    whether or not it has ever reported, with seconds since its last contact and
    an `online` flag (within STATION_OFFLINE_SECONDS)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT s.id, s.name, st.last_seen, st.queued, st.source "
        "FROM stations s LEFT JOIN station_status st ON st.station_id = s.id "
        "ORDER BY s.id"
    ).fetchall()
    conn.close()

    now = datetime.now()
    out = []
    for r in rows:
        secs = None
        if r["last_seen"]:
            try:
                secs = max(0, (now - datetime.fromisoformat(r["last_seen"])).total_seconds())
            except ValueError:
                secs = None
        out.append({
            "id":          r["id"],
            "name":        r["name"],
            "last_seen":   r["last_seen"],
            "seconds_ago": round(secs) if secs is not None else None,
            "online":      secs is not None and secs <= STATION_OFFLINE_SECONDS,
            "queued":      r["queued"] if r["queued"] is not None else 0,
            "source":      r["source"],
        })
    return {"offline_after_s": STATION_OFFLINE_SECONDS, "stations": out}


# runner management
@app.post("/api/runners")
def add_runner(runner: RunnerCreate, _: bool = Depends(require_admin)):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO runners "
            "(vorname, nachname, bib_number, donation_per_km, team) "
            "VALUES (?, ?, ?, ?, ?)",
            (runner.vorname.strip(), runner.nachname.strip(),
             runner.bib_number, runner.donation_per_km, runner.team.strip()),
        )
        conn.commit()
        rid = c.lastrowid
        return {"status": "ok", "runner_id": rid}
    except sqlite3.IntegrityError as e:
        raise HTTPException(409, detail=str(e))
    finally:
        conn.close()


@app.get("/api/runners")
def list_runners():
    conn = get_db()
    rows = conn.execute("""
        SELECT r.id, r.vorname, r.nachname, r.bib_number, r.donation_per_km, r.team,
               COALESCE(p.laps_completed, 0) AS laps,
               p.last_station_id, p.last_seen_time, p.next_checkpoint
        FROM runners r
        LEFT JOIN runner_progress p ON r.id = p.runner_id
        ORDER BY laps DESC, r.bib_number ASC
    """).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["name"] = f"{d['vorname']} {d['nachname']}".strip()
        out.append(d)
    return out


@app.put("/api/runners/{bib_number}")
def update_runner(bib_number: int, runner: RunnerUpdate,
                  _: bool = Depends(require_admin)):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM runners WHERE bib_number = ?", (bib_number,))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, detail="Runner not found")
    try:
        c.execute(
            "UPDATE runners SET "
            "vorname = COALESCE(?, vorname), "
            "nachname = COALESCE(?, nachname), "
            "bib_number = COALESCE(?, bib_number), "
            "donation_per_km = COALESCE(?, donation_per_km), "
            "team = COALESCE(?, team) "
            "WHERE id = ?",
            (runner.vorname, runner.nachname, runner.bib_number,
             runner.donation_per_km, runner.team, row["id"]),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise HTTPException(409, detail=str(e))
    finally:
        conn.close()
    return {"status": "ok"}


@app.delete("/api/runners/{bib_number}")
def delete_runner(bib_number: int, _: bool = Depends(require_admin)):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM runners WHERE bib_number = ?", (bib_number,))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, detail="Runner not found")
    rid = row["id"]
    c.execute("DELETE FROM cooldown_tracker WHERE runner_id = ?", (rid,))
    c.execute("DELETE FROM runner_progress  WHERE runner_id = ?", (rid,))
    c.execute("DELETE FROM runners          WHERE id = ?",        (rid,))
    conn.commit(); conn.close()
    return {"status": "ok"}


# Beacon configuration

@app.get("/api/beacons")
def list_beacons():
    conn = get_db()
    rows = conn.execute(
        "SELECT b.bib_number, b.beacon_mac, "
        "       (r.id IS NOT NULL) AS assigned "
        "FROM beacons b "
        "LEFT JOIN runners r ON r.bib_number = b.bib_number "
        "ORDER BY b.bib_number ASC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/beacons/whitelist")
def beacon_whitelist(_: bool = Depends(require_station)):
    """Flat list of all registered beacon MAC addresses.

    Intended for the ESP boards to fetch the BLE scan whitelist. Station-key
    protected so the valid MACs aren't handed out to anyone who could then
    forge scans for them.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT beacon_mac FROM beacons ORDER BY beacon_mac ASC"
    ).fetchall()
    conn.close()
    return {"macs": [r["beacon_mac"] for r in rows]}


@app.post("/api/beacons")
def add_beacon(beacon: BeaconCreate, _: bool = Depends(require_admin)):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO beacons (bib_number, beacon_mac) VALUES (?, ?)",
            (beacon.bib_number, beacon.beacon_mac.upper()),
        )
        conn.commit()
        return {"status": "ok"}
    except sqlite3.IntegrityError as e:
        raise HTTPException(409, detail=str(e))
    finally:
        conn.close()


@app.put("/api/beacons/{bib_number}")
def update_beacon(bib_number: int, beacon: BeaconUpdate,
                  _: bool = Depends(require_admin)):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT bib_number FROM beacons WHERE bib_number = ?", (bib_number,))
    if not c.fetchone():
        conn.close()
        raise HTTPException(404, detail="Beacon not found")
    mac = beacon.beacon_mac.upper() if beacon.beacon_mac is not None else None
    try:
        c.execute(
            "UPDATE beacons SET "
            "bib_number = COALESCE(?, bib_number), "
            "beacon_mac = COALESCE(?, beacon_mac) "
            "WHERE bib_number = ?",
            (beacon.bib_number, mac, bib_number),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise HTTPException(409, detail=str(e))
    finally:
        conn.close()
    return {"status": "ok"}


@app.delete("/api/beacons/{bib_number}")
def delete_beacon(bib_number: int, _: bool = Depends(require_admin)):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT bib_number FROM beacons WHERE bib_number = ?", (bib_number,))
    if not c.fetchone():
        conn.close()
        raise HTTPException(404, detail="Beacon not found")
    c.execute("DELETE FROM beacons WHERE bib_number = ?", (bib_number,))
    conn.commit(); conn.close()
    return {"status": "ok"}


# course distance + pace estimation

def get_course_distances():
    """Return (segments_m, lap_total_m, cumulative_m).

    segments[i]  = metres from station (i+1) to (i+2); the last wraps N→1.
    cumulative[k]= metres from station 1 to station k along the lap (1-indexed).
    Falls back to equal segments derived from the configured lap distance when
    no measured route has been saved yet, so pace still works before a route is
    drawn.
    """
    checkpoints = get_config("checkpoint_count")
    lap_km      = get_config("lap_distance_km")
    segments = get_setting("segments", None)
    lap_m    = get_setting("lap_distance_m", None)
    if not segments or len(segments) != checkpoints:
        per = lap_km * 1000 / checkpoints
        segments = [per] * checkpoints
        lap_m = lap_km * 1000
    if not lap_m:
        lap_m = sum(segments)

    cum = {1: 0.0}
    acc = 0.0
    for k in range(2, checkpoints + 1):
        acc += segments[k - 2] # segment from (k-1) -> k
        cum[k] = acc
    return segments, lap_m, cum


def moving_seconds(timestamps):
    """Moving time in seconds from an ordered list of scan timestamps.

    Sums the gaps between consecutive scans, excluding any gap longer than
    the configured rest threshold — those are rests (the runner left the loop
    for the field and stopped generating scans), not running time.
    """
    max_gap = get_config("moving_gap_max_seconds")
    total = 0.0
    for a, b in zip(timestamps, timestamps[1:]):
        try:
            gap = (datetime.fromisoformat(b)
                   - datetime.fromisoformat(a)).total_seconds()
        except ValueError:
            continue
        if 0 < gap <= max_gap:
            total += gap
    return total


def estimate_pace(laps, last_station_id, moving_s, lap_m, cum):
    """Average pace (min/km) and speed (km/h) over the supplied moving time,
    i.e. distance covered ÷ time actually spent running (rests excluded)."""
    if not last_station_id or not moving_s:
        return None, None
    dist_m = laps * lap_m + cum.get(last_station_id, 0.0)
    if moving_s <= 0 or dist_m <= 0:
        return None, None
    dist_km = dist_m / 1000
    return round((moving_s / 60) / dist_km, 2), round(dist_km / (moving_s / 3600), 2)


def reconstruct_laps(events, checkpoints, cooldown_s):
    """Replay the lenient lap logic of ingest_scan over a runner's ordered scans.

    `events` is a list of (station_id, datetime) sorted by time. Returns
    (first_counted_time, [lap_completion_times]) so the detail view can show
    per-lap splits that match the live lap count.
    """
    last_at: dict[int, datetime] = {}   # station -> last counted time (cooldown)
    nxt = None
    started = None
    lap_times = []
    for sid, t in events:
        prev = last_at.get(sid)
        if prev is not None and cooldown_s > 0 and (t - prev).total_seconds() < cooldown_s:
            continue
        last_at[sid] = t
        if nxt is None:                       # first counted scan: start tracking
            nxt = (sid % checkpoints) + 1
            started = t
            continue
        if sid == 1:
            if nxt != 2:                      # progress since last lap → a lap
                nxt = 2; lap_times.append(t)
        elif nxt == 1:
            if sid < checkpoints:             # crossed the line undetected
                nxt = (sid % checkpoints) + 1; lap_times.append(t)
        elif sid == nxt:
            nxt = (nxt % checkpoints) + 1
        elif sid > nxt:
            nxt = (sid % checkpoints) + 1
    return started, lap_times


# Dashboard data
@app.get("/api/leaderboard")
def leaderboard():
    conn = get_db()
    rows = conn.execute("""
        SELECT r.vorname, r.nachname, r.bib_number, r.donation_per_km, r.team,
               b.beacon_mac,
               COALESCE(p.laps_completed, 0) AS laps,
               p.last_station_id, p.last_seen_time
        FROM runners r
        LEFT JOIN beacons b ON r.bib_number = b.bib_number
        LEFT JOIN runner_progress p ON r.id = p.runner_id
        ORDER BY laps DESC, r.bib_number ASC
    """).fetchall()

    _, lap_m, cum = get_course_distances()
    out = []
    for r in rows:
        laps = r["laps"]
        dist_m  = laps * lap_m + cum.get(r["last_station_id"], 0.0)
        dist_km = dist_m / 1000
        # Moving time: walk this runner's scans and drop the rest gaps.
        ts = [row["timestamp"] for row in conn.execute(
            "SELECT timestamp FROM scan_events WHERE beacon_mac = ? "
            "ORDER BY timestamp", (r["beacon_mac"],))] if r["beacon_mac"] else []
        moving_s = moving_seconds(ts)
        if moving_s <= 0 and len(ts) >= 2:
            # Every gap looked like a rest (e.g. a very slow walker); fall back
            # to raw elapsed so the runner still shows a pace rather than "—".
            try:
                moving_s = (datetime.fromisoformat(ts[-1])
                            - datetime.fromisoformat(ts[0])).total_seconds()
            except ValueError:
                moving_s = 0
        pace, speed = estimate_pace(laps, r["last_station_id"], moving_s, lap_m, cum)
        out.append({
            "name":             f"{r['vorname']} {r['nachname']}".strip(),
            "bib_number":       r["bib_number"],
            "team":             r["team"],
            "donation_per_km":  r["donation_per_km"],
            "laps":             laps,
            "distance_km":      round(dist_km, 2),
            "donations":        round(dist_km * r["donation_per_km"], 2),
            "last_station_id":  r["last_station_id"],
            "last_seen_time":   r["last_seen_time"],
            "pace_min_km":      pace,
            "speed_kmh":        speed,
        })
    conn.close()
    return out


@app.get("/api/runners/{bib_number}/detail")
def runner_detail(bib_number: int):
    """Per-runner breakdown for the dashboard drill-down: totals plus lap splits
    reconstructed from this runner's scan history."""
    conn = get_db()
    row = conn.execute("""
        SELECT r.vorname, r.nachname, r.bib_number, r.donation_per_km, r.team,
               b.beacon_mac,
               COALESCE(p.laps_completed, 0) AS laps,
               p.last_station_id, p.last_seen_time
        FROM runners r
        LEFT JOIN beacons b ON r.bib_number = b.bib_number
        LEFT JOIN runner_progress p ON r.id = p.runner_id
        WHERE r.bib_number = ?
    """, (bib_number,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, detail="Runner not found")

    raw = []
    if row["beacon_mac"]:
        raw = conn.execute(
            "SELECT station_id, timestamp FROM scan_events WHERE beacon_mac = ? "
            "ORDER BY timestamp", (row["beacon_mac"],)).fetchall()
    conn.close()

    parsed = []
    for e in raw:
        try:
            parsed.append((e["station_id"], datetime.fromisoformat(e["timestamp"])))
        except ValueError:
            pass

    checkpoints = get_config("checkpoint_count")
    cooldown_s  = get_config("cooldown_seconds")
    _, lap_m, cum = get_course_distances()

    started, lap_dts = reconstruct_laps(parsed, checkpoints, cooldown_s)
    splits = []
    prev = started
    for i, t in enumerate(lap_dts, start=1):
        if prev is not None:
            splits.append({"lap": i, "seconds": round((t - prev).total_seconds()),
                           "at": t.isoformat()})
        prev = t
    split_secs = [s["seconds"] for s in splits if s["seconds"] > 0]
    best_lap = min(split_secs) if split_secs else None
    avg_lap  = round(sum(split_secs) / len(split_secs)) if split_secs else None

    laps = row["laps"]
    dist_km = (laps * lap_m + cum.get(row["last_station_id"], 0.0)) / 1000
    ts_list = [t.isoformat() for _, t in parsed]
    moving_s = moving_seconds(ts_list)
    if moving_s <= 0 and len(parsed) >= 2:
        moving_s = (parsed[-1][1] - parsed[0][1]).total_seconds()
    pace, speed = estimate_pace(laps, row["last_station_id"], moving_s, lap_m, cum)

    return {
        "bib_number":       row["bib_number"],
        "name":             f"{row['vorname']} {row['nachname']}".strip(),
        "team":             row["team"],
        "donation_per_km":  row["donation_per_km"],
        "laps":             laps,
        "distance_km":      round(dist_km, 2),
        "donations":        round(dist_km * row["donation_per_km"], 2),
        "pace_min_km":      pace,
        "speed_kmh":        speed,
        "last_station_id":  row["last_station_id"],
        "last_seen_time":   row["last_seen_time"],
        "first_seen_time":  parsed[0][1].isoformat() if parsed else None,
        "scan_count":       len(parsed),
        "moving_seconds":   round(moving_s),
        "laps_detail":      splits,
        "best_lap_seconds": best_lap,
        "avg_lap_seconds":  avg_lap,
    }


@app.get("/api/teams")
def teams():
    """Leaderboard aggregated by team. Runners without a team are grouped under
    an empty key so the dashboard can hide the table when no teams are used."""
    groups: dict[str, dict] = {}
    for r in leaderboard():
        key = (r["team"] or "").strip()
        g = groups.get(key)
        if not g:
            g = groups[key] = {"team": key, "runners": 0, "laps": 0,
                               "distance_km": 0.0, "donations": 0.0}
        g["runners"]     += 1
        g["laps"]        += r["laps"]
        g["distance_km"] += r["distance_km"]
        g["donations"]   += r["donations"]
    out = sorted(groups.values(),
                 key=lambda g: (-g["distance_km"], -g["laps"]))
    for g in out:
        g["distance_km"] = round(g["distance_km"], 2)
        g["donations"]   = round(g["donations"], 2)
    return out


@app.get("/api/stats")
def stats():
    conn = get_db()
    c = conn.cursor()
    total_runners = c.execute(
        "SELECT COUNT(*) FROM runners"
    ).fetchone()[0]
    total_laps = c.execute(
        "SELECT COALESCE(SUM(laps_completed),0) FROM runner_progress"
    ).fetchone()[0]
    active = c.execute(
        "SELECT COUNT(*) FROM runner_progress WHERE last_station_id IS NOT NULL"
    ).fetchone()[0]
    # Earliest scan = de-facto event start, used to extrapolate final totals.
    first_scan = c.execute("SELECT MIN(timestamp) FROM scan_events").fetchone()[0]
    conn.close()

    # Donations and total distance are distance-based, derive them (and the
    # average pace) from leaderboard, whose per-runner distance already counts
    # progress between stations — not just completed laps.
    lb = leaderboard()
    total_donations = sum(r["donations"] for r in lb)
    total_distance_km = sum(r["distance_km"] for r in lb)
    paces = [r["pace_min_km"] for r in lb if r["pace_min_km"]]
    avg_pace = round(sum(paces) / len(paces), 2) if paces else None

    # Projected final totals: extend the current collective rate (totals so far ÷
    # time since the run opened) to the *projected* end time. The baseline is the
    # official start if set, else the first scan. Once the run has actually been
    # stopped the totals are final, so the projection is simply the tally as-is —
    # the projected end time only ever drives the live forecast, never the result.
    # Returns null when there is nothing to project from (no end time, no scans).
    event_end_iso = get_setting("event_end", None)
    event_stopped = get_setting("event_stopped", None)
    baseline = get_setting("event_start", None) or first_scan
    projected_donations = projected_distance_km = None
    if event_stopped:
        projected_donations   = round(total_donations, 2)
        projected_distance_km = round(total_distance_km, 1)
    elif event_end_iso and baseline:
        try:
            start = datetime.fromisoformat(baseline)
            end   = datetime.fromisoformat(event_end_iso)
            now   = datetime.now()
            elapsed = (now - start).total_seconds()
            if now >= end:
                # Projected end reached — the projection is simply the tally so far.
                projected_donations   = round(total_donations, 2)
                projected_distance_km = round(total_distance_km, 1)
            elif elapsed > 0:
                remaining = (end - now).total_seconds()
                projected_donations   = round(
                    total_donations + total_donations / elapsed * remaining, 2)
                projected_distance_km = round(
                    total_distance_km + total_distance_km / elapsed * remaining, 1)
        except (TypeError, ValueError):
            pass

    _, lap_m, _ = get_course_distances()
    return {
        "total_runners":     total_runners,
        "active_runners":    active,
        "total_laps":        total_laps,
        "total_distance_km": round(total_distance_km, 1),
        "total_donations":   round(total_donations, 2),
        "lap_distance_km":   round(lap_m / 1000, 2),
        "avg_pace_min_km":   avg_pace,
        "donation_goal":     round(get_config("donation_goal"), 2),
        "event_end":         event_end_iso,
        "event_stopped":     event_stopped,
        "projected_donations":   projected_donations,
        "projected_distance_km": projected_distance_km,
    }


# Admin: reset everything
def wipe_run_data() -> None:
    """Delete all per-run data (runners, beacons, scans, progress). Leaves
    archived past_runs, stations and the route/config untouched."""
    conn = get_db()
    for t in ("scan_events", "cooldown_tracker", "runner_progress", "runners", "beacons"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit(); conn.close()


def wipe_run_activity() -> None:
    """Reset timing/lap data while keeping the prepared runners and beacons."""
    conn = get_db()
    for t in ("scan_events", "cooldown_tracker", "runner_progress"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit(); conn.close()


@app.post("/api/reset")
def reset_all(_: bool = Depends(require_admin)):
    """Wipe all data — admin only."""
    wipe_run_data()
    return {"status": "ok", "message": "All data cleared"}


# Run lifecycle: start / end, with archiving of finished runs
def archive_run() -> Optional[int]:
    """Freeze the current run as a JSON snapshot in past_runs. No-op (returns
    None) when there's nothing to save (no runners and no scans yet)."""
    conn = get_db()
    has = conn.execute(
        "SELECT (SELECT COUNT(*) FROM runners) + (SELECT COUNT(*) FROM scan_events)"
    ).fetchone()[0]
    conn.close()
    if not has:
        return None

    started = get_setting("event_start", None)
    stopped = get_setting("event_stopped", None)
    data = {
        "stats":         stats(),
        "leaderboard":   leaderboard(),
        "teams":         teams(),
        "event_start":   started,
        "event_end":     get_setting("event_end", None),
        "event_stopped": stopped,
    }

    label_src = started or stopped
    try:
        label = ("Lauf vom " + datetime.fromisoformat(label_src).strftime("%d.%m.%Y")
                 if label_src else "Lauf")
    except (TypeError, ValueError):
        label = "Lauf"

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO past_runs (label, started_at, ended_at, created_at, data) "
        "VALUES (?, ?, ?, ?, ?)",
        (label, started, stopped, datetime.now().isoformat(), json.dumps(data)),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


@app.post("/api/run/start")
def run_start(_: bool = Depends(require_admin)):
    """Start a run with the prepared roster and tag assignments.

    An active previous run is archived first. Only its scan/progress data is
    reset; runners and beacons deliberately survive so they can be entered and
    checked before the official clock starts.
    """
    if (get_setting("event_start", None)
            and not get_setting("event_stopped", None)):
        archive_run()
    wipe_run_activity()
    set_setting("event_start", datetime.now().isoformat())
    set_setting("event_end", None)
    set_setting("event_stopped", None)
    return {"status": "ok"}


@app.post("/api/run/end")
def run_end(_: bool = Depends(require_admin)):
    """End the current run: record the actual stop time and archive a snapshot.
    The live data stays in place so the dashboard keeps showing the final result
    until a new run is started."""
    if get_setting("event_stopped", None):
        return {"status": "ok"}      # already ended — don't archive twice
    # Mark the run stopped *before* snapshotting so the archive freezes the
    # final, ended state — ended_at, the embedded event_stopped, and the final
    # (not projected) totals all reflect a finished run.
    set_setting("event_stopped", datetime.now().isoformat())
    archive_run()
    return {"status": "ok"}


@app.get("/api/runs")
def list_runs(_: bool = Depends(require_admin)):
    """Past runs, newest first, with headline totals for the admin list."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, label, started_at, ended_at, created_at, data "
        "FROM past_runs ORDER BY id DESC"
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            st = json.loads(r["data"]).get("stats", {})
        except (TypeError, ValueError):
            st = {}
        out.append({
            "id":                r["id"],
            "label":             r["label"],
            "started_at":        r["started_at"],
            "ended_at":          r["ended_at"],
            "created_at":        r["created_at"],
            "total_runners":     st.get("total_runners"),
            "total_distance_km": st.get("total_distance_km"),
            "total_donations":   st.get("total_donations"),
        })
    return out


@app.get("/api/runs/{run_id}")
def get_run(run_id: int, _: bool = Depends(require_admin)):
    """Full archived snapshot of a single past run."""
    conn = get_db()
    row = conn.execute(
        "SELECT id, label, started_at, ended_at, created_at, data "
        "FROM past_runs WHERE id = ?", (run_id,)
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, detail="Run not found")
    try:
        data = json.loads(row["data"])
    except (TypeError, ValueError):
        data = {}
    return {
        "id":         row["id"],
        "label":      row["label"],
        "started_at": row["started_at"],
        "ended_at":   row["ended_at"],
        "created_at": row["created_at"],
        "data":       data,
    }


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: int, _: bool = Depends(require_admin)):
    """Remove an archived run (e.g. a test run)."""
    conn = get_db()
    cur = conn.execute("DELETE FROM past_runs WHERE id = ?", (run_id,))
    conn.commit(); conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, detail="Run not found")
    return {"status": "ok"}


# Favicon: a tri-colour (blue/green/orange) heart, matching the "Spendenlauf"
# wordmark — a charity-run motif that stays legible down to 16px.
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<defs><clipPath id="h"><path d="M16 29C16 29 3 20.5 3 11.5 3 7 6.5 4 10 4 '
    '13 4 15 6 16 8 17 6 19 4 22 4 25.5 4 29 7 29 11.5 29 20.5 16 29 16 29Z"/>'
    '</clipPath></defs><g clip-path="url(#h)">'
    '<rect width="11" height="32" fill="#2563eb"/>'
    '<rect x="11" width="10" height="32" fill="#16a34a"/>'
    '<rect x="21" width="11" height="32" fill="#ea580c"/>'
    '</g></svg>'
)


@app.get("/favicon.svg")
def favicon():
    return Response(content=FAVICON_SVG, media_type="image/svg+xml")


# Serve dashboard
@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    path = Path(__file__).parent / "static" / "dashboard.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))

@app.get("/admin", response_class=HTMLResponse)
def serve_admin():
    path = Path(__file__).parent / "static" / "admin.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))
