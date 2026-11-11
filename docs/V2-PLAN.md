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

- [ ] 2.1 Snooze intent: any duration or time said ("1 minute baad", "after lunch", "kal subah"), stored on the
      server so it survives a restart and both the app and the closed-app watcher honour it; the buttons use the
      learned default; every confirmation says the exact time.
- [ ] 2.2 "Remind me in N minutes" keeps seconds and never asks "tomorrow?"; the first reminder is the one JARVIS
      said, not the kind's lead.
- [ ] 2.3 Every question JARVIS asks carries a contract (what answer it takes, what happens) read by both the app
      and the closed-app loop: briefing "move X?", check-in "done / move / leave", nudges worded as real commands.
- [ ] 2.4 Told-once keyed by the item and its due moment, not its version: an edit that does not move the time
      does not re-announce.
- [ ] 2.5 Date-only task added after the day reminder time still reminds that day; check-ins not on days off;
      date-only staff tasks give the PA a follow-up.
- [ ] 2.6 Wording slips fixed (PA meeting reminder, task lead phrasing).
- [ ] 2.7 A missed reminder (phone off at its time) still gets a notification and a held entry.
- [ ] 2.8 Reminder sound choice for closed-app reminders. **APK**

## Wave 3 — alerts and screens

- [ ] 3.1 One alert state per item on the phone (queue / held / snoozed / repeats merged), checked against the
      server at fire time, so a task finished elsewhere never rings. **APK**
- [ ] 3.2 Updates screen for low-priority news; acknowledged on viewing. **APK**
- [ ] 3.3 Cancelled and completed lists with restore / reopen. **APK**
- [ ] 3.4 Home "Today" = today only, with a late chip; answer-card rows always open (fetched by id). **APK**
- [ ] 3.5 Confirm before delete / decline; decline with a note. **APK**
- [ ] 3.6 "Hey JARVIS" switch per device, not per account. **APK**
- [ ] 3.7 Six answer flags → one state machine in the conversation. **APK**
- [ ] 3.8 Small details: version from the app config, login polish, setup "ask again later", status pill tap,
      drawer cleanup, long-press hint, live suggestions, timestamps in the conversation, larger text, CSV BOM,
      stale-version retry, voice-friendly validation errors, briefing not spoken in a meeting, Grok length. **APK**

## Wave 4 — the dynamic core

- [ ] 4.1 Habits per person (snooze, leads, meeting length, language, trust per card kind, favourite lists),
      learned from use, shown in Settings, deletable.
- [ ] 4.2 Profile block in every brain call (names, address, language, style, habits); one reply builder for every
      spoken line (facts + profile + question contract → line, Hindi twin, card, what to listen for).
- [ ] 4.3 Rules as data: alert policy, leads, snooze default, trust with undo, quiet / office hours, heavy day.
- [ ] 4.4 Names and address by voice from either phone (MD sets both, PA sets self), profile-changed push, one
      spoken note to the other person.
- [ ] 4.5 History template: "when did I last meet Sharma", "what did I give Suraj last month", outcomes included.

## Wave 5 — reliability and speed

- [ ] 5.1 Reminders pushed by the server at the exact second; the watcher keeps the next hour's list offline.
- [ ] 5.2 Context trimming and caching of the stable part; "the AI is slow" said once instead of the rule parser
      guessing.
- [ ] 5.3 Diagnostics: errors and crashes logged to the server, "send diagnostics" in Settings. **APK**
- [ ] 5.4 TaskPanel honesty: JARVIS knows when the staff door is closed and says so.
- [ ] 5.5 Fewer pollers: push-driven refresh, slower when idle. **APK**
- [ ] 5.6 Recurrence; "did the meeting happen?"; escalation wording for days overdue.
- [ ] 5.7 Barge-in while JARVIS speaks (if the phone allows it). **APK**

## Done before this plan (10–11 Nov)

- [x] Today means today; narrowing to one item; follow-ups from the last list (b1df77b)
- [x] Notification tap opens the conversation and asks one question (b1df77b)
- [x] Fewer false wakes, quiet close, wake counter (b1df77b)
- [x] Sign-out crash fixed; "P.A." spoken as letters; names and form of address from the server (b1df77b, e5064ed)
