"""Memory and corrections, and the right moment to speak (26 Nov 2026).

- What JARVIS knows for good is in front of the brain on every turn (the memory block), grouped by kind.
- "Galat" / "nahi, wo wali" after a reply marks the turn as a correction, the rule it teaches is kept the same
  minute, and the exchange becomes a bank case replayed against the real model (manage.py bank run).
- The night's reading: the finished day's thread, read by the model for lasting facts, applied as a diff to what
  JARVIS learned itself; the speaker's own notes are never touched.
- "Nothing pending today, you can relax", once a day, at the first free stretch with nothing to fill.
- The always-on part is told when to keep quiet (a meeting, quiet hours), and offers a count first with the app
  closed: a yes reads one item with its question, a no brings them back at the usual "later" time.
The models here are scripted doubles; the tools and the services run for real."""
import json
from datetime import time, timedelta
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from core import conversation, followups, memory, services
from core import timeutils as tu
from core.management.commands.bank import check_case
from core.models import Alert, BankCase, BrainThread, Kind, Memory, Priority, PrivateTask, ReminderSnooze
from core.push import payload_for
from core.reminders import reminders_for
from core.tests import BaseCase, _cmd, _workday
from core.tests_act import _FakeModel
from core.tests_daykeeper import ALL_DAY


class CorrectionWordsTests(BaseCase):
    def test_a_correction_opens_with_its_words(self):
        for said in ("galat", "Galat hai, Sharma ji wali", "nahi, wo wali", "nahi ye nahi", "aise nahi", "mera matlab GST wali",
                     "no, not that one", "wrong, I meant the Pune one", "गलत", "नहीं, वो वाली", "ऐसे नहीं"):
            self.assertTrue(memory.is_correction(said), said)

    def test_an_ordinary_sentence_is_not_a_correction(self):
        for said in ("Sharma ji ko kal 4 baje rakh do", "nahi rehne do", "meeting cancel karo, nahi aana hai usko",
                     "kal kya hai", "haan", "Nikki ko bolo file bheje"):
            self.assertFalse(memory.is_correction(said), said)


class MemoryBlockTests(BaseCase):
    def test_the_block_groups_by_kind_and_is_empty_when_nothing_is_known(self):
        self.assertEqual(memory.block(self.md), "")
        services.add_memory(self.md, "City is Indore.")
        memory.note(self.md, "Mehta ji is the Pune distributor", kind=Memory.Kind.PERSON)
        memory.note(self.md, "'Sharma ji wali' means the GST meeting", kind=Memory.Kind.CORRECTION)
        block = memory.block(self.md, "Sir")
        self.assertTrue(block.startswith("<memory>") and block.endswith("</memory>"))
        self.assertIn("Notes Sir asked to keep:\n- City is Indore.", block)
        self.assertIn("People:\n- Mehta ji is the Pune distributor", block)
        self.assertIn("Corrections (mistakes made once; never again):\n- 'Sharma ji wali' means the GST meeting", block)
        self.assertEqual(memory.block(self.pa), "")  # owner-only: nothing of the MD's reaches the PA

    def test_a_learned_fact_is_kept_once_and_the_shelf_has_a_limit(self):
        a = memory.note(self.md, "Meetings are 30 minutes unless said")
        b = memory.note(self.md, "meetings are 30 minutes   unless said")
        self.assertEqual(a.id, b.id)
        self.assertEqual(Memory.objects.filter(owner=self.md).count(), 1)
        with mock.patch.object(memory, "LEARNED_LIMIT", 3):
            memory.note(self.md, "Calls the bank on Mondays", kind=Memory.Kind.HABIT)
            memory.note(self.md, "Nikki handles GST", kind=Memory.Kind.PERSON)
            memory.note(self.md, "Prefers the time, never the date, for today", kind=Memory.Kind.PREFERENCE)
        texts = set(Memory.objects.filter(owner=self.md).values_list("text", flat=True))
        self.assertEqual(len(texts), 3)
        self.assertNotIn("Meetings are 30 minutes unless said", texts)  # the oldest preference made room

    def test_the_list_shows_notes_first_then_what_was_learned_and_the_limit_counts_notes_only(self):
        memory.note(self.md, "Mehta ji is the Pune distributor", kind=Memory.Kind.PERSON)
        services.add_memory(self.md, "City is Indore.")
        rows = services.list_memories(self.md)
        self.assertEqual([(r["source"], r["kind"]) for r in rows], [("said", "note"), ("learned", "person")])
        with mock.patch.object(services, "MEMORY_LIMIT", 1):
            with self.assertRaises(services.ServiceError):
                services.add_memory(self.md, "Another note")
            self.assertIsNotNone(memory.note(self.md, "Still learns"))  # the learned shelf is its own


class NightReadingTests(BaseCase):
    def thread(self, day, said="Sharma ji wali meeting kal 4 baje kar do", reply="Done: Meeting with Sharma ji, kal 4 PM."):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": f'<turn now="x">\n<open_items>\nnothing open\n</open_items>\n<said>{said}</said>\n</turn>'}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "move_item", "input": {"id": "1", "date": "2026-11-27", "time": "16:00"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": '{"done": true, "title": "Meeting with Sharma ji"}'}]},
            {"role": "assistant", "content": [{"type": "text", "text": json.dumps({"say_en": reply, "say_hi": "", "about": ["1"]})}]},
        ]
        return BrainThread.objects.create(user=self.md, day=day, messages_json=json.dumps(messages), turns=1)

    def test_the_transcript_is_the_day_in_plain_words(self):
        row = self.thread(tu.today_local() - timedelta(days=1))
        text = memory.transcript(json.loads(row.messages_json))
        self.assertIn("Speaker: Sharma ji wali meeting kal 4 baje kar do", text)
        self.assertIn("JARVIS move_item:", text)
        self.assertIn("-> {\"done\": true", text)
        self.assertIn("JARVIS: Done: Meeting with Sharma ji, kal 4 PM.", text)

    def test_the_diff_is_applied_to_what_was_learned_and_never_to_his_own_notes(self):
        said = services.add_memory(self.md, "City is Indore.")
        old = memory.note(self.md, "Meetings are 45 minutes", kind=Memory.Kind.PREFERENCE)
        gone = memory.note(self.md, "Mehta ji is in Mumbai", kind=Memory.Kind.PERSON)
        row = self.thread(tu.today_local() - timedelta(days=1))
        handles = {}

        def fake_model(user, prompt, body):
            for line in body.splitlines():
                if line.startswith("[m"):
                    handles[line.split("]")[0][1:]] = line
            h_said = next(h for h, l in handles.items() if "City is Indore" in l)
            h_old = next(h for h, l in handles.items() if "45 minutes" in l)
            h_gone = next(h for h, l in handles.items() if "Mumbai" in l)
            self.assertIn("<day date=", body)
            self.assertIn("Speaker: Sharma ji wali", body)
            return {"add": [{"kind": "habit", "text": "Sharma ji's meeting is on Fridays at 4 PM"}, {"kind": "nonsense", "text": "Likes short replies"}],
                    "update": [{"handle": h_old, "text": "Meetings are 30 minutes"}, {"handle": h_said, "text": "City is Pune."}],
                    "remove": [h_gone, h_said]}

        with mock.patch("core.memory._ask_model", side_effect=fake_model):
            out = memory.learn_day(row, address="Sir")
        self.assertEqual(out, {"added": 2, "updated": 1, "removed": 1})
        self.assertTrue(BrainThread.objects.get(id=row.id).learned)
        rows = {m.text: m for m in Memory.objects.filter(owner=self.md)}
        self.assertIn("City is Indore.", rows)                   # his own note: not updated, not removed
        self.assertEqual(rows["City is Indore."].id, Memory.objects.get(id=said["id"]).id)
        self.assertNotIn("Mehta ji is in Mumbai", rows)          # removed
        self.assertFalse(Memory.objects.filter(id=gone.id).exists())
        self.assertEqual(Memory.objects.get(id=old.id).text, "Meetings are 30 minutes")
        self.assertEqual(rows["Sharma ji's meeting is on Fridays at 4 PM"].kind, "habit")
        self.assertEqual(rows["Likes short replies"].kind, "preference")  # an unknown kind lands on the preference shelf
        self.assertTrue(all(m.source == "learned" for t, m in rows.items() if t != "City is Indore."))

    def test_a_day_the_model_could_not_read_stays_unread_and_an_empty_day_is_marked(self):
        row = self.thread(tu.today_local() - timedelta(days=1))
        with mock.patch("core.memory._ask_model", return_value=None):
            memory.learn_day(row)
        self.assertFalse(BrainThread.objects.get(id=row.id).learned)
        empty = BrainThread.objects.create(user=self.md, day=tu.today_local() - timedelta(days=2), messages_json="[]", turns=0)
        memory.learn_day(empty)
        self.assertTrue(BrainThread.objects.get(id=empty.id).learned)

    @override_settings(JARVIS_AI_ENABLED=True, ANTHROPIC_API_KEY="k")
    def test_the_night_reads_every_finished_day_not_yet_read_and_never_today(self):
        self.thread(tu.today_local() - timedelta(days=2))
        self.thread(tu.today_local() - timedelta(days=1))
        self.thread(tu.today_local())
        with mock.patch("core.memory._ask_model", return_value={"add": [{"kind": "person", "text": "Nikki handles GST"}]}) as m:
            self.assertEqual(memory.learn_pending(), 2)
            self.assertEqual(memory.learn_pending(), 0)  # read once
        self.assertEqual(m.call_count, 2)
        self.assertFalse(BrainThread.objects.get(user=self.md, day=tu.today_local()).learned)
        texts = [m.text for m in Memory.objects.filter(owner=self.md)]  # (encrypted: never filtered by text in the database)
        self.assertEqual(texts.count("Nikki handles GST"), 1)  # the same fact twice is one


@override_settings(JARVIS_AI_ACT_ENABLED=True, JARVIS_AI_ACT_MODEL="claude-sonnet-5-5", JARVIS_AI_ACT_STRONG_MODEL="claude-opus-5-5")
class CorrectionTurnTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.day = _workday(1)
        self.sharma = services.create_private_task(self.md, {"title": "Meeting with Sharma ji", "kind": Kind.MEETING,
                                                             "due_date": self.day, "due_time": time(10, 30), "duration_minutes": 30})
        self.gst = services.create_private_task(self.md, {"title": "GST review with Sharma ji", "kind": Kind.MEETING,
                                                          "due_date": self.day, "due_time": time(15, 0), "duration_minutes": 30})

    def say(self, text, model):
        with mock.patch("core.ai_brain.ai_available", return_value=True), mock.patch("core.act._client", return_value=model), \
                mock.patch("core.ai_brain.interpret", mock.Mock(side_effect=AssertionError("the fixed engine must not read this"))):
            r = self.md_client.post("/api/v1/assistant/interpret/", {"text": text}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_the_memory_block_is_in_front_of_the_brain_and_the_correction_is_kept_and_banked(self):
        memory.note(self.md, "Mehta ji is the Pune distributor", kind=Memory.Kind.PERSON)
        sid, gid = str(self.sharma.id), str(self.gst.id)
        first = _FakeModel([("move_item", {"id": sid, "date": self.day.isoformat(), "time": "16:00"})],
                           lambda r: {"say_en": "Done: Meeting with Sharma ji, 4 PM.", "say_hi": "", "about": [sid], "learned": ""})
        self.say("Sharma ji wali ko 4 baje kar do", first)
        system = first.requests[0]["system"]
        self.assertEqual(len(system), 2)
        self.assertIn("Mehta ji is the Pune distributor", system[1]["text"])
        self.assertEqual(system[1]["cache_control"], {"type": "ephemeral"})
        self.assertIn('"learned": ""', system[0]["text"])
        self.assertNotIn("<correction>", first.requests[0]["messages"][0]["content"][0]["text"])
        # "Galat, GST wali": the turn is marked, the right one is moved back and forth, and the rule is kept.
        second = _FakeModel([("move_item", {"id": sid, "date": self.day.isoformat(), "time": "10:30"}),
                             ("move_item", {"id": gid, "date": self.day.isoformat(), "time": "16:00"})],
                            lambda r: {"say_en": "GST review with Sharma ji, 4 PM; the 10:30 one stays.", "say_hi": "", "about": [gid],
                                       "learned": "'Sharma ji wali' means the GST review with Sharma ji, not the plain meeting"})
        d = self.say("galat, GST wali", second)
        turn = second.requests[0]["messages"][-1]["content"][0]["text"]
        self.assertIn("<correction>", turn)
        self.assertIn("<said>galat, GST wali</said>", turn)
        self.assertTrue(d["booked"])
        self.gst.refresh_from_db()
        self.assertEqual(tu.to_local(self.gst.due_at).strftime("%H:%M"), "16:00")
        kept = Memory.objects.get(owner=self.md, kind=Memory.Kind.CORRECTION)
        self.assertEqual((kept.text, kept.source), ("'Sharma ji wali' means the GST review with Sharma ji, not the plain meeting", "learned"))
        case = BankCase.objects.get(user=self.md)
        self.assertEqual(case.said, "galat, GST wali")
        self.assertEqual(case.day, tu.today_local())
        before = json.loads(case.before_json)
        self.assertTrue(before[0]["content"][0]["text"].startswith("<turn "))
        self.assertIn("Sharma ji wali ko 4 baje kar do", before[0]["content"][0]["text"])
        self.assertEqual(json.loads(case.turn_json)["role"], "user")
        expected = json.loads(case.expected_json)
        self.assertEqual([t["name"] for t in expected["tools"]], ["move_item", "move_item"])
        self.assertEqual(expected["tools"][1]["input"]["id"], gid)
        self.assertEqual(expected["say_en"], "GST review with Sharma ji, 4 PM; the 10:30 one stays.")
        self.assertEqual(case.learned, kept.text)
        # The next turn sees the correction in its memory block.
        third = _FakeModel({"say_en": "Ji.", "say_hi": "", "about": []})
        self.say("theek hai", third)
        self.assertIn("Corrections (mistakes made once; never again):", third.requests[0]["system"][1]["text"])

    def test_a_stated_preference_is_kept_as_one_without_a_correction(self):
        model = _FakeModel({"say_en": "Noted.", "say_hi": "", "about": [], "learned": "Meetings are 30 minutes unless said otherwise"})
        self.say("meri meetings aam taur par 30 minute ki hoti hain", model)
        kept = Memory.objects.get(owner=self.md)
        self.assertEqual((kept.kind, kept.source), ("preference", "learned"))
        self.assertFalse(BankCase.objects.exists())

    def test_the_first_turn_of_the_day_is_never_a_correction(self):
        model = _FakeModel({"say_en": "Phir se boliye.", "say_hi": "", "about": []})
        self.say("galat", model)
        self.assertNotIn("<correction>", model.requests[0]["messages"][0]["content"][0]["text"])
        self.assertFalse(BankCase.objects.exists())


class BankReplayTests(BaseCase):
    def case(self, expected_tools):
        before = [{"role": "user", "content": [{"type": "text", "text": '<turn now="x">\n<open_items>\n[1] Meeting with Sharma ji\n</open_items>\n<said>Sharma ji wali 4 baje</said>\n</turn>'}]},
                  {"role": "assistant", "content": [{"type": "text", "text": json.dumps({"say_en": "Done.", "say_hi": "", "about": ["1"]})}]}]
        turn = {"role": "user", "content": [{"type": "text", "text": '<turn now="x">\n<correction>…</correction>\n<said>galat, GST wali</said>\n</turn>'}]}
        return BankCase.objects.create(user=self.md, day=tu.today_local(), said="galat, GST wali", before_json=json.dumps(before),
                                       turn_json=json.dumps(turn), expected_json=json.dumps({"tools": expected_tools, "say_en": "Fixed."}))

    def test_the_replay_passes_when_the_model_reaches_for_the_same_tools(self):
        case = self.case([{"name": "move_item", "input": {"id": "2", "time": "16:00"}}, {"name": "move_item", "input": {"id": "1", "time": "10:30"}}])
        same = _FakeModel([("move_item", {"id": "1", "date": "2026-11-27", "time": "10:30", "force": False}), ("move_item", {"id": "2", "time": "16:00"})])
        out = check_case(case, "claude-opus-5-5", client=same)
        self.assertTrue(out["ok"], out)
        sent = same.requests[0]
        self.assertEqual(len(sent["messages"]), 3)
        self.assertIn("galat, GST wali", sent["messages"][-1]["content"][0]["text"])
        self.assertEqual(len(sent["system"]), 1)  # the prompt alone: no memory, so the words themselves are tested
        other = _FakeModel([("cancel_items", {"ids": ["1"]})])
        self.assertFalse(check_case(case, "claude-opus-5-5", client=other)["ok"])
        wrong_id = _FakeModel([("move_item", {"id": "1", "time": "16:00"}), ("move_item", {"id": "2", "time": "10:30"})])
        self.assertFalse(check_case(case, "claude-opus-5-5", client=wrong_id)["ok"])

    def test_a_correction_that_needed_no_tool_passes_only_with_a_reply_and_nothing_touched(self):
        case = self.case([])
        self.assertTrue(check_case(case, "claude-opus-5-5", client=_FakeModel({"say_en": "Ji, GST wali hi hai.", "say_hi": "", "about": []}))["ok"])
        self.assertFalse(check_case(case, "claude-opus-5-5", client=_FakeModel([("move_item", {"id": "1"})]))["ok"])


class RelaxLineTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.private.delete()
        today = tu.today_local()
        self.now = tu.combine_local(today, time(11, 0))
        services.create_private_task(self.md, {"title": "Meeting with architect", "kind": Kind.MEETING,
                                               "due_date": today, "due_time": time(14, 0), "duration_minutes": 60})

    def keeper(self, now=None):
        with mock.patch("core.followups.get_prefs", return_value=dict(ALL_DAY)):
            return followups.free_time(now=now or self.now)

    def relax(self):
        return Alert.objects.filter(recipient=self.md, ref_type="followup", ref_id__startswith="free-none:")

    def test_nothing_pending_is_said_once_a_day_at_the_first_free_stretch(self):
        self.assertGreaterEqual(self.keeper(), 1)  # (everyone on the team with a free stretch and nothing pending hears it)
        line = self.relax().get()
        self.assertEqual(line.spoken_text, "Sir, nothing is pending today. You can relax.")
        self.assertEqual(line.spoken_text_hi, "सर, आज कुछ पेंडिंग नहीं है, आप रिलैक्स कर सकते हैं।")
        self.assertEqual(line.priority, Priority.LOW)
        self.assertIsNone(line.question)
        self.keeper()
        self.keeper(self.now.replace(hour=15, minute=5))  # the stretch after the meeting: not again
        self.assertEqual(self.relax().count(), 1)

    def test_with_late_or_pending_work_the_plan_is_offered_instead(self):
        services.create_private_task(self.md, {"title": "Renew the trade licence", "due_date": tu.today_local() - timedelta(days=2)})
        self.keeper()
        self.assertFalse(self.relax().exists())
        self.assertTrue(Alert.objects.filter(recipient=self.md, ref_type="followup", ref_id__startswith="free:").exists())

    def test_work_that_merely_does_not_fit_is_not_nothing_pending(self):
        # A free stretch of 20 minutes, a pending item of two hours: nothing fits, but it is not "nothing pending".
        PrivateTask.objects.filter(owner=self.md).delete()
        today = tu.today_local()
        services.create_private_task(self.md, {"title": "Meeting", "kind": Kind.MEETING, "due_date": today, "due_time": time(11, 25), "duration_minutes": 480})
        services.create_private_task(self.md, {"title": "Write the annual report", "due_date": today - timedelta(days=1), "duration_minutes": 120})
        self.keeper()
        self.assertFalse(self.relax().exists())
        self.assertFalse(Alert.objects.filter(recipient=self.md, ref_type="followup", ref_id__startswith="free:").exists())


class SilenceNowTests(BaseCase):
    def test_a_meeting_or_quiet_hours_means_silent(self):
        day_prefs = {"quietHoursEnabled": False, "meetingMode": False}
        self.assertEqual(services.silence_now(self.md, day_prefs, tu.combine_local(tu.today_local(), time(12, 0))), {"silent": False, "why": ""})
        self.assertEqual(services.silence_now(self.md, {**day_prefs, "meetingMode": True})["why"], "meeting")
        start = self.private.scheduled_start  # the fixture's meeting, in an hour
        self.assertEqual(services.silence_now(self.md, day_prefs, start + timedelta(minutes=5))["why"], "meeting")
        override = {**day_prefs, "meetingOverrideUntil": (start + timedelta(hours=2)).isoformat()}
        self.assertFalse(services.silence_now(self.md, override, start + timedelta(minutes=5))["silent"])
        night = {"quietHoursEnabled": True, "quietStartHour": 22, "quietEndHour": 7, "meetingMode": False}
        self.assertEqual(services.silence_now(self.md, night, tu.combine_local(tu.today_local(), time(23, 30)))["why"], "quiet_hours")
        self.assertFalse(services.silence_now(self.md, night, tu.combine_local(tu.today_local(), time(12, 0)))["silent"])

    def test_the_reminder_list_and_the_push_carry_it(self):
        out = reminders_for(self.md, hours=1)
        self.assertIn("silence", out)
        self.assertEqual(set(out["silence"]), {"silent", "why"})
        services.set_prefs(self.md, {"meetingMode": True})
        self.assertEqual(reminders_for(self.md, hours=1)["silence"], {"silent": True, "why": "meeting"})
        alert = Alert.objects.create(recipient=self.md, kind=Alert.Kind.INFO, title="A note", body="b", spoken_text="Sir, a note.")
        self.assertEqual(payload_for(alert)["silent"], "1")
        services.set_prefs(self.md, {"meetingMode": False, "quietHoursEnabled": False})
        self.assertEqual(payload_for(alert)["silent"], "")


class ClosedAppOfferTests(BaseCase):
    def offer(self, items):
        about = {"kind": "offer", "question": "Sir, 2 reminders. Shall I tell you?", "items": items}
        return conversation.turn(self.md, "", event="ask", about=about)

    def test_yes_reads_the_first_with_its_question_and_counts_the_rest(self):
        when = tu.now_local() + timedelta(minutes=20)
        task = services.create_private_task(self.md, {"title": "Call the bank", "kind": Kind.CALL, "due_date": when.date(),
                                                      "due_time": when.time().replace(second=0, microsecond=0)})
        due = next(r for r in reminders_for(self.md, hours=1)["reminders"] if r["title"] == "Call the bank" and r["label"] == "due")
        items = [{"kind": "reminder", "key": due["key"], "title": "Call the bank", "spoken": due["spoken"], "question": due["question"]},
                 {"kind": "reminder", "key": "k2", "title": "Pay the vendor", "spoken": "Sir, pay the vendor.", "question": None}]
        r = self.offer(items)
        self.assertEqual((r["action"], r["listen"]), ("asking", True))
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="confirm_pending")):
            r = conversation.turn(self.md, "haan")
        self.assertEqual(r["action"], "asking")
        self.assertTrue(r["show"].startswith(due["spoken"]), r)
        self.assertIn(due["question"]["en"], r["show"])
        self.assertNotIn("Pay the vendor", r["show"])  # one at a time
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="confirm_pending")):
            r = conversation.turn(self.md, "ho gaya")
        self.assertIn("Marked as done", r["show"])
        self.assertEqual(PrivateTask.objects.get(id=task.id).status, "COMPLETED")
        self.assertTrue(r["show"].endswith("Sir, one more. Shall I tell you?"), r)  # the next is counted, not read
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="confirm_pending")):
            r = conversation.turn(self.md, "haan")
        self.assertEqual(r["show"], "Sir, pay the vendor.")
        self.assertEqual(r["action"], "read_missed")

    def test_a_no_to_one_read_item_keeps_the_rest_and_a_first_without_a_question_counts_the_next(self):
        when = tu.now_local() + timedelta(minutes=20)
        services.create_private_task(self.md, {"title": "Call the bank", "kind": Kind.CALL, "due_date": when.date(),
                                               "due_time": when.time().replace(second=0, microsecond=0)})
        due = next(r for r in reminders_for(self.md, hours=1)["reminders"] if r["title"] == "Call the bank" and r["label"] == "due")
        items = [{"kind": "reminder", "key": "k0", "title": "A note", "spoken": "Sir, Nikki sent a note.", "question": None},
                 {"kind": "reminder", "key": due["key"], "title": "Call the bank", "spoken": due["spoken"], "question": due["question"]},
                 {"kind": "reminder", "key": "k2", "title": "Pay the vendor", "spoken": "Sir, pay the vendor.", "question": None}]
        self.offer(items)
        # The first has no question of its own: it is read and the next is counted in the same breath.
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="confirm_pending")):
            r = conversation.turn(self.md, "haan")
        self.assertEqual(r["show"], "Sir, Nikki sent a note. Sir, 2 more. Shall I tell you?")
        self.assertEqual(r["action"], "asking")
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="confirm_pending")):
            r = conversation.turn(self.md, "haan")
        self.assertTrue(r["show"].startswith(due["spoken"]))
        # "Nahi" to the bank call's own question leaves that one, and the last is still counted, not lost.
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="cancel_pending")):
            r = conversation.turn(self.md, "nahi")
        self.assertTrue(r["show"].startswith("Okay, leaving it."), r)
        self.assertTrue(r["show"].endswith("Sir, one more. Shall I tell you?"), r)
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="confirm_pending")):
            r = conversation.turn(self.md, "haan")
        self.assertEqual(r["show"], "Sir, pay the vendor.")

    def test_no_brings_them_back_at_the_usual_later_time(self):
        alert = Alert.objects.create(recipient=self.md, kind=Alert.Kind.INFO, title="A note", body="b", spoken_text="Sir, a note.")
        items = [{"kind": "reminder", "key": "task:1:due", "title": "Call the bank", "spoken": "Sir, call the bank.", "question": None},
                 {"kind": "alert", "id": str(alert.id), "title": "A note", "spoken": "Sir, a note.", "question": None}]
        self.offer(items)
        services.set_prefs(self.md, {"snoozeMinutes": 10})
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="cancel_pending")):
            r = conversation.turn(self.md, "nahi")
        self.assertEqual((r["show"], r["action"]), ("Okay, I'll bring it up again in 10 minutes.", "snoozed"))
        snooze = ReminderSnooze.objects.get(user=self.md, key="task:1:due")
        self.assertAlmostEqual((snooze.until - timezone.now()).total_seconds(), 600, delta=30)
        alert.refresh_from_db()
        self.assertIsNotNone(alert.snoozed_until)
        self.assertIsNone(alert.acknowledged_at)
