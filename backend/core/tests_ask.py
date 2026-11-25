"""The answer path (24 Nov 2026): a question is answered in one exact sentence from the facts the model fetched with
the lookups; the item it spoke about becomes the cursor; "dikhao" keeps the card; a model that cannot be reached
leaves the templates to answer. The model here is a scripted double; the lookups run for real."""
import json
import os
import unittest
from datetime import time, timedelta
from unittest import mock

import anthropic
import httpx
from django.test import override_settings

from core import ask, services
from core import timeutils as tu
from core.models import Kind, Priority, SharedTask
from core.tests import BaseCase, _cmd, _workday


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Response:
    def __init__(self, content, stop_reason):
        self.content, self.stop_reason, self.usage = content, stop_reason, None


class _FakeClient:
    """Round 1: the model calls the lookups it is given; round 2: it answers with the object built by `final`."""

    def __init__(self, calls: list[dict], final):
        self.calls, self.final, self.requests = calls, final, []
        self.messages = self

    def create(self, **kw):
        self.requests.append(kw)
        msgs = kw["messages"]
        if len(msgs) == 1:
            blocks = [_Block(type="tool_use", id=f"t{n}", name=c["name"], input=c.get("input") or {}) for n, c in enumerate(self.calls)]
            return _Response(blocks, "tool_use")
        results = {r["tool_use_id"]: json.loads(r["content"]) for r in msgs[-1]["content"]}
        answer = self.final(results)
        return _Response([_Block(type="text", text=json.dumps(answer, ensure_ascii=False))], "end_turn")


@override_settings(JARVIS_AI_ASK_ENABLED=True, JARVIS_AI_ACT_ENABLED=False)  # the answer path on its own; the act path has tests_act
class AnswerPathTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.nikki = services.team_add(self.md, {"name": "Nikki", "title": "Accountant", "username": "nikki", "password": "nikki1"})
        day = _workday(1)
        self.sharma = services.create_private_task(self.md, {"title": "Meeting with Sharma ji", "kind": Kind.MEETING, "priority": Priority.NORMAL,
                                                             "due_date": day, "due_time": time(10, 30), "duration_minutes": 30})
        self.mehta = services.create_private_task(self.md, {"title": "Meeting with Mehta ji", "kind": Kind.MEETING, "priority": Priority.NORMAL,
                                                            "due_date": day, "due_time": time(16, 0), "duration_minutes": 30})
        self.day = day

    def interpret(self, text, cmd, client):
        with mock.patch("core.ai_brain.ai_available", return_value=True), \
                mock.patch("core.ai_brain._get_answer_client", return_value=client), \
                mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = self.md_client.post("/api/v1/assistant/interpret/", {"text": text}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_the_next_meeting_is_one_sentence_and_becomes_the_cursor(self):
        def final(results):
            facts = results["t0"]
            # The lookup gave the model the real facts, in time order: the fixture's meeting (today, in an hour), then
            # tomorrow's two; every one with "in_minutes" so the model never does clock maths.
            titles = [i["title"] for i in facts["items"]]
            self.assertEqual(titles[1:], ["Meeting with Sharma ji", "Meeting with Mehta ji"], titles)
            self.assertTrue(all(i["in_minutes"] > 0 for i in facts["items"]))
            first = facts["items"][1]
            return {"say_en": "Sharma ji ke saath, kal 10:30 baje.", "say_hi": "शर्मा जी के साथ, कल 10:30 बजे।", "about": [first["id"]], "show": ""}
        client = _FakeClient([{"name": "next_up", "input": {"count": 3, "kinds": ["MEETING"]}}], final)
        d = self.interpret("meri agli meeting kaunsi hai", _cmd(intent="query", view_kind="upcoming", spoken_reply="Sir, 22 items…"), client)
        self.assertEqual(d["spoken_reply"], "Sharma ji ke saath, kal 10:30 baje.")
        self.assertEqual(d["reply_hi"], "शर्मा जी के साथ, कल 10:30 बजे।")
        self.assertNotIn("22 items", d["spoken_reply"])
        # The strongest model, the lookups on the table, the question at the end.
        first = client.requests[0]
        self.assertEqual(first["model"], "claude-opus-5-5")
        self.assertEqual({t["name"] for t in first["tools"]}, {"diary", "next_up", "person", "waiting", "item", "search", "panel", "counts"})
        self.assertIn("meri agli meeting kaunsi hai", first["messages"][0]["content"])
        # The item spoken about is the cursor: "isko hata do" / "iske baad kya" next will mean it.
        last = services.last_view(self.md)
        self.assertEqual([i["id"] for i in last["items"]], [str(self.sharma.id)])
        self.assertEqual(last["cursor"], 0)

    def test_no_meeting_at_twelve_is_said_plainly_from_the_diary(self):
        def final(results):
            diary = results["t0"]["days"][0]
            self.assertEqual([i["start"][11:] for i in diary["items"]], ["10:30", "16:00"])
            self.assertTrue(diary["free"])  # the gaps are there for "am I free at 12"
            return {"say_en": "Nahi, 12 baje kuch nahi hai; agli 4 baje Mehta ji ke saath hai.", "say_hi": "नहीं, 12 बजे कुछ नहीं है।", "about": [diary["items"][1]["id"]], "show": ""}
        client = _FakeClient([{"name": "diary", "input": {"day": self.day.isoformat()}}], final)
        d = self.interpret("kal 12 baje koi meeting hai?", _cmd(intent="query", view_kind="today"), client)
        self.assertTrue(d["spoken_reply"].startswith("Nahi, 12 baje"), d["spoken_reply"])
        self.assertEqual([i["id"] for i in services.last_view(self.md)["items"]], [str(self.mehta.id)])

    def test_an_instruction_filed_as_a_question_is_done_not_answered(self):
        # "Nikki ko bolo GST file bheje" reached the server as a question (the voice chose show): the answer model
        # says it is not a question, the brain re-reads the words and the work is given; never "main yahan se nahi
        # bhej sakta" (his test, 25 Nov 2026).
        client = _FakeClient([], lambda r: {"do": True})
        query = _cmd(intent="query", view_kind="person", view_who="Nikki")
        work = _cmd(intent="assign_to_person", staff_name="Nikki", recipients=["Nikki"], title="Send the GST file", spoken_reply="Ji Sir.")
        with mock.patch("core.ai_brain.ai_available", return_value=True), \
                mock.patch("core.ai_brain._get_answer_client", return_value=client), \
                mock.patch("core.ai_brain.interpret", side_effect=[query, work]) as brain:
            r = self.md_client.post("/api/v1/assistant/interpret/", {"text": "Nikki ko bolo GST file bheje"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        self.assertEqual(brain.call_count, 2)
        self.assertTrue(d["booked"], d["spoken_reply"])
        self.assertIn("Nikki has it", d["spoken_reply"])
        self.assertEqual(SharedTask.objects.get(title="Send the GST file").assignee_user, self.nikki)
        # The question path is not asked twice: the answer model saw the sentence once.
        self.assertEqual(len(client.requests), 2)

    def test_dikhao_keeps_the_card_and_never_calls_the_model(self):
        client = _FakeClient([], lambda r: {})
        d = self.interpret("kal ke tasks dikhao", _cmd(intent="query", view_kind="tasks", view_from=self.day.isoformat(), view_to=self.day.isoformat()), client)
        self.assertEqual(client.requests, [])
        self.assertIn("view", d)

    def test_when_the_model_cannot_be_reached_the_templates_answer(self):
        class Down:
            messages = None

            def __init__(self):
                self.messages = self

            def create(self, **kw):
                raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))

        d = self.interpret("meri agli meeting kaunsi hai", _cmd(intent="query", view_kind="upcoming"), Down())
        self.assertTrue(d["spoken_reply"])
        self.assertIn("view", d)

    def test_the_lookups_read_the_real_data(self):
        services.create_person_task(self.md, self.nikki, {"title": "Bank reconciliation", "kind": Kind.TASK, "due_date": self.day}, compose_note=False)
        services.send_message_to(self.nikki, [self.md], "GST file bhej diya hai")
        f = ask._Facts(self.md, True)
        person = ask.run_lookup(f, "person", {"name": "Nikki"})
        self.assertEqual(person["found"][0]["name"], "Nikki")
        self.assertEqual(person["found"][0]["open_work"], 1)
        self.assertEqual(person["found"][0]["work"][0]["title"], "Bank reconciliation")
        self.assertEqual(f.person_named, "Nikki")
        waiting = ask.run_lookup(f, "waiting", {"types": ["MESSAGE"], "who": "Nikki"})
        self.assertEqual([w["title"] for w in waiting["items"]], ["GST file bhej diya hai"])
        found = ask.run_lookup(f, "search", {"words": "reconciliation"})
        self.assertEqual([i["title"] for i in found["items"]], ["Bank reconciliation"])
        counts = ask.run_lookup(f, "counts", {})
        self.assertIn("pending", counts)
        self.assertEqual(counts["team_open_work"].get("Nikki"), 1)
        nobody = ask.run_lookup(f, "person", {"name": "Zubin"})
        self.assertIn("nobody by that name", nobody["note"])
        after = ask.run_lookup(f, "next_up", {"count": 1, "after_id": str(self.sharma.id)})
        self.assertEqual(after["items"][0]["title"], "Meeting with Mehta ji")   # "iske baad kya"
        one = ask.run_lookup(f, "item", {"id": str(self.sharma.id)})
        self.assertEqual((one["title"], one["scope"]), ("Meeting with Sharma ji", "private"))

    def test_the_locked_phone_keeps_personal_items_out_of_the_facts(self):
        f = ask._Facts(self.md, False)
        nxt = ask.run_lookup(f, "next_up", {"count": 5})
        self.assertNotIn("Meeting with Sharma ji", [i["title"] for i in nxt["items"]])

    def test_the_thread_is_held_for_the_day(self):
        self.assertEqual(services.LAST_VIEW_TTL, timedelta(hours=24))
        services.remember_last_view(self.md, {"kind": "ask"}, "x", items=[{"id": str(self.sharma.id), "title": "Meeting with Sharma ji"}], cursor=0)
        self.assertEqual(services.last_view(self.md)["cursor"], 0)
        self.assertLess(tu.now_local() - tu.now_local(), timedelta(seconds=1))


@unittest.skipUnless(os.environ.get("JARVIS_REAL_BRAIN_TESTS") == "1" and os.environ.get("ANTHROPIC_API_KEY"),
                     "set JARVIS_REAL_BRAIN_TESTS=1 (and the API key) to run the bank against the real model")
@override_settings(JARVIS_AI_ENABLED=True, JARVIS_AI_ASK_ENABLED=True)
class AnswerBankTests(BaseCase):
    """His own sentences (24 Nov 2026) against the real model, each with what the one-line answer must contain.
    The fixture is small and known, so the right answer is known too."""

    def setUp(self):
        super().setUp()
        self.nikki = services.team_add(self.md, {"name": "Nikki", "title": "Accountant", "username": "nikki", "password": "nikki1"})
        day = _workday(1)
        self.day = day
        self.sharma = services.create_private_task(self.md, {"title": "Meeting with Sharma ji", "kind": Kind.MEETING, "priority": Priority.NORMAL,
                                                             "due_date": day, "due_time": time(10, 30), "duration_minutes": 30})
        self.mehta = services.create_private_task(self.md, {"title": "Meeting with Mehta ji", "kind": Kind.MEETING, "priority": Priority.NORMAL,
                                                            "due_date": day, "due_time": time(16, 0), "duration_minutes": 30})
        services.create_person_task(self.md, self.nikki, {"title": "Bank reconciliation", "kind": Kind.TASK, "due_date": day}, compose_note=False)
        services.send_message_to(self.nikki, [self.md], "GST file bhej diya hai")
        services.send_message_to(self.nikki, [self.md], "Vendor ka call aaya tha")

    def ask(self, text: str) -> dict:
        r = self.md_client.post("/api/v1/assistant/interpret/", {"text": text}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_bank(self):
        day_word = "kal" if self.day == tu.today_local() + timedelta(days=1) else self.day.strftime("%A")
        bank = [
            (f"{day_word} meri agli meeting kaunsi hai", ["sharma"], ["22 items", "coming up"]),
            (f"{day_word} 12 baje koi meeting hai?", ["nahi", "no"], ["sharma"]),
            ("Nikki ke baare mein batao", ["nikki", "accountant"], []),
            ("Nikki ki taraf se koi message hai?", ["2", "do", "two"], ["samajh"]),
            ("mujhe koi lease sign karni hai kya?", ["nahi", "no"], ["samajh"]),
            # Two meetings of 30 minutes: the total, said in hours and minutes, never "60 30 30" item by item.
            (f"mujhe {day_word} kitna time meetings mein jaayega?", ["1 ghant", "ek ghant", "1 hour", "one hour", "60 min"], ["samajh", "nahi pata", "specified"]),
        ]
        failures = []
        for text, must, must_not in bank:
            d = self.ask(text)
            said = (d.get("spoken_reply") or "").lower()
            if not any(m in said for m in must) or any(m in said for m in must_not) or len(said.split(". ")) > 3:
                failures.append(f"{text!r} -> {d.get('spoken_reply')!r}")
        # "Iske baad kya": the cursor from the first answer.
        self.ask(f"{day_word} meri agli meeting kaunsi hai")
        d = self.ask("iske baad kya hai?")
        if "mehta" not in (d.get("spoken_reply") or "").lower():
            failures.append(f"'iske baad kya hai?' -> {d.get('spoken_reply')!r}")
        # "Pehla kya hai" after a count of messages: the list of the last answer is the cursor list.
        self.ask("Nikki ki taraf se kya kya aaya hai?")
        d = self.ask("pehla kya hai?")
        said = (d.get("spoken_reply") or "").lower()
        if not ("gst" in said or "vendor" in said):
            failures.append(f"'pehla kya hai?' -> {d.get('spoken_reply')!r}")
        self.assertEqual(failures, [], "\n".join(failures))
