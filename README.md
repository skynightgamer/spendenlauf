# Spendenlauf Tracker

Backend server + live dashboard with admin panel for BLE beacon-based lap counting.

## What it does

Five ESP32 BLE scanners sit around a 2 km loop. Each runner carries a BLE beacon tag. When a scanner detects a beacon, it POSTs a scan event to this server. The server reconstructs laps by tracking sequential checkpoint passes (1 → 2 → 3 → 4 → 5 = one lap), applies per-station cooldown filtering, and feeds a live leaderboard dashboard.

## Quick start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Start the server
uvicorn server:app --host 0.0.0.0 --port 8000 --reload

# 3. In a second terminal, run the simulator (if necessary)
# registers 20 fake runners and sends scan events at 10× real time. Dashboard updates every 3 seconds.
# options:
# python simulate.py --runners 10    # fewer runners
# python simulate.py --speed 20      # 20× real time (faster)
# python simulate.py --reset         # wipe all data before starting
# python simulate.py --url http://192.168.1.50:8000   # remote server
python simulate.py --reset
```

## API reference

All endpoints are also documented at `/docs` (Swagger UI) when the server is running.

### Scan ingestion (from ESP32)

```
POST /api/scan
{
  "station_id": 3,
  "beacon_mac": "AA:BB:CC:DD:EE:01",
  "timestamp": "2026-06-19T10:23:45",
  "rssi": -65
}
```

Response:

```json
{ "status": "ok", "event": "checkpoint", "laps": 1 }
```

Events: `started`, `checkpoint`, `lap_complete`, `checkpoint_skipped`, `lap_complete_skipped`, `out_of_order`, `waiting_for_start`.

### Runner management

```
POST   /api/runners                 — register a runner
GET    /api/runners                 — list all runners + progress
PUT    /api/runners/{bib_number}    — edit a runner (admin only)
DELETE /api/runners/{bib_number}    — remove a runner
```

A runner is linked to its beacon through the **start number** (`bib_number`), so
the matching beacon must be configured first (see below). Register body:

```json
{
  "vorname": "Lena",
  "nachname": "Müller",
  "bib_number": 1,
  "donation_per_km": 2.50
}
```

### Beacon configuration

A beacon pairs a start number with the MAC address of its BLE tag. Scan events
are matched to runners via this pairing (`beacon_mac` → `bib_number` → runner).

```
GET    /api/beacons                 — list beacons (+ whether assigned to a runner)
POST   /api/beacons                 — pair a start number with a MAC
PUT    /api/beacons/{bib_number}    — edit a pairing (admin only)
DELETE /api/beacons/{bib_number}    — remove a pairing
```

Body:

```json
{ "bib_number": 1, "beacon_mac": "AA:BB:CC:DD:EE:01" }
```

### Dashboard data

```
GET /api/leaderboard   — ranked runner list with laps, distance, donations
GET /api/stats         — aggregate totals (laps, km, donations, active count)
```

### Admin

```
POST /api/reset        — wipe all data (testing only)
```

## Admin login & station map

The dashboard shows a live map (Leaflet + OpenStreetMap) with the station
locations and the planned running route. Both are set up in the admin area.

### Setup

1. Create a `.env` file (copy `.env.example`) with the admin credentials:

   ```
   ADMIN_USERNAME=admin
   ADMIN_PASSWORD=your-secret-password
   ```

2. Open `http://localhost:8000/admin` and log in.

3. The admin area has three sub-tabs:

   **Karten-Konfiguration** — the station/route map editor:
   - **Stations** — drag the numbered markers onto their real-world spots; rename them inline.
   - **Entlang Straßen** — auto-connects the stations into a lap that follows
     real footpaths (incl. trails / Feldwege, e.g. in the Englischer Garten),
     using the public FOSSGIS foot router. It also shows the **lap length** and
     the **distance between each pair of stations**.
   - **Speichern** — saves station coordinates, the route, the measured distances,
     and the current map view.

   **Läufer-Konfiguration** — add, edit and delete runners (Vorname, Nachname,
   donation €/km). The start number is picked from a dropdown of configured
   beacons, so each runner is tied to a beacon.

   **Beacon-Konfiguration** — pair start numbers with beacon MAC addresses
   (add / edit / delete). Configure beacons here before adding runners.

The map then appears on the main dashboard for everyone, with live runner
counts per station. The map is restricted and defaults to the Munich area.

## Estimated pace

Each runner's rough pace is estimated from the time between their station
passes: distance covered (completed laps × measured lap length, plus progress
within the current lap) ÷ elapsed time since their first scan. The leaderboard
shows a **Pace** column (min/km) per runner and the stats row shows the field's
**Ø Pace**. Distances use the measured route when a route has been drawn,
otherwise they fall back to the `LAP_DISTANCE_KM` constant.

### Stations & route

```
GET  /api/stations     — list stations with coordinates  (public)
PUT  /api/stations     — update names/coordinates         (admin only)
GET  /api/route        — { route, map_view, segments, lap_distance_m } (public)
PUT  /api/route        — save route + map view + distances (admin only)
```

## Configuration

Edit the constants at the top of `server.py`:

| Constant           | Default | Meaning                                    |
| ------------------ | ------- | ------------------------------------------ |
| `CHECKPOINT_COUNT` | 5       | Number of stations around the loop         |
| `COOLDOWN_SECONDS` | 180     | Ignore re-scans within this window (3 min) |
| `LAP_DISTANCE_KM`  | 2.0     | Loop length for distance calculations      |
| `DATABASE`          | `spendenlauf.db` | SQLite file path                  |

## Lap reconstruction logic

The algorithm is lenient to handle real-world BLE flakiness:

- Each runner has a "next expected checkpoint" starting at 1.
- Hitting the expected checkpoint advances it (1 → 2 → 3 → 4 → 5).
- Hitting checkpoint 5 (after any forward progress) counts as a completed lap.
- If a runner skips a checkpoint (e.g. hits 4 when expecting 3), the system accepts it and keeps going.
- Out-of-order scans (going backward) are ignored.
- Per-station cooldown prevents the same runner being counted twice at one station within 3 minutes.

## ESP32 firmware

The ESP32 stations have firmware that:

The code is available under: https://github.com/skynightgamer/spendenlauf_firmware

1. Scans for BLE advertisements
2. Filters by known beacon MACs or a UUID prefix
3. On detection, POSTs to this server:

```
POST http://<server-ip>:8000/api/scan
Content-Type: application/json

{"station_id":1, "beacon_mac":"AA:BB:CC:DD:EE:01", "timestamp":"...", "rssi":-62}
```

The ESP32 connects to a shared Wi-Fi network (phone hotspot or portable router) and uses the server's LAN IP.

## Project structure

```
spendenlauf/
├── server.py           ← FastAPI backend
├── static/
│   ├── dashboard.html  ← live dashboard + map (served at /)
│   └── admin.html      ← admin login + map / runner / beacon config (served at /admin)
├── simulate.py         ← test data generator
├── .env                ← admin credentials (copy from .env.example)
├── .env.example        ← template for .env
├── requirements.txt    ← Python dependencies
└── README.md           ← this file
```
