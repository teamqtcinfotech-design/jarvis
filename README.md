# JARVIS — private executive assistant (first-look build)

```
backend/                Django + DRF + PostgreSQL · AI brain (Claude Haiku 5.5 / Sonnet 5.5) · TaskPanel connector
mobile/                 React Native (Expo SDK 58, Expo Router) Android app for MD and PA
taskpanel-integration/  NEW "access door" folder to add inside the live public_html/taskPanel (no existing file edited)
```

Staff tasks live in the **live TaskPanel**. JARVIS reaches it only through the access door
(signed requests, per-person grants). See `taskpanel-integration/README-INSTALL.md`.
Until the door is installed, the backend runs `TASKPANEL_MODE=mock`, a test double with the same rules
(QTC Developer vs QTC Developer2, Nikki on leave, department/category checks).

## Terminals while developing
| Terminal | Command | Purpose |
|---|---|---|
| 1 | `cd D:\Friday\backend; .\.venv\Scripts\python.exe manage.py runserver 0.0.0.0:8000` | API server |
| 2 | `cd D:\Friday\backend; .\.venv\Scripts\python.exe manage.py taskpanel_worker` | sends staff tasks to TaskPanel, brings back Started/Hold/Completed |
| 3 | `adb reverse tcp:8000 tcp:8000; cd D:\Friday\mobile; npx expo run:android --variant release` | phone build (USB) |

## Run the server (this PC)

```powershell
cd D:\Friday\backend
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py seed_demo          # first time: creates md / pa and prints passwords once
.\.venv\Scripts\python.exe manage.py runserver 0.0.0.0:8000
```
The phone must be on the same Wi-Fi. In the app's login screen use `<PC IP>:8000` (e.g. `192.168.29.42:8000`).
If the phone cannot connect, allow Python through Windows Firewall on **Private** networks.

Tests (privacy boundary, approvals, idempotency, Hinglish parser): `.\.venv\Scripts\python.exe manage.py test core`

## Build and install the APK (USB debugging)

Needs Java 17 (`JAVA_HOME`) and the Android SDK (`ANDROID_HOME`), then a **new** terminal.
```powershell
cd D:\Friday\mobile
adb devices                                   # phone must show as "device"
npx expo prebuild --platform android          # regenerates mobile\android from app.json (needed after app.json or plugin changes)
npx expo run:android --variant release        # builds and installs a standalone APK on the phone
```
The APK is written to `mobile\android\app\build\outputs\apk\release\app-release.apk` (shareable by USB/WhatsApp to sideload).

## Live push (Firebase Cloud Messaging)

Alerts (requests, tasks for Mansi, follow-ups, the morning briefing) are pushed to the phones the moment they are
created, so a closed app shows and speaks them within seconds. `mobile/google-services.json` is the Android app's
config inside the shared Firebase project. The server needs the project's service-account key:
`FIREBASE_SERVICE_ACCOUNT_FILE=/srv/jarvis/firebase.json` on the VPS (copied there by hand, never committed).
Without it push is off and the phones fall back to a half-minute check.

## Who sees what

| | MD | PA |
|---|---|---|
| Personal (private) tasks | ✅ encrypted at rest, MD only | ❌ never — not titles, counts, search or AI |
| Assign to Laravel staff | ✅ | ✅ (MD sees it in Delegated) |
| Task for MD | creates directly | sends a request → MD accepts / declines |
| Change MD's task | edits directly | proposes → MD approves |
| MD calendar | full | "Busy 11–12" only |

## Smart alerts

Speak when he is with the phone · vibrate silently with generic text during a meeting / face down / quiet hours (urgent only) ·
hold when the phone has been still (away) and say **"While you were away…"** when he picks it up · urgent repeats at 5 and 15 min
until acknowledged · every reminder is also an exact OS alarm so nothing is lost if the app is closed.

## Demo script (5 minutes)

1. Login as **md** → greeting, presence bar "With you", Mansi's request is announced aloud → **Accept**.
2. Tap mic: *"kal subah saade gyarah baje Sharma franchise ke saath meeting, urgent"* → card shows tomorrow 11:30 AM, Urgent → **Save**.
3. Mic: *"Ramesh ko bolo parso tak GST reconciliation bhej de"* → JARVIS asks *which Ramesh* → pick Ramesh Kumar → **Assign**.
4. **Briefing** → Sonnet reads the day aloud.
5. Settings → "away after 1 min", put the phone down; when a reminder fires it is held; pick it up → "While you were away…".
6. Login as **pa** on a second phone → sees "Busy" blocks only, no MD private tasks; send *"Sir ke liye kal 3 baje bank meeting"* → MD's phone announces it.

## What is real vs test mode (be explicit with the client)

- **Real:** AI command understanding and briefings (Claude), voice (Android speech), privacy rules, approvals, reminders.
- **Test mode:** TaskPanel (`TASKPANEL_MODE=mock`, test staff list; staff tasks move to Working after ~2 min
  and Completed after ~5 min for the demo). Switch to live after installing `taskpanel-integration/`.
- **Pending decisions:** "all staff" (each person / a department / an announcement), MD & PA TaskPanel logins,
  category policy (AI-suggested vs always General). JARVIS currently asks you to name one person.
- **Not in this build:** Windows app, calendar sync, next-phase audit fixes.
