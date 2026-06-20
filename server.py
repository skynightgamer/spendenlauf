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
# Pace is based on "moving time": gaps between scans longer than this are
# treated as a rest (the runner left the loop for the field) and excluded.
# Normal loop segments are well under this; a rest excursion is far longer.
MOVING_GAP_MAX_SECONDS = 300  # 5 min
DATABASE          = "spendenlauf.db"

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

    # Seed one row per checkpoint with default coordinates near the Schyrenbad.
    for sid, name, lat, lng in DEFAULT_STATIONS:
        c.execute(
            "INSERT OR IGNORE INTO stations (id, name, latitude, longitude) "
            "VALUES (?, ?, ?, ?)",
            (sid, name, lat, lng),
        )

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


# Runtime-tunable settings, editable from the admin panel.
# key -> (default, caster, min, max). Stored in `settings` as "cfg_<key>"; the
# module constants above are the defaults when nothing has been saved yet.
CONFIG_SPEC = {
    "checkpoint_count":       (CHECKPOINT_COUNT,       int,   2,   20),
    "cooldown_seconds":       (COOLDOWN_SECONDS,       int,   0,   3600),
    "lap_distance_km":        (LAP_DISTANCE_KM,        float, 0.1, 100.0),
    "moving_gap_max_seconds": (MOVING_GAP_MAX_SECONDS, int,   30,  3600),
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

class RunnerCreate(BaseModel):
    vorname:         str
    nachname:        str
    bib_number:      int
    donation_per_km: float = 0.0

class RunnerUpdate(BaseModel):
    vorname:         Optional[str]   = None
    nachname:        Optional[str]   = None
    bib_number:      Optional[int]   = None
    donation_per_km: Optional[float] = None

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


@app.get("/api/config")
def get_config_all():
    return {k: get_config(k) for k in CONFIG_SPEC}


@app.put("/api/config")
def update_config(data: ConfigUpdate, _: bool = Depends(require_admin)):
    for key, val in data.model_dump(exclude_unset=True).items():
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
    return {"status": "ok", "config": {k: get_config(k) for k in CONFIG_SPEC}}


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

    # Authoritative server-side timestamp (naive local time).
    scan_time = datetime.now()
    ts_iso = scan_time.isoformat()

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


# runner management
@app.post("/api/runners")
def add_runner(runner: RunnerCreate, _: bool = Depends(require_admin)):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO runners "
            "(vorname, nachname, bib_number, donation_per_km) "
            "VALUES (?, ?, ?, ?)",
            (runner.vorname.strip(), runner.nachname.strip(),
             runner.bib_number, runner.donation_per_km),
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
        SELECT r.id, r.vorname, r.nachname, r.bib_number, r.donation_per_km,
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
            "donation_per_km = COALESCE(?, donation_per_km) "
            "WHERE id = ?",
            (runner.vorname, runner.nachname, runner.bib_number,
             runner.donation_per_km, row["id"]),
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


# Dashboard data
@app.get("/api/leaderboard")
def leaderboard():
    conn = get_db()
    rows = conn.execute("""
        SELECT r.vorname, r.nachname, r.bib_number, r.donation_per_km,
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
    conn.close()

    # Donations and total distance are distance-based, derive them (and the
    # average pace) from leaderboard, whose per-runner distance already counts
    # progress between stations — not just completed laps.
    lb = leaderboard()
    total_donations = sum(r["donations"] for r in lb)
    total_distance_km = sum(r["distance_km"] for r in lb)
    paces = [r["pace_min_km"] for r in lb if r["pace_min_km"]]
    avg_pace = round(sum(paces) / len(paces), 2) if paces else None

    _, lap_m, _ = get_course_distances()
    return {
        "total_runners":     total_runners,
        "active_runners":    active,
        "total_laps":        total_laps,
        "total_distance_km": round(total_distance_km, 1),
        "total_donations":   round(total_donations, 2),
        "lap_distance_km":   round(lap_m / 1000, 2),
        "avg_pace_min_km":   avg_pace,
    }


# Admin: reset everything
@app.post("/api/reset")
def reset_all(_: bool = Depends(require_admin)):
    """Wipe all data — admin only."""
    conn = get_db()
    for t in ("scan_events", "cooldown_tracker", "runner_progress", "runners", "beacons"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit(); conn.close()
    return {"status": "ok", "message": "All data cleared"}


# Serve dashboard
@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    path = Path(__file__).parent / "static" / "dashboard.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))

@app.get("/admin", response_class=HTMLResponse)
def serve_admin():
    path = Path(__file__).parent / "static" / "admin.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))
