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

## 5. nginx + TLS

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
