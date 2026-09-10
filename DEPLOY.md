# Customer 360 — server deployment

Two repos, deployed side-by-side with the HF portfolio tool on the same box, mirroring
the portfolio's Docker pattern (backend image on the host network with secrets via
`--env-file`; frontend as a `node:22` bind-mount container). The only coordination
needed is **ports** (no collision with the portfolio) and the **shared JWT signing key**
(so single sign-on works — backlog #12).

| Repo | Contents | Runs on |
| --- | --- | --- |
| `c360_Backennd` (this repo) | Django/DRF API + Dockerfile | backend, host port **9001** |
| `c360_Frontend` | Next.js 16 app | frontend, host port **5401** |

| Service | Host port | Notes |
| --- | --- | --- |
| Portfolio backend / frontend | 9000 / 5400 | existing, untouched |
| **C360 backend** | **9001** | gunicorn, `--network=host` (`PORT=9001`) |
| **C360 frontend** | **5401** | `next start` on container :3000 |

Public URL: **https://ceo.hfcb.co.ke/customer-360** · LAN: **http://128.2.1.25:5401/customer-360**

---

## 0. Clone both repos

```bash
sudo mkdir -p /data/apps/customer360
sudo chown "$USER":"$USER" /data/apps/customer360
cd /data/apps/customer360
git clone https://github.com/allanaswani/c360_Backennd.git
git clone https://github.com/allanaswani/c360_Frontend.git
```

This leaves the repos at their own names: `/data/apps/customer360/c360_Backennd`
and `/data/apps/customer360/c360_Frontend` (used in every path below).

---

## 1. Backend secrets — `/etc/hf/c360.env`

Copy the template and fill it in. **Never commit this file** (it holds the Trino
password and the SSO signing key).

```bash
sudo mkdir -p /etc/hf
cp /data/apps/customer360/c360_Backennd/.env.example /etc/hf/c360.env
sudo nano /etc/hf/c360.env
```

The lines that must be set:

- `C360_JWT_SIGNING_KEY` → **the portfolio backend's `SECRET_KEY`, verbatim.** This is
  what makes a portfolio-issued token validate here (SSO). Nothing else works without it.
- `DJANGO_SECRET_KEY` → a fresh unique value, C360's own:
  `python3 -c "import secrets;print(secrets.token_urlsafe(64))"`
- `C360_DATA_MODE=live` and the Trino block (`TRINO_HOST`, `TRINO_PORT=8443`,
  `TRINO_USER`, `TRINO_PASSWORD`, `TRINO_HTTP_SCHEME=https`, `TRINO_VERIFY_SSL=false`).
- `DJANGO_DEBUG=false`, `C360_REQUIRE_AUTH=true`, `C360_COOKIE_SECURE=true`, `PORT=9001`.
- `DJANGO_ALLOWED_HOSTS=ceo.hfcb.co.ke,128.2.1.25,localhost,127.0.0.1`.
- `C360_CORS_ORIGINS` / `C360_CSRF_TRUSTED_ORIGINS=https://ceo.hfcb.co.ke,http://128.2.1.25:5401`.
- App DB: simplest is SQLite on a bind-mount so it survives `docker rm` — set
  `DB_NAME=/app/appdb/db.sqlite3` and keep the `-v …/appdb:/app/appdb` mount below.
  (Better long-term: point `DB_*` at a dedicated database on the portfolio's Postgres.)
- Email (`EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD`) — needed for real OTP delivery.
  **Also set `C360_EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend`** — the
  default is the console backend, which prints mail to the container log and sends
  nothing. Without this the operations reports below appear to work and never arrive.
- Operational reporting (section 7): `C360_REPORT_FROM`, `C360_REPORT_RECIPIENTS`,
  `C360_APP_URL=https://ceo.hfcb.co.ke/customer-360`.

---

## 2. Backend — build, run, first-run migrations

```bash
cd /data/apps/customer360/c360_Backennd
docker build -t c360-backend:latest .
docker rm -f c360-backend

docker run -d --name c360-backend --restart unless-stopped \
  --network=host \
  --env-file /etc/hf/c360.env \
  -v /data/apps/customer360/appdb:/app/appdb \
  c360-backend:latest

# First deploy only, in order:
docker exec c360-backend python manage.py migrate
docker exec c360-backend python manage.py seed_roles
docker exec -e C360_ADMIN_PASSWORD='pick-a-strong-password' c360-backend \
  python manage.py bootstrap_admin --username admin --email ops@hfcb.co.ke

# Optional: confirm the warehouse is reachable from the box
docker exec c360-backend python manage.py smoke_test_connections

docker logs -f c360-backend
```

---

## 3. Frontend — run (node:22, same pattern as the portfolio)

```bash
cd /data/apps/customer360/c360_Frontend
docker rm -f c360-frontend

docker run -d --name c360-frontend --restart unless-stopped \
  -p 5401:3000 -v "$(pwd)":/app -w /app \
  -e NEXT_PUBLIC_BASE_PATH=/customer-360 \
  -e NEXT_PUBLIC_API_BASE=/customer-360/api \
  -e C360_BACKEND_ORIGIN=http://127.0.0.1:9001 \
  node:22 \
  sh -c "npm install && npm run build && npm start"

docker logs -f c360-frontend
```

These three envs are read at build time: `BASE_PATH` serves the app under
`/customer-360`; `API_BASE` makes the SPA call that same-origin path; `C360_BACKEND_ORIGIN`
lets the Next server proxy `/customer-360/api/*` → `:9001` — which is what makes
**LAN-direct** access (`128.2.1.25:5401`, no nginx) work.

---

## 4. nginx (public host only) — inside the existing `server_name ceo.hfcb.co.ke` block

```nginx
# API — strip the /customer-360 prefix, forward to the C360 backend.
location /customer-360/api/ {
    proxy_pass http://127.0.0.1:9001/api/;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
# Frontend (Next.js, basePath=/customer-360) — keep the prefix.
location /customer-360/ {
    proxy_pass http://127.0.0.1:5401;
    proxy_http_version 1.1;
    proxy_set_header Upgrade           $http_upgrade;
    proxy_set_header Connection        "upgrade";
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

On the LAN nobody goes through nginx — users hit `http://128.2.1.25:5401/customer-360`
directly and the Next server proxies the API for them.

---

## 5. Single sign-on checklist (backlog #12)

1. `C360_JWT_SIGNING_KEY` == portfolio backend `SECRET_KEY`.
2. `C360_REQUIRE_AUTH=true`, `DJANGO_DEBUG=false`, `C360_COOKIE_SECURE=true`.
3. `DJANGO_ALLOWED_HOSTS` includes `ceo.hfcb.co.ke` and the LAN IP.
4. Portfolio side deployed too: the enriched-token serializer on the portfolio
   **backend** (so its tokens carry the identity/RBAC claims) and the "Open Customer 360"
   launcher card on the portfolio **frontend**. With both apps on the same host, C360
   adopts the portfolio's login cookie on boot — no second login.

---

## 6. Redeploys

```bash
# Backend
cd /data/apps/customer360/c360_Backennd && git pull
docker build -t c360-backend:latest . && docker rm -f c360-backend
docker run -d --name c360-backend --restart unless-stopped --network=host \
  --env-file /etc/hf/c360.env -v /data/apps/customer360/appdb:/app/appdb c360-backend:latest
docker exec c360-backend python manage.py migrate   # if models changed

# Frontend
cd /data/apps/customer360/c360_Frontend && git pull
docker rm -f c360-frontend
docker run -d --name c360-frontend --restart unless-stopped \
  -p 5401:3000 -v "$(pwd)":/app -w /app \
  -e NEXT_PUBLIC_BASE_PATH=/customer-360 -e NEXT_PUBLIC_API_BASE=/customer-360/api \
  -e C360_BACKEND_ORIGIN=http://127.0.0.1:9001 \
  node:22 sh -c "npm install && npm run build && npm start"
```

---

## 7. Operations reports and alerts (email)

The app measures its own health, but nothing leaves the box on its own — these
commands do, and they only run if you schedule them. Skip this section and the
dashboards still work; you simply won't hear about an outage at 02:00.

Recipients and sender come from the env file (section 1):

```bash
C360_EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
C360_REPORT_FROM=HFCB Customer 360 Reports <reports.analytics@hfcb.co.ke>
C360_REPORT_RECIPIENTS=washingtone.amolo@hfcb.co.ke,allan.aswani@hfcb.co.ke
C360_APP_URL=https://ceo.hfcb.co.ke/customer-360
```

Alert thresholds (all optional, shown with their defaults):

```bash
C360_ALERT_ERROR_RATE_PCT=5       # server-error rate over the last hour
C360_ALERT_P95_MS=2000            # p95 latency over the last hour
C360_ALERT_DATA_STALE_DAYS=3      # warehouse as-of date falling behind
C360_ALERT_RENOTIFY_MINUTES=360   # quiet period before an OPEN incident re-sends
```

### Check it before you schedule it

Every command has `--dry-run`: it builds the whole report, prints what it would
send, and sends nothing.

```bash
docker exec c360-backend python manage.py send_ops_report --period daily --dry-run
docker exec c360-backend python manage.py check_ops_alerts --dry-run
docker exec c360-backend python manage.py send_error_log --minutes 60 --dry-run
```

To send a one-off to yourself without touching the configured list:

```bash
docker exec c360-backend python manage.py send_ops_report --period daily --to you@hfcb.co.ke
```

### The schedule — `sudo crontab -e` on the host

```cron
# Incident alerts. Safe to run often: state is kept in c360_alert_state, so an
# incident sends ONE email when it starts, at most one reminder every
# C360_ALERT_RENOTIFY_MINUTES while it stays open, and one when it clears.
*/5 * * * *  docker exec c360-backend python manage.py check_ops_alerts >/dev/null 2>&1

# Error log, batched hourly. Silent when the hour had no errors.
7 * * * *    docker exec c360-backend python manage.py send_error_log --minutes 60 >/dev/null 2>&1

# Daily digest, 07:00 — yesterday's traffic, errors, latency, data health,
# usage and every administrative change, with the full tables attached.
0 7 * * *    docker exec c360-backend python manage.py send_ops_report --period daily >/dev/null 2>&1

# Weekly rollup, Monday 07:15.
15 7 * * 1   docker exec c360-backend python manage.py send_ops_report --period weekly >/dev/null 2>&1
```

Cron runs in UTC unless the host says otherwise — check with `timedatectl`, and
shift the hours if the box is not on Africa/Nairobi.

**Do not add `--dry-run` to the cron lines.** And note the alert job is the one
that must run on a short interval; the digests are the ones that must not (a
duplicated digest is noise, a missed alert is an outage nobody saw).

### What arrives

| Command | When | Sends |
|---|---|---|
| `check_ops_alerts` | on a state change | Warehouse unreachable, data stale, a source emptied or sharply down, error rate or p95 over threshold, no traffic during working hours — and a matching "resolved" mail |
| `send_error_log` | hourly, only if errors | Every 4xx/5xx grouped by endpoint, CSV + Excel attached |
| `send_ops_report --period daily` | 07:00 | Traffic, error rate, latency, uptime, data health, who used it, every administrative change |
| `send_ops_report --period weekly` | Mon 07:15 | The same over seven days |
