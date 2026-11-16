#!/usr/bin/env bash
# Deploy JARVIS on the VPS. Used by GitHub Actions on every push to main, and safe to run by hand.
# It touches ONLY /srv/jarvis and the PM2 processes jarvis-api / jarvis-worker.
set -euo pipefail

APP_DIR=/srv/jarvis
LOCK=/tmp/jarvis-deploy.lock

# ---- Over-the-air app updates, sent by GitHub Actions:
#        deploy.sh app-update publish <commit>   (the app's code arrives as a .tgz on stdin)
#        deploy.sh app-update status|promote|rollback
# GitHub's key may run nothing but this script (a forced command in authorized_keys), so the words it asked for
# also arrive in SSH_ORIGINAL_COMMAND. Only these exact actions are accepted.
ARGS=("$@")
if [ ${#ARGS[@]} -eq 0 ] && [ -n "${SSH_ORIGINAL_COMMAND:-}" ]; then
  read -r -a ARGS <<< "${SSH_ORIGINAL_COMMAND#bash /srv/jarvis/deploy/deploy.sh}"
fi
if [ "${ARGS[0]:-}" = "app-update" ]; then
  cd "$APP_DIR/backend"
  case "${ARGS[1]:-}" in
    publish)
      SHA="${ARGS[2]:-}"
      [[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "app-update publish: give the full commit id" >&2; exit 2; }
      mkdir -p "$APP_DIR/updates-store/incoming"
      FILE="$APP_DIR/updates-store/incoming/app-update-$SHA.tgz"
      head -c 209715200 > "$FILE"   # at most 200 MB
      exec .venv/bin/python manage.py app_update publish "$FILE" --commit "$SHA"
      ;;
    status|promote|rollback)
      exec .venv/bin/python manage.py app_update "${ARGS[1]}"
      ;;
    *)
      echo "app-update: unknown action (publish, status, promote, rollback)" >&2
      exit 2
      ;;
  esac
fi

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

echo "==> App update store (GitHub uploads the app's code here; the server reads it)"
mkdir -p "$APP_DIR/updates-store/incoming"
find "$APP_DIR/updates-store/incoming" -name '*.tgz' -mtime +7 -delete 2>/dev/null || true

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
