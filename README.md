# Spendenlauf Tracker

Backend server + live dashboard with admin panel for BLE beacon-based lap counting.

## What it does

Five ESP32 BLE scanners sit around a 2 km loop. Each runner carries a BLE beacon tag. When a scanner detects a beacon, it POSTs a scan event to this server. The server reconstructs laps by tracking sequential checkpoint passes (1 → 2 → 3 → 4 → 5 = one lap), applies per-station cooldown filtering, and feeds a live leaderboard dashboard.

## Quick start (local development)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create your .env (copy the template) and set credentials.
#    For local HTTP, set COOKIE_SECURE=false so the admin session cookie works.
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

> `--host 0.0.0.0` and `--reload` are **development only**. For a public server
> see [Production deployment](#production-deployment) below — it binds to
> localhost behind nginx and drops auto-reload.

## API reference

The interactive Swagger UI (`/docs`, `/redoc`, `/openapi.json`) is **disabled by
default** so the endpoint list isn't publicly advertised. To use it while
developing, pass `docs_url="/docs"` (etc.) to `FastAPI(...)` in `server.py`.

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
POST   /api/runners                 — register a runner (admin only)
GET    /api/runners                 — list all runners + progress
PUT    /api/runners/{bib_number}    — edit a runner (admin only)
DELETE /api/runners/{bib_number}    — remove a runner (admin only)
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
POST   /api/beacons                 — pair a start number with a MAC (admin only)
PUT    /api/beacons/{bib_number}    — edit a pairing (admin only)
DELETE /api/beacons/{bib_number}    — remove a pairing (admin only)
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
POST /api/reset        — wipe all data (admin only)
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

3. The admin area has four sub-tabs:

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

   **Einstellungen** — tune the runtime settings without editing code or
   restarting (station count, station cooldown, fallback lap length, rest
   threshold — see [Configuration](#configuration)). Changes take effect on the
   next scan / dashboard refresh. Changing **Anzahl Stationen** (station count)
   asks for confirmation, since doing so mid-event invalidates in-flight laps.

The map then appears on the main dashboard for everyone, with live runner
counts per station. The map is restricted and defaults to the Munich area.

## Estimated pace

Each runner's rough pace is estimated as distance covered (completed laps ×
measured lap length, plus progress within the current lap) ÷ **moving time**.
Moving time sums the gaps between a runner's scans but excludes any gap longer
than the rest threshold (`moving_gap_max_seconds`, default 5 min): when a runner
leaves the loop to rest in the field they stop generating scans, and that pause
is dropped rather than counted as slow running. (If *every* gap looks like a
rest — e.g. a very slow walker — it falls back to raw elapsed time so a pace is
still shown.) The leaderboard shows a **Pace** column (min/km) per runner and
the stats row shows the field's **Ø Pace**. Distances use the measured route
when one has been drawn, otherwise the configurable fallback lap length.

### Stations & route

```
GET  /api/stations     — list stations with coordinates  (public)
PUT  /api/stations     — update names/coordinates         (admin only)
GET  /api/route        — { route, map_view, segments, lap_distance_m } (public)
PUT  /api/route        — save route + map view + distances (admin only)
GET  /api/config       — current runtime settings          (public)
PUT  /api/config       — update runtime settings           (admin only)
```

`PUT /api/config` accepts any subset of the keys below; each is range-checked
and persisted, taking effect immediately (no restart):

```json
{ "checkpoint_count": 5, "cooldown_seconds": 180,
  "lap_distance_km": 2.0, "moving_gap_max_seconds": 300 }
```

## Configuration

These four settings are tunable at runtime from the admin panel's
**Einstellungen** tab (or `PUT /api/config`). The override is stored in the
database and takes effect on the next scan / refresh — no restart. The constants
at the top of `server.py` are just the **defaults** used until an override is
saved:

| Key / constant                              | Default | Meaning                                              |
| ------------------------------------------- | ------- | ---------------------------------------------------- |
| `checkpoint_count` / `CHECKPOINT_COUNT`     | 5       | Number of stations around the loop                   |
| `cooldown_seconds` / `COOLDOWN_SECONDS`     | 180     | Ignore re-scans of the same station within this window |
| `lap_distance_km` / `LAP_DISTANCE_KM`       | 2.0     | Fallback loop length when no route has been drawn     |
| `moving_gap_max_seconds` / `MOVING_GAP_MAX_SECONDS` | 300 | Scan gaps longer than this count as a rest, excluded from pace |

> **`checkpoint_count` is structural.** Changing it mid-event invalidates
> in-flight laps and assumes the physical stations match, so the admin UI
> confirms before applying it — set it before runners start. The simulator reads
> this value from `/api/config` at startup, so it always matches the server.

`DATABASE` (`spendenlauf.db`, the SQLite file path) remains a code-only constant
in `server.py`.

## Production deployment

Reference setup: an Ubuntu server with **nginx** terminating TLS (Let's Encrypt)
and reverse-proxying to the app on `127.0.0.1:8000`, managed by **systemd**.

### 1. Get the code onto the server

```bash
sudo mkdir -p /opt/spendenlauf
sudo chown "$USER" /opt/spendenlauf
git clone <your-repo> /opt/spendenlauf        # or rsync the folder
cd /opt/spendenlauf

python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 2. Configure `.env`

```bash
cp .env.example .env
# Edit .env:
#   ADMIN_USERNAME / ADMIN_PASSWORD  → strong values
#   COOKIE_SECURE=true               → required behind HTTPS (the default)
```

The SQLite database (`spendenlauf.db`) is created automatically on first start,
next to the app in `/opt/spendenlauf`.

### 3. Run command (what systemd will execute)

```bash
./venv/bin/uvicorn server:app --host 127.0.0.1 --port 8000
```

Differences from the dev command:

- **`--host 127.0.0.1`** — bind to localhost only, so the app is reachable
  *only* through nginx (which adds TLS, security headers and login rate-limiting).
  Never expose port 8000 directly.
- **no `--reload`** — a stable long-running process; no restarts mid-event.
- **single process** — do *not* add `--workers`. Admin sessions live in memory,
  so multiple workers would each have their own store and logins would fail.

### 4. systemd service

Create `/etc/systemd/system/spendenlauf.service`:

```ini
[Unit]
Description=Spendenlauf Tracker
After=network.target

[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=/opt/spendenlauf
ExecStart=/opt/spendenlauf/venv/bin/uvicorn server:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

> `WorkingDirectory` must point at the app folder so the relative
> `spendenlauf.db` path and `.env` resolve correctly. Make sure the `User`
> (e.g. `www-data`) can read the code and write the DB:
> `sudo chown -R www-data:www-data /opt/spendenlauf`.

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now spendenlauf
sudo systemctl status spendenlauf          # check it's running
journalctl -u spendenlauf -f               # follow logs
```

After deploying new code: `sudo systemctl restart spendenlauf`.

### 5. nginx + TLS

The repo ships an [`nginx.config`](nginx.config) for `spendenlauf.taskminder.de`
(adjust `server_name` to your domain). It redirects HTTP→HTTPS, proxies to
`127.0.0.1:8000`, sets security headers (HSTS, CSP, X-Frame-Options, …) and
rate-limits `/api/login` (~5/min per IP).

```bash
sudo cp nginx.config /etc/nginx/sites-available/spendenlauf
sudo ln -s /etc/nginx/sites-available/spendenlauf /etc/nginx/sites-enabled/

# Obtain the certificate (certbot edits the config in place):
sudo certbot --nginx -d spendenlauf.taskminder.de

sudo nginx -t && sudo systemctl reload nginx
```

The `limit_req_zone` directive sits at the top of `nginx.config`; it is valid
there because Ubuntu's `nginx.conf` includes `sites-enabled/*` inside its
`http {}` block.

## Lap reconstruction logic

The algorithm is lenient to handle real-world BLE flakiness:

- Each runner has a "next expected checkpoint" starting at 1.
- Hitting the expected checkpoint advances it (1 → 2 → 3 → 4 → 5).
- Hitting checkpoint 5 (after any forward progress) counts as a completed lap.
- If a runner skips a checkpoint (e.g. hits 4 when expecting 3), the system accepts it and keeps going.
- Out-of-order scans (going backward) are ignored.
- Per-station cooldown prevents the same runner being counted twice at one station within the cooldown window (default 3 min, configurable).

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
