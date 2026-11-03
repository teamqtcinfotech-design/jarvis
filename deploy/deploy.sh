#!/usr/bin/env bash
# Deploy JARVIS on the VPS. Used by GitHub Actions on every push to main, and safe to run by hand.
# It touches ONLY /srv/jarvis and the PM2 processes jarvis-api / jarvis-worker.
set -euo pipefail

APP_DIR=/srv/jarvis
LOCK=/tmp/jarvis-deploy.lock

exec 9>"$LOCK"
flock -n 9 || { echo "Another JARVIS deploy is running; skipping."; exit 0; }

cd "$APP_DIR"
echo "==> Fetching latest main"
git fetch --quiet origin main
git reset --hard origin/main   # .env and other untracked files are kept

cd "$APP_DIR/backend"
if [ ! -f .env ]; then
  echo "ERROR: $APP_DIR/backend/.env is missing (copy deploy/env.production.example and fill it in)." >&2
  exit 1
fi
[ -d .venv ] || python3 -m venv .venv

echo "==> Installing Python packages"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo "==> Checking, migrating, collecting static files"
.venv/bin/python manage.py check
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py collectstatic --noinput --verbosity 0

echo "==> Reloading JARVIS processes only"
JARVIS_PORT="$(grep -E '^JARVIS_PORT=' .env | tail -n1 | cut -d= -f2 | tr -d '[:space:]' || true)"
export JARVIS_PORT="${JARVIS_PORT:-8010}"
if pm2 describe jarvis-api >/dev/null 2>&1; then
  pm2 reload "$APP_DIR/deploy/ecosystem.config.js" --update-env
else
  pm2 start "$APP_DIR/deploy/ecosystem.config.js"
fi
pm2 save >/dev/null

echo "==> JARVIS deployed at commit $(git -C "$APP_DIR" rev-parse --short HEAD)"
