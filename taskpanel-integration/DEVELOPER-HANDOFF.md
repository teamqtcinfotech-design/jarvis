# JARVIS ↔ TaskPanel — handoff for the TaskPanel developer

**Audience:** the developer who built and deployed the live `public_html/taskPanel`.
**Goal:** let the JARVIS app (MD's voice assistant) create staff tasks in the **live TaskPanel** and read their
progress back — **without editing any existing TaskPanel or Laravel file**.
**Spec it follows:** *TaskPanel and VPS AI Integration Guide* (Shiv Trinetra, 3 Nov 2026) — sections cited as §.

---

## 1. What we are asking you to do (summary)

1. Review the new folder `integration/` (attached) — it goes inside the live TaskPanel as
   `public_html/taskPanel/integration/`. It is an **access door**, not a new panel.
2. Fill the few places marked **`VERIFY`** in `src/TaskPanelAdapter.php` with your real class/method/column
   names (section 4 below), **or** send us a zip of the `taskPanel` code and we will do it.
3. Install on staging first, then live (checklist in `README-INSTALL.md`).
4. Issue two grants (MD and PA) with `bin/grant.php`, and share the integration secret securely.

Estimated effort for you: **2–4 hours** (review + VERIFY answers + install), plus a short pilot test with us.

## 2. Hard rules this design keeps (please confirm nothing breaks them)

| Rule | How |
|---|---|
| No existing file edited (Laravel or TaskPanel) | Everything is in the new `integration/` folder with its **own** `.htaccess`; TaskPanel's `.htaccess` is untouched |
| No existing table altered | 6 **new** tables in the TaskPanel DB only (`migrations/001_jarvis_integration.sql`) |
| Business rules stay in TaskPanel | Task creation goes **only** through your existing `TaskService::create` — never a direct INSERT into task tables. Category, department, leave, activity and notifications all run as today |
| Main software untouched | No Laravel `APP_KEY`, sessions, main DB password, or shared MySQL user is given to JARVIS. The door never uses `SessionBridge` or browser CSRF |
| JARVIS cannot impersonate anyone | Every request needs **both** a server signature (HMAC-SHA256, per-environment key) **and** a per-person grant issued by you (§5.1, §9.1). The sender is taken from the grant, never from request JSON |
| Existing panel keeps working if JARVIS is down | The door is passive; if it is disabled or removed, TaskPanel behaves exactly as now |

## 3. How it works

```
JARVIS phone app ──► JARVIS server (Django, laptop/VPS)
                          │  signed HTTPS + actor grant
                          ▼
   public_html/taskPanel/integration/   (NEW door)
        ├─ checks signature, timestamp (±300 s), one-time nonce, grant, scopes
        ├─ GET  /v1/directory          staff (user_key, name, department, leave) + categories
        ├─ POST /v1/drafts/validate    same checks as creation, writes nothing
        ├─ POST /v1/commands           task.create → YOUR TaskService::create (idempotent by command_id)
        ├─ GET  /v1/commands/{id}      receipt lookup after a timeout (no duplicates)
        ├─ GET  /v1/tasks/{uuid}, /v1/tasks/snapshot
        └─ GET  /v1/changes            events: created / started / held / completed (actor-scoped)
                          │
                          ▼
   Live TaskPanel (same tasks, same staff screens, same notifications)
```

**Change feed without hooks:** because existing files must not change, the door *detects* changes: it re-reads
tasks whose `updated_at` (or new activity rows) moved since the last scan, compares an allowlisted snapshot hash,
and appends an event only when something changed. This catches changes from the web UI, cron, leave transfer —
every path. It runs on demand (at most every 3 s) and from a 1-minute cron fallback.

## 4. VERIFY — what we need from your code (answer these or send the zip)

Please answer each item (file + class + method/column). These are the only places that depend on your code.

1. **Bootstrapping without a browser:** how does `cron.php` load TaskPanel services and the DB? (Composer
   autoload path; does it `require bootstrap/app.php`; anything that would start a session we must avoid?)
2. **TaskPanel DB connection:** exact class/method returning the TaskPanel **own** PDO (guide says `Database::own()`) — full namespace.
3. **Staff directory:** the `StaffService` method that lists assignable staff with **user_key**, display name,
   assignment department, active flag. Is the stable key `user_key`, `source_admin_id`, or both?
4. **Leave:** how to know if a person is on leave today and until when (`LeaveService` method/fields).
5. **Categories:** where categories and their department rules live (`config/master.json`?), and the JSON shape.
   Which category is the safe "General" one?
6. **Creating a task:** exact `TaskService::create(...)` signature and input keys. We use the documented browser
   fields `cat, title, detail, to, pri, manual=true, dueKind=custom, dueDate, dueTime, remarks` (§6.2). How does it
   receive the **acting user/sender** (the browser path gets it from `SessionBridge`)? What does it return (id or `T-id`)?
7. **Date-only deadlines:** which `dueKind` value and default time should a date-only task use (we assumed 18:30 IST)?
   Is there a same-day cutoff?
8. **Task table & columns** (read-only use): table name and columns for id, title, status (`todo/working/waiting/done`),
   priority (`normal/high/urgent`), assignee key, sender key, category, due date/time, `version`, completed time,
   **updated time**, hold reason.
9. **Does `updated_at` change on every mutation** — start, hold, complete, reassign, carry forward, notes?
   If not, which column or activity row reflects each change? (Activity table name + `task_id` + created time.)
10. **Who may read a task:** sender and assignee only, or also admins/viewers? (the feed filters by this)
11. **MD and PA:** do they have TaskPanel staff identities? Please give their **user_key** values.
12. **Hosting:** Apache with `mod_rewrite` and `AllowOverride` for a subfolder `.htaccess`? PHP CLI path for cron?
    MySQL or MariaDB version (we use `GET_LOCK`)? Staging copy available?

## 5. Install (details in `README-INSTALL.md`)

1. Backup TaskPanel DB + folder. 2. Upload `integration/`. 3. `config.local.php` from the example
(writes **off**). 4. Run the SQL on the TaskPanel DB. 5. `php integration/bin/self-check.php` → send us the output.
6. `/taskPanel/integration/v1/health` returns `{"status":"ok"}`; normal TaskPanel login still works.
7. Issue grants: `php integration/bin/grant.php issue --client=<key id> --staff-key=<user_key> --label="MD" --days=30`
   (and PA). 8. Add the cron line for `bin/sync-publish.php`.

## 6. What to send back to us (securely — not email/WhatsApp for secrets)

| Item | Example |
|---|---|
| Answers to section 4 **or** a zip of `public_html/taskPanel` (code only, no `.env`) | — |
| Output of `bin/self-check.php` | text |
| Base URL | `https://<domain>/taskPanel/integration/v1` |
| Key id, environment, instance id | `django-production-01`, `production`, `taskpanel-production-01` |
| Integration secret (64 random chars) | via password manager / in person |
| MD + PA grants from `grant.php` | via password manager / in person |
| A pilot staff member who may receive test tasks | name + user_key |

## 7. Joint test plan (staging first, guide §12)

| # | Test | Pass condition |
|---|---|---|
| 1 | Health, directory, validate with writes **off** | Staff + categories load; validate works; **no task created** |
| 2 | Wrong signature / old timestamp / reused nonce / wrong grant | 401, nothing changes |
| 3 | Create one task to the pilot staff member | Exactly **one** task in TaskPanel with normal activity + notification |
| 4 | Send the same command again | Same task returned (`replayed: true`), no duplicate |
| 5 | Same command id with changed data | 409, original kept |
| 6 | Assign to someone on leave / wrong department category | 422 with a clear reason, nothing saved |
| 7 | Pilot starts, holds, completes it in TaskPanel | JARVIS shows Working / Hold / Completed within ~5 s |
| 8 | Disable (`enabled => false`) | JARVIS shows "Pending"; TaskPanel unaffected |

## 8. Rollback

Set `'enabled' => false` (instant) or delete the `integration/` folder. Revoke people with
`bin/grant.php revoke`. The new tables can stay unused. **Do not restore the full TaskPanel DB** over real tasks.

## 9. Known limits (phase 1)

Create only (start/hold/complete/reassign from the app are phase 2). Rapid changes within one scan arrive as the
latest state. A crash in the instant between `TaskService` commit and the door's receipt is held as
"needs review" (never duplicated). If you prefer, phase 2 can replace detection with in-transaction events once
editing TaskPanel files is approved.

Questions: contact the JARVIS developer (QTC INFOTECH).
