# JARVIS access door for the live TaskPanel — install checklist

This adds **one new folder** to the live TaskPanel. It is not a new panel: tasks sent by JARVIS are saved in the
same live TaskPanel through its own `TaskService`, so staff see them in TaskPanel / My Tasks as usual.

**Nothing existing is edited:** no Laravel file, no existing TaskPanel file, no existing table.

```
public_html/taskPanel/integration/        ← NEW folder (upload this)
├── .htaccess                             (applies only inside this folder)
├── index.php                             (the door: /taskPanel/integration/v1/...)
├── config.example.php → copy to config.local.php
├── src/                                  (security, commands, change feed, adapter)
├── migrations/001_jarvis_integration.sql (NEW tables only)
└── bin/ grant.php · sync-publish.php · self-check.php  (command-line only)
```

## Before you start
- [ ] Send the JARVIS developer a zip of `public_html/taskPanel` (code only, no `.env`) so the adapter
      (`src/TaskPanelAdapter.php`, lines marked VERIFY) is matched to your exact TaskPanel version.
- [ ] Take a **TaskPanel database backup** and a copy of the `taskPanel` folder.

## Install (about 15 minutes)
1. Upload the `integration` folder to `public_html/taskPanel/integration/`.
2. Copy `config.example.php` to `config.local.php` and fill in:
   - `environment` / `instance_id` (e.g. `production`, `taskpanel-production-01`)
   - one client entry, e.g. `django-production-01`, with a **new 64-character random secret**
     (share it with the JARVIS admin over a secure channel — never email/WhatsApp)
   - `schema` column names (step 4 shows the real ones)
   - keep `'writes_enabled' => false` for now
3. Run the SQL **on the TaskPanel database only**: `migrations/001_jarvis_integration.sql`
   (creates 6 new tables; changes nothing existing).
4. Run the read-only check and send its output to the JARVIS developer:
   `php public_html/taskPanel/integration/bin/self-check.php`
5. Open `https://<your-domain>/taskPanel/integration/v1/health` → must show `{"status":"ok","schema_version":1}`.
   Your normal TaskPanel login and screens must work exactly as before.

## Connect the MD and PA (TaskPanel admin)
```
php bin/grant.php issue --client=django-production-01 --staff-key=<MD user_key> --label="MD" --days=30
php bin/grant.php issue --client=django-production-01 --staff-key=<PA user_key> --label="PA" --days=30
```
Each prints a grant **once**. The JARVIS admin links it (hidden prompt):
```
python manage.py taskpanel_link md --actor-key <MD user_key> --actor-name "Managing Director"
python manage.py taskpanel_link pa --actor-key <PA user_key> --actor-name "PA"
```

## JARVIS server settings (`backend/.env`)
```
TASKPANEL_MODE=live
TASKPANEL_BASE_URL=https://<your-domain>/taskPanel/integration/v1
TASKPANEL_KEY_ID=django-production-01
TASKPANEL_SIGNING_SECRET=<same secret as config.local.php>
TASKPANEL_ENV=production
TASKPANEL_EXPECTED_INSTANCE=taskpanel-production-01
TASKPANEL_WRITES_ENABLED=false     # true only for the pilot, after read-only checks pass
```

## Go-live order (guide §13.3)
1. Read-only: JARVIS loads the staff directory; validation works; **no task is created**.
2. Pilot: set `writes_enabled` true (both sides) → assign **one** test task to a named pilot staff member →
   confirm it appears once in TaskPanel with its normal notification.
3. Reverse check: the staff member starts / completes it in TaskPanel → JARVIS shows Working / Completed.
4. Add the cron line (existing server cron, CLI only):
   `* * * * * /usr/bin/php /path/to/public_html/taskPanel/integration/bin/sync-publish.php >/dev/null 2>&1`

## Switch off / roll back
- Instant: set `'enabled' => false` in `config.local.php` (JARVIS shows "Pending"; TaskPanel is unaffected).
- Revoke a person: `php bin/grant.php revoke --staff-key=<user_key>`.
- Full removal: delete the `integration` folder. The 6 new tables can stay unused; **do not restore the whole
  TaskPanel database** over tasks created after go-live.

## Known limits of this version
- Only **creating** tasks is enabled (start/hold/complete/reassign from the app come in phase 2).
- Changes made in TaskPanel reach JARVIS within ~5 seconds while JARVIS polls (cron fallback: 1 minute).
  Two changes within the same few seconds arrive as the latest state.
- If the server crashes in the split second between TaskPanel saving a task and the door recording its receipt,
  that command is held as "needs review" instead of risking a duplicate.
