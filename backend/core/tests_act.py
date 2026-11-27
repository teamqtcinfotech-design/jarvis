"""The act path (25 Nov 2026): the sentence is read by the model with the day's thread in front of it and done through
the tools around the services; the fixed engine reads it only when the model cannot be reached or a card is open on
the phone. The model here is a scripted double; the tools run for real."""
import json
import os
import unittest
from datetime import time
from unittest import mock

import anthropic
import httpx
from django.test import override_settings

from core import act, services
from core import timeutils as tu
from core.models import BrainThread, Kind, Priority, SharedTask
from core.tests import BaseCase, _cmd, _workday


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self, mode="json", exclude_none=True):  # noqa: ARG002 — the SDK's signature
        return dict(self.__dict__)


class _Response:
    def __init__(self, content, stop_reason):
        self.content, self.stop_reason, self.usage = content, stop_reason, None


class _FakeModel:
    """A scripted model. Each step is a list of tool calls [(name, input), ...] or the final answer: a dict, or a
    callable given the results of the last tool round (keyed by tool id "t<round>_<n>")."""

    def __init__(self, *steps):
        self.steps, self.requests, self.round = list(steps), [], 0
        self.messages = self

    def create(self, **kw):
        self.requests.append(kw)
        step = self.steps.pop(0) if self.steps else {"say_en": "Ji.", "say_hi": "", "about": []}
        if callable(step):
            last = kw["messages"][-1]["content"]
            results = {r["tool_use_id"]: json.loads(r["content"]) for r in (last if isinstance(last, list) else [])
                       if isinstance(r, dict) and r.get("type") == "tool_result"}
            step = step(results)
        if isinstance(step, list):
            self.round += 1
            blocks = [_Block(type="tool_use", id=f"t{self.round}_{i}", name=name, input=args) for i, (name, args) in enumerate(step)]
            return _Response(blocks, "tool_use")
        return _Response([_Block(type="text", text=json.dumps(step, ensure_ascii=False))], "end_turn")


class _Down:
    def __init__(self):
        self.messages = self

    def create(self, **kw):
        raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))


@override_settings(JARVIS_AI_ACT_ENABLED=True, JARVIS_AI_ACT_MODEL="claude-sonnet-5-5", JARVIS_AI_ACT_STRONG_MODEL="claude-opus-5-5")
class ActPathTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.nikki = services.team_add(self.md, {"name": "Nikki", "title": "Accountant", "username": "nikki", "password": "nikki1"})
        self.day = _workday(1)
        self.sharma = services.create_private_task(self.md, {"title": "Meeting with Sharma ji", "kind": Kind.MEETING, "priority": Priority.NORMAL,
                                                             "due_date": self.day, "due_time": time(10, 30), "duration_minutes": 30})
        self.mehta = services.create_private_task(self.md, {"title": "Meeting with Mehta ji", "kind": Kind.MEETING, "priority": Priority.NORMAL,
                                                            "due_date": self.day, "due_time": time(16, 0), "duration_minutes": 30})

    def say(self, text, model, brain=None):
        engine = brain if brain is not None else mock.Mock(side_effect=AssertionError("the fixed engine must not read this"))
        with mock.patch("core.ai_brain.ai_available", return_value=True), mock.patch("core.act._client", return_value=model), \
                mock.patch("core.ai_brain.interpret", engine):
            r = self.md_client.post("/api/v1/assistant/interpret/", {"text": text}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_a_rename_is_done_at_once_and_the_thread_carries_it_to_the_next_turn(self):
        sid = str(self.sharma.id)
        model = _FakeModel([("edit_item", {"id": sid, "title": "Test GST review"})],
                           lambda r: {"say_en": "Done: Test GST review.", "say_hi": "हो गया: Test GST review।", "about": [sid]})
        d = self.say("Sharma ji wali meeting ka naam Test GST review rakh do", model)
        self.sharma.refresh_from_db()
        self.assertEqual(self.sharma.title, "Test GST review")
        self.assertEqual((d["spoken_reply"], d["booked"], d["undo"], d["engine"], d["model"]),
                         ("Done: Test GST review.", True, True, "act", "claude-sonnet-5-5"))
        first = model.requests[0]
        turn = first["messages"][0]["content"][0]["text"]
        self.assertIn(sid, turn)                                         # the open items carry their ids
        self.assertIn("<said>Sharma ji wali meeting ka naam", turn)
        self.assertTrue({"diary", "edit_item", "move_item", "give_work", "undo", "old_engine"} <= {t["name"] for t in first["tools"]})
        self.assertEqual(first["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(BrainThread.objects.get(user=self.md).turns, 1)
        self.assertEqual(services.last_view(self.md)["items"][0]["id"], sid)  # the cursor: "isko" next means it
        # "undo": the earlier turn rides along in the thread, and the old name comes back.
        model2 = _FakeModel([("undo", {})], lambda r: {"say_en": r["t1_0"]["said"], "say_hi": "", "about": []})
        d = self.say("undo", model2)
        self.sharma.refresh_from_db()
        self.assertEqual(self.sharma.title, "Meeting with Sharma ji")
        self.assertTrue(d["spoken_reply"])
        sent = model2.requests[0]["messages"]
        self.assertGreater(len(sent), 2)
        self.assertIn("Test GST review", json.dumps(sent[:-1], ensure_ascii=False))
        self.assertEqual(BrainThread.objects.get(user=self.md).turns, 2)

    def test_a_move_onto_another_meeting_asks_once_and_the_yes_moves_it(self):
        sid = str(self.sharma.id)

        def question(results):
            fact = results["t1_0"]
            self.assertEqual(fact["clash"][0]["title"], "Meeting with Mehta ji")
            self.assertEqual(fact["suggested"]["time"], "16:30")
            return {"say_en": "Aapki 4 baje Mehta ji ke saath meeting hai. Sharma ji ko 4:30 rakh dun?", "say_hi": "", "about": [sid]}

        d = self.say("Sharma ji wali ko 4 baje kar do",
                     _FakeModel([("move_item", {"id": sid, "date": self.day.isoformat(), "time": "16:00"})], question))
        self.sharma.refresh_from_db()
        self.assertEqual(tu.to_local(self.sharma.due_at).hour, 10)        # nothing moved before the answer
        self.assertFalse(d["booked"])
        self.assertIn("4:30 rakh dun", d["spoken_reply"])
        d = self.say("haan", _FakeModel([("move_item", {"id": sid, "date": self.day.isoformat(), "time": "16:30", "force": True})],
                                        lambda r: {"say_en": "Done: Meeting with Sharma ji, 4:30 PM.", "say_hi": "", "about": [sid]}))
        self.sharma.refresh_from_db()
        self.assertEqual(tu.to_local(self.sharma.due_at).strftime("%H:%M"), "16:30")
        self.assertTrue(d["booked"] and d["undo"])

    def test_a_question_is_answered_from_the_lookups_and_sets_the_cursor(self):
        def final(results):
            items = results["t1_0"]["items"]
            return {"say_en": f"{items[0]['title']} ke saath, kal 10:30 baje.", "say_hi": "", "about": [items[0]["id"]]}

        d = self.say("meri kal ki pehli meeting kaunsi hai", _FakeModel([("next_up", {"count": 1, "kinds": ["MEETING"]})], final))
        self.assertFalse(d["booked"])
        self.assertTrue(d["spoken_reply"].endswith("kal 10:30 baje."))
        self.assertEqual(services.last_view(self.md)["cursor"], 0)

    def test_work_for_one_person_goes_at_once_and_a_list_for_two_becomes_the_card(self):
        def relay(results):
            fact = results["t1_0"]
            self.assertTrue(fact.get("done"), fact)
            return {"say_en": fact["said"], "say_hi": fact.get("said_hi") or "", "about": []}

        d = self.say("Nikki ko bolo GST file bhej de", _FakeModel([("give_work", {"items": [{"to": "Nikki", "title": "Send the GST file"}]})], relay))
        self.assertEqual(SharedTask.objects.get(title="Send the GST file").assignee_user, self.nikki)
        self.assertIn("Nikki has it", d["spoken_reply"])
        self.assertTrue(d["booked"])
        # Two things for two people: the list card the speaker checks; nothing is sent until "sab bhej do".
        d = self.say("Nikki ko bolo ledger check kare aur Priya ko bolo courier bhej de",
                     _FakeModel([("give_work", {"items": [{"to": "Nikki", "title": "Check the ledger"}, {"to": "Priya", "title": "Send the courier"}]})]))
        self.assertEqual(len(d.get("batch") or []), 1, d.get("spoken_reply"))
        self.assertFalse(d.get("booked"))
        self.assertFalse(SharedTask.objects.filter(title="Check the ledger").exists())
        self.assertEqual(d["engine"], "act")

    def test_the_same_work_for_several_people_is_sent_at_once_and_the_brain_says_the_line(self):
        # His rule (27 Nov 2026): the same work to several named people goes at once, one line naming them.
        def line(results):
            fact = results["t1_0"]
            self.assertTrue(fact["done"], fact)
            self.assertEqual(fact["people"], ["Nikki", "Priya"])
            self.assertEqual([s["what"] for s in fact["sent"]], ["Complete all your pending tasks by tomorrow"] * 2)
            return {"say_en": "Nikki aur Priya ko bhej diya.", "say_hi": "Nikki और Priya को भेज दिया।", "about": []}

        same = [{"to": "Nikki", "title": "Complete all your pending tasks by tomorrow"}, {"to": "Priya", "title": "Complete all your pending tasks by tomorrow"}]
        d = self.say("in dono ko bolo kal tak apne saare pending task complete karein", _FakeModel([("give_work", {"items": same, "send_now": True})], line))
        self.assertEqual(SharedTask.objects.filter(title="Complete all your pending tasks by tomorrow").count(), 2)
        self.assertEqual({t.assignee_user_id for t in SharedTask.objects.filter(title="Complete all your pending tasks by tomorrow")}, {self.nikki.id, self.pa.id})
        self.assertEqual(d["spoken_reply"], "Nikki aur Priya ko bhej diya.")
        self.assertIn(d["speak_text"], ("Nikki aur Priya ko bhej diya.", "Nikki और Priya को भेज दिया।"))  # the twin of his language
        self.assertTrue(d["booked"] and d["undo"])
        self.assertFalse(d.get("batch"))

    def test_a_list_card_is_spoken_in_the_brains_words_not_read_item_by_item(self):
        # Different work for different people: the list stays on the phone for his yes; the line said with it is the
        # brain's one question, never the card's read-back; the card itself is untouched.
        def line(results):
            fact = results["t1_0"]
            self.assertTrue(fact["card"], fact)
            self.assertEqual([(i["to"], i["what"]) for i in fact["items"]], [("Nikki", "Check the ledger"), ("Priya", "Send the courier")])
            self.assertNotIn("needs", fact)
            return {"say_en": "Nikki aur Priya — bhej dun?", "say_hi": "Nikki और Priya — भेज दूँ?", "about": []}

        model = _FakeModel([("give_work", {"items": [{"to": "Nikki", "title": "Check the ledger"}, {"to": "Priya", "title": "Send the courier"}], "send_now": False})], line)
        d = self.say("Nikki ko bolo ledger check kare aur Priya ko bolo courier bhej de", model)
        self.assertEqual(len(d.get("batch") or []), 1)
        self.assertFalse(d.get("booked"))
        self.assertFalse(SharedTask.objects.filter(title="Check the ledger").exists())
        self.assertEqual(d["spoken_reply"], "Nikki aur Priya — bhej dun?")
        self.assertEqual([b["title"] for b in d["batch"]], ["Send the courier"])  # the card itself is untouched
        self.assertEqual(d["engine"], "act")
        # The brain's line is in the thread (what it said), and the yes runs the list in one line naming the people.
        sent = json.dumps(model.requests[-1]["messages"], ensure_ascii=False)
        self.assertIn("the list is on the phone and nothing is sent yet", sent)  # the facts went to the brain, not a read-back
        card = {k: v for k, v in d.items() if k not in ("say", "view")}
        r = self.md_client.post("/api/v1/assistant/execute/", {"card": card}, format="json", HTTP_IDEMPOTENCY_KEY="list-1")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["en"], "Sent to Nikki and Priya.")
        self.assertEqual(r.json()["hi"], "Nikki और Priya को भेज दिया।")
        self.assertTrue(SharedTask.objects.filter(title="Check the ledger", assignee_user=self.nikki).exists())
        self.assertTrue(SharedTask.objects.filter(title="Send the courier", assignee_user=self.pa).exists())

    def test_send_now_waits_when_the_card_still_needs_an_answer(self):
        # A name nobody knows: nothing is sent; the brain gets what the card needs and asks in its own words.
        def line(results):
            fact = results["t1_0"]
            self.assertTrue(fact.get("card") and fact.get("needs"), fact)
            return {"say_en": "Aman kaun hai? Team mein nahi mila.", "say_hi": "", "about": []}

        model = _FakeModel([("give_work", {"items": [{"to": "Nikki", "title": "File the return"}, {"to": "Aman", "title": "File the return"}], "send_now": True})], line)
        d = self.say("Nikki aur Aman ko bolo return file karein", model)
        self.assertFalse(d.get("booked"))
        self.assertFalse(SharedTask.objects.filter(title="File the return").exists())
        self.assertEqual(d["spoken_reply"], "Aman kaun hai? Team mein nahi mila.")

    def say_asked(self, text, model, asked, pending=None):
        with mock.patch("core.ai_brain.ai_available", return_value=True), mock.patch("core.act._client", return_value=model), \
                mock.patch("core.ai_brain.interpret", mock.Mock(side_effect=AssertionError("the fixed engine must not read this"))):
            body = {"text": text, "awaiting": 'JARVIS asked: "Sir, 2 reminders. Shall I tell you?"', "asked": asked, "pending": pending}
            r = self.md_client.post("/api/v1/assistant/interpret/", body, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_the_answer_to_a_count_is_read_by_the_brain_with_the_count_in_front_of_it(self):
        # 27 Nov 2026: "batao batao" used to go to the old classifier, which asked "kya bataun?". The brain now sees
        # what was asked and answers through answer_asked; the phone acts on the same draft shapes as before.
        asked = {"kind": "offer", "text": "Sir, 2 reminders. Shall I tell you?",
                 "items": [{"id": "rem:1", "what": "reminder", "title": "Call the bank", "from": "", "question": {"kind": "done", "en": "Done, or remind you later?"}},
                           {"id": "alert:7", "what": "message", "title": "Papers ready", "from": "Nikki", "question": None}]}
        model = _FakeModel([("answer_asked", {"decision": "yes"})], lambda r: {"say_en": "Ji.", "say_hi": "जी।", "about": []})
        d = self.say_asked("batao batao", model, asked)
        self.assertEqual((d["action"], d["engine"], d["asked_kind"]), ("confirm_pending", "act", "offer"))
        turn = model.requests[0]["messages"][-1]["content"][0]["text"]
        self.assertIn('<asked kind="count">JARVIS asked: "Sir, 2 reminders. Shall I tell you?"', turn)
        self.assertIn('[rem:1] reminder: "Call the bank" (its question: Done, or remind you later?)', turn)
        self.assertIn('[alert:7] message from Nikki: "Papers ready"', turn)
        self.assertIn("answer_asked", {t["name"] for t in model.requests[0]["tools"]})
        # Part of them: "sirf Nikki wala" → read_some with those keys; "20 minute baad" → snooze with the moment; no → cancel.
        d = self.say_asked("sirf Nikki wala", _FakeModel([("answer_asked", {"decision": "yes", "which": ["alert:7", "nope"]})]), asked)
        self.assertEqual((d["action"], d["read_keys"]), ("read_some", ["alert:7"]))
        d = self.say_asked("20 minute baad", _FakeModel([("answer_asked", {"decision": "later", "minutes": 20})],
                                                        lambda r: {"say_en": f"Theek hai, {r['t1_0']['until']}.", "say_hi": "", "about": []}), asked)
        self.assertEqual(d["action"], "snooze")
        until = tu.to_local(__import__("datetime").datetime.fromisoformat(d["snooze_until"]))
        self.assertAlmostEqual((until - tu.now_local()).total_seconds(), 20 * 60, delta=90)
        self.assertIn("Theek hai,", d["spoken_reply"])
        d = self.say_asked("nahi rehne do", _FakeModel([("answer_asked", {"decision": "no"})]), asked)
        self.assertEqual(d["action"], "cancel_pending")

    def test_an_instruction_said_over_a_count_is_done_and_the_count_stays(self):
        sid = str(self.sharma.id)
        asked = {"kind": "offer", "text": "Sir, one reminder. Shall I tell you?",
                 "items": [{"id": "rem:1", "what": "reminder", "title": "Call the bank", "from": "", "question": None}]}
        model = _FakeModel([("edit_item", {"id": sid, "priority": "URGENT"})], lambda r: {"say_en": "Done: Meeting with Sharma ji, urgent.", "say_hi": "", "about": [sid]})
        d = self.say_asked("Sharma ji wali ko urgent kar do", model, asked)
        self.assertEqual(d["action"], "answer")  # not an answer to the count: the phone leaves the count where it is
        self.sharma.refresh_from_db()
        self.assertEqual(self.sharma.priority, "URGENT")

    def test_a_reminders_question_and_a_card_are_answered_the_same_way(self):
        asked = {"kind": "question", "text": "Done, or remind you later?", "title": "Call the bank", "question": {"kind": "done", "en": "Done, or remind you later?"}}
        d = self.say_asked("ho gaya", _FakeModel([("answer_asked", {"decision": "yes"})]), asked)
        self.assertEqual((d["action"], d["asked_kind"]), ("confirm_pending", "question"))
        card = {"action": "private_task", "title": "Call Mehta ji", "transcript": "Mehta ji ko call karna hai", "due_label": "tomorrow 11 AM"}
        model = _FakeModel([("answer_asked", {"decision": "yes"})])
        d = self.say_asked("haan save karo", model, {"kind": "card", "text": "save it?"}, pending=card)
        self.assertEqual(d["action"], "confirm_pending")
        self.assertIn('<asked kind="card">A card waits on the phone for a yes: Personal task: Call Mehta ji, tomorrow 11 AM', model.requests[0]["messages"][-1]["content"][0]["text"])

    def test_the_closed_app_count_goes_to_the_brain_too(self):
        from core import conversation
        conversation.turn(self.md, "", event="ask", about={"kind": "offer", "question": "Sir, 2 reminders. Shall I tell you?",
                                                           "items": [{"kind": "reminder", "key": "k1", "title": "Call the bank", "spoken": "Sir, call the bank.", "question": None},
                                                                     {"kind": "reminder", "key": "k2", "title": "Pay the vendor", "spoken": "Sir, pay the vendor.", "question": None}]})
        model = _FakeModel([("answer_asked", {"decision": "yes", "which": ["k2"]})], lambda r: {"say_en": "Ji.", "say_hi": "", "about": []})
        with mock.patch("core.ai_brain.ai_available", return_value=True), mock.patch("core.act._client", return_value=model), \
                mock.patch("core.ai_brain.interpret", mock.Mock(side_effect=AssertionError("the fixed engine must not read this"))):
            r = conversation.turn(self.md, "vendor wala pehle sunao")
        self.assertTrue(r["show"].startswith("Sir, pay the vendor."), r)
        self.assertIn("one more. Shall I tell you?", r["show"])
        self.assertIn('[k1] reminder: "Call the bank"', model.requests[0]["messages"][-1]["content"][0]["text"])

    def test_work_in_the_staff_panel_cannot_be_taken_back_from_here_and_jarvis_says_so(self):
        # 27 Nov 2026: undo said "Undone" and the panel still had the tasks (its gateway has no cancel). Now the
        # record stays, and the line says where the work is.
        from core.models import StaffMember
        staff = StaffMember.objects.filter(active=True).first()
        task, _cmd = services.delegate_to_staff(self.md, {"title": "Send the GST file"}, staff, description="Send the GST file")
        actions_mod = __import__("core.actions", fromlist=["undo_last"])
        actions_mod.remember_done(self.md, [{"type": "shared", "id": str(task.id)}], task.title)
        out = actions_mod.undo_last(self.md)
        self.assertEqual(out["undone"], 0)
        self.assertEqual(out["kept"], ["Send the GST file"])
        self.assertIn(f"Send the GST file stays with {staff.name}: I can't take a panel task back from here", out["en"])
        self.assertTrue(SharedTask.objects.filter(id=task.id).exists())  # JARVIS's record stays: the lists never disagree
        # "Nikki ka wo task hata do": the brain is told the same, and the task stays open.
        def line(results):
            fact = results["t1_0"]
            self.assertEqual(fact["cancelled"], [])
            self.assertEqual(fact["in_panel"][0]["title"], "Send the GST file")
            return {"say_en": f"Wo {staff.name} ke panel mein hai, yahan se cancel nahi hota.", "say_hi": "", "about": []}
        d = self.say("GST wala task hata do", _FakeModel([("cancel_items", {"ids": [str(task.id)]})], line))
        self.assertFalse(d["booked"])
        self.assertEqual(SharedTask.objects.get(id=task.id).status, "OPEN")
        self.assertIn("panel", d["spoken_reply"])

    def test_unsure_hands_the_same_turn_fresh_to_the_strong_model(self):
        sid = str(self.sharma.id)
        model = _FakeModel({"escalate": True}, [("edit_item", {"id": sid, "priority": "URGENT"})],
                           lambda r: {"say_en": "Done: Meeting with Sharma ji, urgent.", "say_hi": "", "about": [sid]})
        d = self.say("isko urgent kar do", model)
        self.assertEqual([r["model"] for r in model.requests], ["claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5-5"])
        self.sharma.refresh_from_db()
        self.assertEqual((self.sharma.priority, d["model"]), ("URGENT", "claude-opus-5-5"))
        self.assertNotIn("escalate", json.dumps(model.requests[1]["messages"]))  # the weak reading is not in its thread

    def test_when_the_model_cannot_be_reached_the_fixed_engine_reads_the_sentence(self):
        read = _cmd(intent="change_task", target_refs=[str(self.sharma.id)], changed_fields=["priority"], priority="HIGH", spoken_reply="Ji Sir.")
        d = self.say("Sharma wali ko high priority kar do", _Down(), brain=mock.Mock(return_value=read))
        self.assertNotEqual(d.get("engine"), "act")
        self.sharma.refresh_from_db()
        self.assertEqual(self.sharma.priority, "HIGH")
        self.assertFalse(BrainThread.objects.filter(user=self.md, turns__gt=0).exists())

    def test_what_has_no_tool_is_handed_to_the_fixed_engine(self):
        read = _cmd(intent="set_setting", setting_key="wakeWord", setting_value="OK Boss", spoken_reply="Ji Sir, ab OK Boss boliye.")
        brain = mock.Mock(return_value=read)
        d = self.say("ab mujhe OK Boss bolke bulao", _FakeModel([("old_engine", {})]), brain=brain)
        self.assertEqual((d["engine"], brain.call_count), ("act", 1))

    def test_the_live_line_sends_the_sentence_through_talk_and_a_card_keeps_the_fixed_engine(self):
        secret = {"value": "xai-short-lived-token", "expires_at": 0}
        with mock.patch("core.ai_brain.ai_available", return_value=True), mock.patch("core.xai.configured", return_value=True), \
                mock.patch("core.xai.client_secret", return_value=secret):
            r = self.md_client.get("/api/v1/assistant/live/session/")
        self.assertEqual(r.status_code, 200, r.content)
        sess = r.json()["session"]
        self.assertEqual({t["name"] for t in sess["tools"] if t.get("type") == "function"}, {"talk", "setting", "screen", "briefing", "dismiss", "end"})
        self.assertIn("talk for everything the speaker says", sess["instructions"])
        sid = str(self.sharma.id)
        model = _FakeModel([("edit_item", {"id": sid, "title": "Test budget review"})],
                           lambda r: {"say_en": "Done: Test budget review.", "say_hi": "हो गया: Test budget review।", "about": [sid]})
        with mock.patch("core.ai_brain.ai_available", return_value=True), mock.patch("core.act._client", return_value=model), \
                mock.patch("core.ai_brain.interpret", side_effect=AssertionError("the fixed engine must not read this")):
            r = self.md_client.post("/api/v1/assistant/live/tool/", {"name": "talk", "arguments": {"said": "Sharma ji wali meeting ko Test budget review naam do"}}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        self.assertEqual(d["spoken_reply"], "Done: Test budget review.")
        self.assertIn(d["say"], ("Done: Test budget review.", "हो गया: Test budget review।"))  # the voice reads the twin of the speaker's language
        self.sharma.refresh_from_db()
        self.assertEqual(self.sharma.title, "Test budget review")
        # A card waiting on the phone keeps the fixed engine: "haan" through talk is read by it, never by the thread.
        with mock.patch("core.ai_brain.ai_available", return_value=True), \
                mock.patch("core.act._client", side_effect=AssertionError("the thread must not read this")), \
                mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="confirm_pending")) as brain:
            r = self.md_client.post("/api/v1/assistant/live/tool/", {"name": "talk", "arguments": {"said": "haan"}, "awaiting": "JARVIS asked: save it?",
                                                                      "pending": {"action": "private_task", "title": "x", "transcript": "x"}}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(brain.called)


@unittest.skipUnless(os.environ.get("JARVIS_REAL_BRAIN_TESTS") == "1", "set JARVIS_REAL_BRAIN_TESTS=1 to run against the real models")
@override_settings(JARVIS_AI_ENABLED=True, JARVIS_AI_ACT_ENABLED=True)
class ActBankTests(BaseCase):
    """His sentences against the real models (the bank): a rename, a move onto a clash and its yes, "abhi"."""

    def setUp(self):
        super().setUp()
        self.day = _workday(1)
        self.sharma = services.create_private_task(self.md, {"title": "Meeting with Sharma ji", "kind": Kind.MEETING, "priority": Priority.NORMAL,
                                                             "due_date": self.day, "due_time": time(10, 30), "duration_minutes": 30})
        self.mehta = services.create_private_task(self.md, {"title": "Meeting with Mehta ji", "kind": Kind.MEETING, "priority": Priority.NORMAL,
                                                            "due_date": self.day, "due_time": time(16, 0), "duration_minutes": 30})

    def say(self, text):
        r = self.md_client.post("/api/v1/assistant/interpret/", {"text": text}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        self.assertEqual(d.get("engine"), "act", d.get("spoken_reply"))
        return d

    def test_rename_move_with_clash_and_now(self):
        self.assertTrue(act.enabled())
        d = self.say("Sharma ji wali meeting ka naam Test GST review rakh do")
        self.sharma.refresh_from_db()
        self.assertEqual(self.sharma.title, "Test GST review", d["spoken_reply"])
        d = self.say("Test GST review wali ko 4 baje kar do")
        self.sharma.refresh_from_db()
        self.assertEqual(tu.to_local(self.sharma.due_at).hour, 10, d["spoken_reply"])     # a clash: asked, not moved
        self.assertIn("Mehta", d["spoken_reply"])
        d = self.say("haan")
        self.sharma.refresh_from_db()
        self.assertEqual(tu.to_local(self.sharma.due_at).strftime("%H:%M"), "16:30", d["spoken_reply"])
        d = self.say("Mehta ji wali meeting abhi kar do")
        self.mehta.refresh_from_db()
        self.assertLess(abs((tu.to_local(self.mehta.due_at) - tu.now_local()).total_seconds()), 180, d["spoken_reply"])
