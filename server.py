"""
Spendenlauf Tracker — Backend Server
=====================================
FastAPI server that ingests BLE scan events from ESP32 stations,
reconstructs laps from sequential checkpoint passes, and serves
a live dashboard plus JSON APIs.

Run:  uvicorn server:app --host 0.0.0.0 --port 8000 --reload
"""

from fastapi import FastAPI, HTTPException, Response, Cookie, Depends
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import sqlite3
import os
import json
import secrets
from datetime import datetime, timedelta
from pathlib import Path


# ── .env loader (tiny, dependency-free) ────────────────────────

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
DATABASE          = "spendenlauf.db"

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SESSION_TTL    = timedelta(hours=12)

# Default station placement: a loop around the Schyrenbad / Sachsenstraße,
# München-Au. Admin can drag these to their exact spots on the map.
DEFAULT_STATIONS = [
    (1, "Station 1", 48.11680, 11.57930),
    (2, "Station 2", 48.11620, 11.58150),
    (3, "Station 3", 48.11480, 11.58100),
    (4, "Station 4", 48.11470, 11.57850),
    (5, "Station 5", 48.11570, 11.57800),
]

# In-memory session store: token → expiry. Cleared on server restart.
SESSIONS: dict[str, datetime] = {}

# ── App setup ──────────────────────────────────────────────────

app = FastAPI(title="Spendenlauf Tracker")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Database helpers ───────────────────────────────────────────

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

    # Beacon hardware: pairs a start number (bib) with a BLE MAC address.
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

    c.execute("""
        CREATE TABLE IF NOT EXISTS cooldown_tracker (
            runner_id      INTEGER NOT NULL,
            station_id     INTEGER NOT NULL,
            last_scan_time TEXT    NOT NULL,
            PRIMARY KEY (runner_id, station_id)
        )
    """)

    # Physical location of each station (set by admin on the map).
    c.execute("""
        CREATE TABLE IF NOT EXISTS stations (
            id        INTEGER PRIMARY KEY,
            name      TEXT NOT NULL,
            latitude  REAL,
            longitude REAL
        )
    """)

    # Generic key/value store (route polyline, saved map view, …).
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


# ── Settings helpers ───────────────────────────────────────────

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


# ── Admin authentication (cookie session) ──────────────────────

def create_session() -> str:
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = datetime.utcnow() + SESSION_TTL
    return token


def session_valid(token: Optional[str]) -> bool:
    if not token:
        return False
    expiry = SESSIONS.get(token)
    if not expiry:
        return False
    if datetime.utcnow() > expiry:
        SESSIONS.pop(token, None)
        return False
    return True


def require_admin(session: Optional[str] = Cookie(default=None)) -> bool:
    """FastAPI dependency that rejects unauthenticated requests."""
    if not session_valid(session):
        raise HTTPException(401, detail="Not authenticated")
    return True


# ── Pydantic models ───────────────────────────────────────────

class ScanEvent(BaseModel):
    station_id: int                # 1–5
    beacon_mac: str                # e.g. "AA:BB:CC:DD:EE:01"
    timestamp:  str                # ISO 8601
    rssi:       int                # e.g. -65

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


# ── Admin auth endpoints ───────────────────────────────────────

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
        httponly=True, samesite="lax",
        max_age=int(SESSION_TTL.total_seconds()),
    )
    return {"status": "ok"}


@app.post("/api/logout")
def logout(response: Response, session: Optional[str] = Cookie(default=None)):
    if session:
        SESSIONS.pop(session, None)
    response.delete_cookie("session")
    return {"status": "ok"}


@app.get("/api/auth")
def auth_status(session: Optional[str] = Cookie(default=None)):
    return {"authenticated": session_valid(session)}


# ── Stations & route ───────────────────────────────────────────

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


# ── Scan ingestion + lap reconstruction ───────────────────────

@app.post("/api/scan")
def ingest_scan(scan: ScanEvent):
    """
    Accept a scan event from an ESP32 station.

    Lap logic (lenient):
      - Track next_checkpoint per runner (starts at 1).
      - If the scan matches next_checkpoint → advance.
      - If the scan is *ahead* of next_checkpoint → accept (skip gap).
      - Hitting checkpoint 5 (after progressing through ≥1) → lap complete.
      - Out-of-order / behind → ignored.
    """
    conn = get_db()
    c = conn.cursor()
    mac = scan.beacon_mac.upper()

    # 1. Store raw event (useful for post-hoc analysis)
    c.execute(
        "INSERT INTO scan_events (station_id, beacon_mac, timestamp, rssi) "
        "VALUES (?, ?, ?, ?)",
        (scan.station_id, mac, scan.timestamp, scan.rssi),
    )

    # 2. Identify runner (beacon MAC → bib number → runner)
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

    # 3. Cooldown check
    scan_time = datetime.fromisoformat(scan.timestamp)
    c.execute(
        "SELECT last_scan_time FROM cooldown_tracker "
        "WHERE runner_id = ? AND station_id = ?",
        (runner_id, scan.station_id),
    )
    cd = c.fetchone()
    if cd:
        last = datetime.fromisoformat(cd["last_scan_time"])
        if (scan_time - last).total_seconds() < COOLDOWN_SECONDS:
            conn.commit(); conn.close()
            return {"status": "ignored", "reason": "cooldown"}

    # Update cooldown timestamp
    c.execute(
        "INSERT OR REPLACE INTO cooldown_tracker "
        "(runner_id, station_id, last_scan_time) VALUES (?, ?, ?)",
        (runner_id, scan.station_id, scan.timestamp),
    )

    # 4. Progress / lap reconstruction
    c.execute(
        "SELECT * FROM runner_progress WHERE runner_id = ?", (runner_id,)
    )
    prog = c.fetchone()

    if not prog:
        # First-ever scan for this runner
        if scan.station_id == 1:
            nxt = 2; laps = 0
            event = "started"
        else:
            nxt = 1; laps = 0
            event = "waiting_for_start"
        c.execute(
            "INSERT INTO runner_progress "
            "(runner_id, next_checkpoint, laps_completed, "
            " last_station_id, last_seen_time) "
            "VALUES (?, ?, ?, ?, ?)",
            (runner_id, nxt, laps, scan.station_id, scan.timestamp),
        )
        result = {"status": "ok", "event": event, "laps": laps}
    else:
        nxt  = prog["next_checkpoint"]
        laps = prog["laps_completed"]

        if scan.station_id == nxt:
            # Expected checkpoint
            if nxt == CHECKPOINT_COUNT:
                laps += 1; nxt = 1
                event = "lap_complete"
            else:
                nxt += 1
                event = "checkpoint"
        elif scan.station_id > nxt:
            # Skipped one or more — be lenient
            if scan.station_id == CHECKPOINT_COUNT:
                laps += 1; nxt = 1
                event = "lap_complete_skipped"
            else:
                nxt = scan.station_id + 1
                event = "checkpoint_skipped"
        else:
            event = "out_of_order"

        c.execute(
            "UPDATE runner_progress "
            "SET next_checkpoint=?, laps_completed=?, "
            "    last_station_id=?, last_seen_time=? "
            "WHERE runner_id=?",
            (nxt, laps, scan.station_id, scan.timestamp, runner_id),
        )
        result = {"status": "ok", "event": event, "laps": laps}

    conn.commit(); conn.close()
    return result


# ── Runner management ─────────────────────────────────────────

@app.post("/api/runners")
def add_runner(runner: RunnerCreate):
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
def delete_runner(bib_number: int):
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


# ── Beacon configuration ──────────────────────────────────────

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
def beacon_whitelist():
    """Flat list of all registered beacon MAC addresses.

    Intended for the ESP boards to fetch the BLE scan whitelist.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT beacon_mac FROM beacons ORDER BY beacon_mac ASC"
    ).fetchall()
    conn.close()
    return {"macs": [r["beacon_mac"] for r in rows]}


@app.post("/api/beacons")
def add_beacon(beacon: BeaconCreate):
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
def delete_beacon(bib_number: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT bib_number FROM beacons WHERE bib_number = ?", (bib_number,))
    if not c.fetchone():
        conn.close()
        raise HTTPException(404, detail="Beacon not found")
    c.execute("DELETE FROM beacons WHERE bib_number = ?", (bib_number,))
    conn.commit(); conn.close()
    return {"status": "ok"}


# ── Course distance + pace estimation ─────────────────────────

def get_course_distances():
    """Return (segments_m, lap_total_m, cumulative_m).

    segments[i]  = metres from station (i+1) to (i+2); the last wraps N→1.
    cumulative[k]= metres from station 1 to station k along the lap (1-indexed).
    Falls back to equal segments derived from LAP_DISTANCE_KM when no measured
    route has been saved yet, so pace still works before a route is drawn.
    """
    segments = get_setting("segments", None)
    lap_m    = get_setting("lap_distance_m", None)
    if not segments or len(segments) != CHECKPOINT_COUNT:
        per = LAP_DISTANCE_KM * 1000 / CHECKPOINT_COUNT
        segments = [per] * CHECKPOINT_COUNT
        lap_m = LAP_DISTANCE_KM * 1000
    if not lap_m:
        lap_m = sum(segments)

    cum = {1: 0.0}
    acc = 0.0
    for k in range(2, CHECKPOINT_COUNT + 1):
        acc += segments[k - 2]          # segment from (k-1) → k
        cum[k] = acc
    return segments, lap_m, cum


def estimate_pace(laps, last_station_id, start_iso, last_iso, lap_m, cum):
    """Rough average pace (min/km) and speed (km/h) over the whole run,
    from distance covered ÷ elapsed time between first and last station pass."""
    if not start_iso or not last_iso or not last_station_id:
        return None, None
    try:
        elapsed = (datetime.fromisoformat(last_iso)
                   - datetime.fromisoformat(start_iso)).total_seconds()
    except ValueError:
        return None, None
    dist_m = laps * lap_m + cum.get(last_station_id, 0.0)
    if elapsed <= 0 or dist_m <= 0:
        return None, None
    dist_km = dist_m / 1000
    return round((elapsed / 60) / dist_km, 2), round(dist_km / (elapsed / 3600), 2)


# ── Dashboard data ────────────────────────────────────────────

@app.get("/api/leaderboard")
def leaderboard():
    conn = get_db()
    rows = conn.execute("""
        SELECT r.vorname, r.nachname, r.bib_number, r.donation_per_km,
               COALESCE(p.laps_completed, 0) AS laps,
               p.last_station_id, p.last_seen_time,
               (SELECT MIN(timestamp) FROM scan_events e
                 WHERE e.beacon_mac = b.beacon_mac) AS start_time
        FROM runners r
        LEFT JOIN beacons b ON r.bib_number = b.bib_number
        LEFT JOIN runner_progress p ON r.id = p.runner_id
        ORDER BY laps DESC, r.bib_number ASC
    """).fetchall()
    conn.close()

    _, lap_m, cum = get_course_distances()
    out = []
    for r in rows:
        laps = r["laps"]
        dist_m  = laps * lap_m + cum.get(r["last_station_id"], 0.0)
        dist_km = dist_m / 1000
        pace, speed = estimate_pace(
            laps, r["last_station_id"], r["start_time"], r["last_seen_time"],
            lap_m, cum)
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

    # Donations are now distance-based, so derive them (and the average pace)
    # from the leaderboard, which already computes per-runner distance.
    lb = leaderboard()
    total_donations = sum(r["donations"] for r in lb)
    paces = [r["pace_min_km"] for r in lb if r["pace_min_km"]]
    avg_pace = round(sum(paces) / len(paces), 2) if paces else None

    _, lap_m, _ = get_course_distances()
    return {
        "total_runners":     total_runners,
        "active_runners":    active,
        "total_laps":        total_laps,
        "total_distance_km": round(total_laps * lap_m / 1000, 1),
        "total_donations":   round(total_donations, 2),
        "lap_distance_km":   round(lap_m / 1000, 2),
        "avg_pace_min_km":   avg_pace,
    }


# ── Admin: reset everything ──────────────────────────────────

@app.post("/api/reset")
def reset_all():
    """Wipe all data — useful during testing."""
    conn = get_db()
    for t in ("scan_events", "cooldown_tracker", "runner_progress", "runners", "beacons"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit(); conn.close()
    return {"status": "ok", "message": "All data cleared"}


# ── Serve dashboard ───────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    path = Path(__file__).parent / "static" / "dashboard.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/admin", response_class=HTMLResponse)
def serve_admin():
    path = Path(__file__).parent / "static" / "admin.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


# ── Startup ───────────────────────────────────────────────────

@app.on_event("startup")
def on_startup():
    init_db()
    print("✔ Database initialised")
    print("✔ Dashboard at http://localhost:8000")
    print("✔ API docs  at http://localhost:8000/docs")
