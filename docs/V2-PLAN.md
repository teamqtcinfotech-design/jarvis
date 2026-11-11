# JARVIS version 2 — master list

Everything found on 10–11 November 2026, merged and de-duplicated, in the order it is being built.
Status: `[ ]` open, `[~]` in progress, `[x]` done (commit). Items marked **APK** need a phone build.

## Wave 1 — language and listening

- [x] 1.1 One reply-language rule per person: decided from the script and words of what was said (plus the model's
      vote), remembered turn by turn, pinnable in Settings ("match me" / always Hindi / always English). Applied to
      every spoken line, server-built ones included (confirmations, saved lines, alerts, reminders, check-ins,
      nudges, briefing, the alert questions). Alerts and reminders carry both languages; the server picks at delivery.
      (`core/lang.py`, `lib/lang.ts`; found on the way: reminder lines were frozen into `announcement` at save time,
      so a changed form of address or language never reached them — now built when served.)
- [x] 1.2 The Grok voice and the phone voice get the language of the line; the closed-app loop no longer forces the
      Hindi voice for English lines. Settings text about voices made true. **APK**
- [x] 1.3 Listening language: on phones without automatic switching, the recogniser starts in Hindi once the last
      four turns were all Hindi (pinned "Mostly pure Hindi" always wins); the closed-app loop gets the same setting. **APK**
- [x] 1.4 Minimum listen 8 s → 2 s; the recogniser is primed with the staff, the contacts, the PA and the MD's names,
      in the app and in the closed-app loop. **APK**
- [x] 1.5 Answer card titled from the view's own title, not the raw transcript. **APK**

## Wave 2 — reminders, snooze, questions

- [x] 2.1 Snooze intent: any duration or time said ("1 minute baad", "after lunch", "kal subah"), stored on the
      server so it survives a restart and both the app and the closed-app watcher honour it; the buttons use the
      learned default; every confirmation says the exact time. (`snooze` intent, `ReminderSnooze`,
      `Alert.snoozed_until`, `POST /reminders/snooze/`, learned `snoozeMinutes`; app + Settings chips) **APK**
- [x] 2.2 "Remind me in N minutes" keeps seconds and never asks "tomorrow?"; the first reminder is the one JARVIS
      said, not the kind's lead. (`due_in_minutes` → exact `due_at`)
- [x] 2.3 Every question JARVIS asks carries a contract (what answer it takes, what happens) read by both the app
      and the closed-app loop: briefing "move X?", check-in "done / move / leave", nudges worded as real commands.
      (`question` on reminders and alerts, `Alert.question`, turn `event=ask` + `about`; the watcher asks and the
      closed-app loop listens for the answer; the app's `questionFor` reads the server's contract) **APK**
- [x] 2.4 Told-once keyed by the item and its due moment, not its version: an edit that does not move the time
      does not re-announce.
- [x] 2.5 Date-only task added after the day reminder time still reminds that day (an hour before office close);
      check-ins not on days off; date-only staff tasks give the PA a follow-up.
- [x] 2.6 Wording slips fixed (PA meeting reminder, task lead phrasing).
- [x] 2.7 A missed reminder (phone off at its time) still gets a notification and a held entry (the watcher shows
      it without voice and without marking it told, so the app offers it among the held ones). **APK**
- [x] 2.8 Reminder sound choice for closed-app reminders (`reminderSound`: phone's sound / silent channel). **APK**

## Wave 3 — alerts and screens

- [x] 3.1 Snoozes and urgent repeats are checked against the server's live list at fire time: a task finished
      elsewhere never rings (snooze itself now lives on the server, see 2.1). **APK**
- [x] 3.2 Updates screen (drawer → Updates): news, decisions and nudges; small news counts as seen on viewing,
      anything with a question stays until answered. **APK**
- [x] 3.3 Cancelled list (drawer → Cancelled, last 30 days) with Restore; Reopen / Restore on a closed task's sheet. **APK**
- [x] 3.4 Home "Today" = today only, with a "N late" chip that opens the late list. (Answer-card rows already open
      from the dashboard pool; a row for an item outside it is not opened: left as is.) **APK**
- [x] 3.5 Confirm before cancelling a task; declining a request asks for a note first (the PA hears it). **APK**
- [x] 3.6 "Hey JARVIS" switch is per phone (kept on the device, never synced); the voice command applies locally. **APK**
- [ ] 3.7 Six answer flags → one state machine in the conversation. NOT done: the flags (expecting / silence /
      called / question / offer / choices / previous) are documented and work; folding them into one machine is a
      refactor with real regression risk and no user-visible gain right now.
- [~] 3.8 Done: version from the app config, CSV byte-order mark, stale-version (409) message + refresh, briefing not
      spoken in a meeting or quiet hours, Hindi twins on the task sheet and request card. Not done (cosmetic, left for
      a later pass): login polish, setup "ask again later", status pill tap, drawer cleanup, long-press hint, live
      suggestions, timestamps in the conversation, larger text, Grok length. **APK**

## Wave 4 — the dynamic core

- [x] 4.1 Habits per person (snooze, meeting length, language, favourite lists), learned from use, listed at
      `GET /me/habits/` in plain words and in Settings ("How JARVIS has learned you work"), any one put back to
      the default. Leads stay settings; "trust per card kind" (saving without a yes) deliberately NOT done: the
      user wants confirmations. **APK**
- [x] 4.2 Profile block in every brain call: names, address (both scripts), `reply_language`, `habits`
      (snooze, meeting length, favourite lists), settings with the names; one builder for every server line
      (`_say` / `_ask` + Hindi twins, question contracts, `lang.py`).
- [x] 4.3 Rules as data: full-day threshold, check-in delay, urgent repeat, evening summary time, staff-silence
      days, request-wait hours, snooze default, meeting length — all settings per person (`heavyDayItems`,
      `checkinAfterMinutes`, `urgentRepeatMinutes`, `digestTime`, `staleStaffDays`, `requestWaitHours`), used by
      scheduling, reminders and follow-ups. Alert policy stays the phone's presence rules (already settings).
- [x] 4.4 Names and address by voice from either phone (`myName`, `paName` (MD only), `addressAs`): the other person
      gets one spoken note, every phone re-reads `/me/` and the dashboard.
- [x] 4.5 History template (`history` view: "when did I last meet Sharma", "what did I give Suraj last month"),
      every outcome, newest first, latest said aloud.

## Wave 5 — reliability and speed

- [x] 5.1 Reminders pushed by the server at their moment (`reminderpush.run()` every worker cycle, type "reminder"
      push with the question contract); the closed-app watcher keeps the last hour's list on the phone and fires
      from it with no connection. The half-minute poll stays as the safety net. **APK**
- [~] 5.2 The system prompts were already cached (`cache_control`); the context is already trimmed per item. When
      the AI cannot be reached and the rule parser reads the sentence, JARVIS now says so once in ten minutes
      ("my brain is slow right now, so I read this myself; please check it") and the card carries the warning.
- [x] 5.3 Diagnostics: errors, failed calls and crashes are kept on the phone (short lines, never task titles),
      sent with Settings → "Send diagnostics" or by themselves after a crash; `GET /diagnostics/` reads them back. **APK**
- [x] 5.4 TaskPanel honesty: in test mode or when not connected, the delegation confirmation says the task stays in
      JARVIS, and the card carries the warning.
- [x] 5.5 Fewer pollers: with live push the alert poll is once a minute and the dashboard every two minutes; a
      push refreshes both at once. **APK**
- [~] 5.6 Late items say how late ("Pay the vendor (3 days late)", oldest first); the evening summary names the
      oldest. "Did the meeting happen?" is the existing check-in. Recurrence NOT done (a separate design: repeat
      rules, exceptions, "skip this week").
- [ ] 5.7 Barge-in while JARVIS speaks. NOT done: the recogniser and the voice cannot hold the microphone at
      once on this phone without echo; a tap or "Hey JARVIS" interrupts as before.

## Done before this plan (10–11 Nov)

- [x] Today means today; narrowing to one item; follow-ups from the last list (b1df77b)
- [x] Notification tap opens the conversation and asks one question (b1df77b)
- [x] Fewer false wakes, quiet close, wake counter (b1df77b)
- [x] Sign-out crash fixed; "P.A." spoken as letters; names and form of address from the server (b1df77b, e5064ed)
