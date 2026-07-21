# Spendenlauf Tracker

Backend server + live dashboard with admin panel for BLE beacon-based lap counting.

## What it does

ESP32 BLE scanners sit around a loop. Each runner carries a BLE beacon tag. When a scanner detects a beacon, it POSTs a scan event to this server. The server reconstructs laps by tracking sequential checkpoint passes (e.g. 1 → 2 → 3 → 4 → 5 = one lap), applies per-station cooldown filtering, and feeds a live leaderboard dashboard.

## Requirements

- **Python 3.10+**
- Dependencies in [`requirements.txt`](requirements.txt) (FastAPI, Uvicorn, Requests)
- ESP32 station firmware (not public)

## Quick start (local development)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create .env (copy the template) and set credentials.
# For local HTTP, set COOKIE_SECURE=false so the admin session cookie works.
cp .env.example .env

# 3. Start the server (dev mode: auto-reload, listens on all interfaces)
uvicorn server:app --host 0.0.0.0 --port 8000 --reload

# 4. In a second terminal, run the simulator (if necessary)
# registers 20 fake runners and sends scan events at 10× real time. Dashboard updates every 3 seconds.
# The simulator logs in as admin first, so its credentials must match .env
# (or pass --username/--password), and the server needs COOKIE_SECURE=false.
# options:
# python simulate.py --runners 10    # fewer runners
# python simulate.py --speed 20      # 20× real time (faster)
# python simulate.py --reset         # wipe all data before starting
# python simulate.py --url http://192.168.1.50:8000   # remote server
python simulate.py --reset
```

> `--host 0.0.0.0` and `--reload` are **development only**. For a public server see
> [DEPLOY.md](DEPLOY.md) — it binds to localhost behind nginx and drops auto-reload.

## ESP32 station contract

The ESP32 firmware talks to the server over two endpoints, both authenticated
with the pre-shared station key (`STATION_API_KEY` in `.env`, sent as the
`X-API-Key` header). Every other endpoint backs the dashboard and admin panel —
to browse those, enable the Swagger UI: pass `docs_url="/docs"` to `FastAPI(...)`
in `server.py` (it's disabled by default) and open `/docs`.

**Report a beacon detection:**

```
POST /api/scan
X-API-Key: <STATION_API_KEY>
Content-Type: application/json

{ "station_id": 3, "beacon_mac": "AA:BB:CC:DD:EE:01", "rssi": -65 }
```

The server stamps each event with its own receive time rather than trusting the
client clock. Response:

```json
{ "status": "ok", "event": "checkpoint", "laps": 1 }
```

`event` is one of: `started`, `checkpoint`, `lap_complete`, `checkpoint_skipped`,
`lap_complete_skipped`, `out_of_order`, `waiting_for_start`. If `STATION_API_KEY`
is unset on the server, ingestion fails closed with HTTP 503.

**Fetch the beacon whitelist** (stations call this once on boot so they only
report known tags; if it fails they fall back to accepting all BLE devices):

```
GET /api/beacons/whitelist
X-API-Key: <STATION_API_KEY>

→ { "macs": ["AA:BB:CC:DD:EE:01", ...] }
```

**Fetch scanner settings** (stations use firmware defaults if this fails and
retry once per minute):

```
GET /api/station/config
X-API-Key: <STATION_API_KEY>

→ {
    "report_cooldown_seconds": 10,
    "rssi_threshold": -80,
    "scan_duration_seconds": 4
  }
```

The report cooldown only suppresses duplicate transmissions from an ESP32. It
is deliberately separate from `cooldown_seconds`, which remains the server's
authoritative rule for whether a scan counts.

## Admin panel & map

Create a `.env` (copy `.env.example`) with `ADMIN_USERNAME` / `ADMIN_PASSWORD`,
then open `http://localhost:8000/admin` and log in. The admin area has four tabs:

- **Karten-Konfiguration** — station/route map editor (Leaflet + OpenStreetMap).
  Drag the numbered station markers onto their real spots and rename them;
  **Entlang Straßen** auto-connects them into a lap that follows real footpaths
  (incl. trails / Feldwege, via the public FOSSGIS foot router). If the shortest
  connections collapse into an out-and-back route, the editor adds shaping
  points to form a loop. Drag the green route to add a blue shaping point; drag
  that point to reroute along other paths, or click it to remove it. The lap
  length and per-segment distances update after every edit; **Speichern** saves
  coordinates, route, shaping points, distances and the current map view.
- **Läufer-Konfiguration** — add / edit / delete runners (Vorname, Nachname,
  donation €/km). The start number is picked from configured beacons, so each
  runner is tied to a beacon. Runners and beacons can be prepared before the
  official start; starting a run clears only scan/lap progress and keeps this
  prepared roster.
- **Beacon-Konfiguration** — pair start numbers with beacon MAC addresses.
  Configure beacons here *before* adding runners.
- **Einstellungen** — tune the runtime settings (see [Configuration](#configuration))
  without editing code or restarting.

The saved map then appears on the public dashboard with live runner counts per
station. The dashboard also shows an estimated **Pace** (min/km) per runner —
distance covered ÷ moving time, where gaps longer than the rest threshold are
excluded so a runner resting in the field isn't counted as slow.

## Configuration

These seven settings are tunable at runtime from the **Einstellungen** tab (or
`PUT /api/config`). The override is stored in the database and takes effect on the
next scan / refresh — no restart. ESP32 stations refresh their three scanner
settings once per minute. The constants at the top of `server.py` are just the
**defaults** used until an override is saved.

| Key / constant                                                              | Default | Meaning                                                        |
| --------------------------------------------------------------------------- | ------- | -------------------------------------------------------------- |
| `checkpoint_count` / `CHECKPOINT_COUNT`                                     | 5       | Number of stations around the loop                             |
| `cooldown_seconds` / `COOLDOWN_SECONDS`                                     | 180     | Ignore re-scans of the same station within this window         |
| `lap_distance_km` / `LAP_DISTANCE_KM`                                       | 2.0     | Fallback loop length when no route has been drawn              |
| `moving_gap_max_seconds` / `MOVING_GAP_MAX_SECONDS`                         | 300     | Scan gaps longer than this count as a rest, excluded from pace |
| `station_report_cooldown_seconds` / `STATION_REPORT_COOLDOWN_SECONDS`       | 10      | Suppress duplicate transmissions from each scanner             |
| `rssi_threshold` / `RSSI_THRESHOLD`                                         | -80     | Weakest BLE signal in dBm that a scanner reports               |
| `scan_duration_seconds` / `SCAN_DURATION_SECONDS`                           | 4       | Duration of each BLE scan cycle                                |

> **`checkpoint_count` is structural.** Changing it mid-event invalidates
> in-flight laps and assumes the physical stations match, so the admin UI confirms
> before applying it — set it before runners start. The simulator reads this value
> from `/api/config` at startup, so it always matches the server.

`DATABASE` (`spendenlauf.db`, the SQLite file path) remains a code-only constant
in `server.py`.

## How laps are counted

A lap is the full loop **1 → 2 → 3 → 4 → 5 → 1**, counted when the runner
crosses the start line (station 1) again — not when they reach the last
station. This keeps the lap count consistent with the distance estimate, which
measures progress from station 1. The algorithm is lenient to handle real-world
BLE flakiness:

- Each runner has a "next expected checkpoint". After station 1 it points at 2 and advances cyclically (2 → 3 → 4 → 5 → 1).
- Hitting the expected checkpoint advances it.
- Re-crossing station 1 after any forward progress counts as a completed lap.
- If a runner skips a checkpoint (e.g. hits 4 when expecting 3), the system accepts it and keeps going. If they were expected back at station 1 but reappear further along, they crossed the line undetected and the lap still counts.
- Out-of-order scans (going backward) and re-scans at the start are ignored.
- Per-station cooldown prevents the same runner being counted twice at one station within the cooldown window (default 3 min, configurable).

## Deployment

For a public server (Ubuntu + nginx/TLS + systemd, app bound to `127.0.0.1:8000`),
see **[DEPLOY.md](DEPLOY.md)**.
