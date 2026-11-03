# JARVIS on the VPS — one-time setup

Everything here is **new and separate**: folder `/srv/jarvis`, database `jarvis`, PM2 processes
`jarvis-api` + `jarvis-worker`, nginx site `jarvis`, Linux user `jarvis`.
**Nothing of the existing project is changed.** Never run `pm2 delete all`, `pm2 kill`, `pm2 restart all`.

Run everything as `root` on the VPS (`ssh root@201.18.193.128`).

---

## 0. Look first (read-only, changes nothing)
```bash
pm2 list                                   # note the existing apps — we will not touch them
ss -ltnp | grep -E ':8010 |:5432 |:80 |:443 ' || true   # is port 8010 free? is postgres/nginx running?
systemctl is-active nginx postgresql || true
python3 --version; psql --version 2>/dev/null || echo "no postgres client"
```
- If **8010 is used**, pick another free port (e.g. 8020) and use it in steps 6 and 8.
- If nginx is **not** active (another web server is used), stop and tell the JARVIS developer.

## 1. System packages (adds only; existing services keep running)
```bash
apt-get update
apt-get install -y python3-venv python3-dev libpq-dev git
# Only if step 0 showed NO postgresql:
apt-get install -y postgresql postgresql-contrib
```

## 2. New database and DB user (only for JARVIS)
```bash
DBPASS="$(openssl rand -base64 30 | tr -dc 'A-Za-z0-9' | head -c 32)"; echo "DB password: $DBPASS"   # save it
sudo -u postgres psql -c "CREATE ROLE jarvis LOGIN PASSWORD '$DBPASS';"
sudo -u postgres psql -c "CREATE DATABASE jarvis OWNER jarvis;"
```

## 3. Linux user that runs JARVIS (no login, no root rights)
```bash
id jarvis >/dev/null 2>&1 || useradd --system --home /srv/jarvis --shell /usr/sbin/nologin jarvis
```

## 4. Read-only GitHub deploy key (so the VPS can pull the code)
```bash
ssh-keygen -t ed25519 -f /root/.ssh/jarvis_github -N "" -C "jarvis-vps-pull"
cat >> /root/.ssh/config <<'EOF'

Host github-jarvis
    HostName github.com
    User git
    IdentityFile /root/.ssh/jarvis_github
    IdentitiesOnly yes
EOF
cat /root/.ssh/jarvis_github.pub
```
GitHub → repo **jarvis** → Settings → **Deploy keys** → Add key → paste it → **leave "Allow write access" OFF**.

## 5. Get the code
```bash
git clone git@github-jarvis:teamqtcinfotech-design/jarvis.git /srv/jarvis
```

## 6. Production settings (`.env`) — secrets are generated here, on the server
```bash
cd /srv/jarvis/backend
python3 -m venv .venv && .venv/bin/pip install --quiet -r requirements.txt
cp ../deploy/env.production.example .env
echo "DJANGO_SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(50))')"
echo "FIELD_ENCRYPTION_KEY=$(.venv/bin/python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')"
nano .env
```
Fill in: `DJANGO_SECRET_KEY`, `FIELD_ENCRYPTION_KEY` (from the two lines printed above), `DB_PASSWORD` (step 2),
`ANTHROPIC_API_KEY` (create a new key `jarvis-prod` in the Friday workspace), your domain in
`DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS`, and `JARVIS_PORT` if you changed it.
**Back up `FIELD_ENCRYPTION_KEY`** somewhere safe — without it MD's private tasks cannot be read.
```bash
chown root:jarvis .env && chmod 640 .env
```

## 7. First deploy + accounts
```bash
bash /srv/jarvis/deploy/deploy.sh
cd /srv/jarvis/backend
.venv/bin/python manage.py seed_demo --accounts-only      # prints the md / pa passwords once — save them
.venv/bin/python manage.py set_name pa "Your PA's name"
pm2 list                                                  # jarvis-api + jarvis-worker online; existing apps unchanged
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8010/api/v1/health/   # expect 401 (= running, login required)
```

## 8. Web address with HTTPS (new nginx site only)
Create a DNS **A record** `jarvis.<your-domain>` → `201.18.193.128` first.
```bash
cp /srv/jarvis/deploy/nginx-jarvis.conf /etc/nginx/sites-available/jarvis
sed -i 's/jarvis.example.com/jarvis.YOUR-DOMAIN.com/' /etc/nginx/sites-available/jarvis   # and the port if changed
ln -s /etc/nginx/sites-available/jarvis /etc/nginx/sites-enabled/jarvis
nginx -t && systemctl reload nginx           # reload only if the test says OK
apt-get install -y certbot python3-certbot-nginx   # skip if certbot already exists
certbot --nginx -d jarvis.YOUR-DOMAIN.com
curl -s -o /dev/null -w "%{http_code}\n" https://jarvis.YOUR-DOMAIN.com/api/v1/health/   # expect 401
```
On the phone, log in with server **`https://jarvis.YOUR-DOMAIN.com`**.

## 9. Automatic deploy on every push to `main` (CI/CD)
A separate key that can **only** run the JARVIS deploy script:
```bash
ssh-keygen -t ed25519 -f /root/.ssh/jarvis_ci -N "" -C "jarvis-github-actions"
echo "command=\"bash /srv/jarvis/deploy/deploy.sh\",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty $(cat /root/.ssh/jarvis_ci.pub)" >> /root/.ssh/authorized_keys
cat /root/.ssh/jarvis_ci          # private key → GitHub secret (next step), then you may delete this file
```
GitHub → repo **jarvis** → Settings → Secrets and variables → **Actions** → New repository secret:

| Name | Value |
|---|---|
| `VPS_HOST` | `201.18.193.128` |
| `VPS_USER` | `root` |
| `VPS_SSH_KEY` | the whole private key printed above (from `-----BEGIN` to `-----END ... KEY-----`) |
| `VPS_PORT` | `22` (only if SSH uses another port) |

From now on: **push to `main` → GitHub runs the tests → if green, deploys JARVIS**. Watch it under the repo's **Actions** tab.

## Everyday commands (JARVIS only)
```bash
pm2 logs jarvis-api --lines 50       # server log
pm2 logs jarvis-worker --lines 50    # TaskPanel worker log
pm2 restart jarvis-api jarvis-worker # restart only JARVIS
bash /srv/jarvis/deploy/deploy.sh    # manual deploy of the latest main
```

## Backup of the JARVIS database (recommended daily cron)
```bash
mkdir -p /srv/jarvis-backups && chmod 700 /srv/jarvis-backups
sudo -u postgres pg_dump -Fc jarvis > /srv/jarvis-backups/jarvis-$(date +%F).dump
```
