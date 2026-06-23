# Production deployment

Reference setup: an Ubuntu server with **nginx** terminating TLS (Let's Encrypt)
and reverse-proxying to the app on `127.0.0.1:8000`, managed by **systemd**.

## 1. Get the code onto the server

```bash
sudo mkdir -p /opt/spendenlauf
sudo chown "$USER" /opt/spendenlauf
git clone <your-repo> /opt/spendenlauf
cd /opt/spendenlauf

python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## 2. Configure `.env`

```bash
cp .env.example .env
# Edit .env:
#   ADMIN_USERNAME / ADMIN_PASSWORD  → strong values
#   STATION_API_KEY                  → strong value, must match the firmware
#   COOKIE_SECURE=true               → required behind HTTPS (the default)
```

The SQLite database (`spendenlauf.db`) is created automatically on first start,
next to the app in `/opt/spendenlauf`.

## 3. Run command (what systemd will execute)

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

## 4. systemd service

Create `/etc/systemd/system/spendenlauf.service`:

```ini
[Unit]
Description=Spendenlauf Tracker
After=network.target
# Never give up restarting — by default systemd stops trying after 5 crashes
# in 10s ("start-limit hit"). During an event we always want it to come back.
StartLimitIntervalSec=0

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

`Restart=always` + `StartLimitIntervalSec=0` is what makes the server
self-healing: if the process crashes, runs out of memory, or the machine
reboots, systemd brings it back within ~3s with no human intervention. The
SQLite data survives a restart because it lives on disk — but a crash can still
lose whatever was written in the last few minutes if the disk itself dies, which
is what the backups in the next section guard against.

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

## 5. Database backups (snapshot every few minutes)

The whole event lives in one SQLite file. systemd keeps the *process* alive, but
that won't help if the file is lost or corrupted. So during the event we take a
consistent snapshot every few minutes and keep a rolling history — worst case we
lose only the last few minutes, not the day.

The repo ships [`backup-db.sh`](backup-db.sh). It uses `sqlite3 .backup` (a
proper online backup) rather than `cp`, because the DB runs in **WAL mode** and a
plain copy taken mid-write can be torn or miss the `-wal` file. It also runs an
`integrity_check` on each snapshot and prunes to the most recent `KEEP` files.

Install the `sqlite3` CLI if it isn't already present:

```bash
sudo apt-get install -y sqlite3
```

Drive it from a **systemd timer** (survives reboots, logs to the journal).
Create `/etc/systemd/system/spendenlauf-backup.service`:

```ini
[Unit]
Description=Spendenlauf DB backup
After=spendenlauf.service

[Service]
Type=oneshot
User=www-data
Group=www-data
WorkingDirectory=/opt/spendenlauf
# KEEP=288 × 5 min ≈ 24h of history. Tune to taste.
Environment=APP_DIR=/opt/spendenlauf KEEP=288
ExecStart=/opt/spendenlauf/backup-db.sh
```

And `/etc/systemd/system/spendenlauf-backup.timer`:

```ini
[Unit]
Description=Snapshot the Spendenlauf DB every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
```

Make the script executable, then enable the timer:

```bash
chmod +x /opt/spendenlauf/backup-db.sh
sudo systemctl daemon-reload
sudo systemctl enable --now spendenlauf-backup.timer

sudo systemctl list-timers spendenlauf-backup.timer   # when it next fires
sudo systemctl start spendenlauf-backup.service       # take one now to test
ls -lt /opt/spendenlauf/backups/                      # newest snapshot on top
```

> **Tip:** for real durability, point `BACKUP_DIR` at a different disk or sync
> the `backups/` folder offsite (e.g. `rclone`/`rsync` to cloud storage), so a
> dead disk doesn't take the snapshots with it.
>
> Prefer cron? `*/5 * * * * APP_DIR=/opt/spendenlauf KEEP=288 /opt/spendenlauf/backup-db.sh`.

**Restore** (after stopping the service so nothing is writing):

```bash
sudo systemctl stop spendenlauf
cp /opt/spendenlauf/backups/spendenlauf-YYYYMMDD-HHMMSS.db /opt/spendenlauf/spendenlauf.db
# Clear any stale WAL/SHM sidecars from the old DB:
rm -f /opt/spendenlauf/spendenlauf.db-wal /opt/spendenlauf/spendenlauf.db-shm
sudo chown www-data:www-data /opt/spendenlauf/spendenlauf.db
sudo systemctl start spendenlauf
```

## 6. nginx + TLS

The repo ships an [`nginx.config`](nginx.config) for `spendenlauf.taskminder.de`.
It redirects HTTP -> HTTPS, proxies to `127.0.0.1:8000`, sets security headers (HSTS,
CSP, X-Frame-Options, …) and rate-limits `/api/login` (~5/min per IP).

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
