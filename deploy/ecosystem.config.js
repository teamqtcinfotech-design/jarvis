// PM2 processes for JARVIS ONLY. Every name starts with "jarvis-" so nothing clashes with other apps on this server.
// Start/reload only through this file:  pm2 reload /srv/jarvis/deploy/ecosystem.config.js --update-env
// Never run "pm2 delete all", "pm2 kill" or "pm2 restart all" on a shared server.
const APP = '/srv/jarvis/backend';
const PORT = process.env.JARVIS_PORT || '8010';
const WALKIE_PORT = process.env.JARVIS_WALKIE_PORT || '8011'; // the walkie-talkie relay; nginx passes /ws/ to it

module.exports = {
  apps: [
    {
      name: 'jarvis-api',
      cwd: APP,
      script: `${APP}/.venv/bin/gunicorn`,
      args: `jarvis.wsgi:application --bind 127.0.0.1:${PORT} --workers 3 --timeout 90 --access-logfile - --error-logfile -`,
      interpreter: 'none',
      uid: 'jarvis', // runs as an unprivileged user, not root
      gid: 'jarvis',
      env: { DJANGO_SETTINGS_MODULE: 'jarvis.settings', PYTHONUNBUFFERED: '1' },
      max_restarts: 10,
      restart_delay: 3000,
    },
    {
      name: 'jarvis-worker',
      cwd: APP,
      script: `${APP}/.venv/bin/python`,
      args: 'manage.py taskpanel_worker',
      interpreter: 'none',
      uid: 'jarvis',
      gid: 'jarvis',
      env: { DJANGO_SETTINGS_MODULE: 'jarvis.settings', PYTHONUNBUFFERED: '1' },
      max_restarts: 10,
      restart_delay: 5000,
    },
    {
      // The walkie-talkie relay: one open line per phone, one speaker at a time (core/walkie.py).
      name: 'jarvis-walkie',
      cwd: APP,
      script: `${APP}/.venv/bin/python`,
      args: `manage.py walkie_server --port ${WALKIE_PORT}`,
      interpreter: 'none',
      uid: 'jarvis',
      gid: 'jarvis',
      env: { DJANGO_SETTINGS_MODULE: 'jarvis.settings', PYTHONUNBUFFERED: '1' },
      max_restarts: 10,
      restart_delay: 3000,
    },
  ],
};
