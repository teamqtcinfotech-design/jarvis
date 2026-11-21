"""The live voice line (core/livevoice.py): the form the voice model fills becomes the brain's own command, the
server reads the time words itself, and the usual rules (who, clashes, privacy, cards) run unchanged."""
import json
from datetime import date, timedelta
from unittest import mock

from core import livevoice, services
from core import timeutils as tu
from core.models import Alert, Kind, Priority, PrivateTask, SharedTask
from core.tests import BaseCase


class TimeWordsTests(BaseCase):
    def test_devanagari_and_latin_time_words_read_by_the_server(self):
        parso = (tu.today_local() + timedelta(days=2)).isoformat()
        kal = (tu.today_local() + timedelta(days=1)).isoformat()
        self.assertEqual(livevoice.roman_when("परसों चार बजे"), "parso 4 baje")
        self.assertEqual(livevoice.when_from_words("परसों चार बजे"), {"due_date": parso, "due_time": "16:00"})
        self.assertEqual(livevoice.when_from_words("parso 4 baje"), {"due_date": parso, "due_time": "16:00"})
        self.assertEqual(livevoice.when_from_words("kal subah"), {"due_date": kal, "due_time": "10:00"})
        # The half and quarter hours as they are really said (Nov 2026): "saadhe gyarah" is 11:30, not 10:00.
        self.assertEqual(livevoice.when_from_words("kal subah saadhe gyarah baje")["due_time"], "11:30")
        self.assertEqual(livevoice.when_from_words("dopahar saadhe barah baje")["due_time"], "12:30")
        self.assertEqual(livevoice.when_from_words("साढ़े ग्यारह बजे")["due_time"], "11:30")
        self.assertEqual(livevoice.when_from_words("paune char baje")["due_time"], "15:45")
        self.assertEqual(livevoice.when_from_words("sawa paanch baje")["due_time"], "17:15")
        self.assertEqual(livevoice.when_from_words("kal do baje")["due_time"], "14:00")
        self.assertEqual(livevoice.when_from_words("kar do"), {"due_date": "", "due_time": ""})
        self.assertEqual(livevoice.when_from_words("कल शाम"), {"due_date": kal, "due_time": "18:00"})
        self.assertEqual(livevoice.when_from_words(""), {"due_date": "", "due_time": ""})
        # A calendar date, however far: "12 December ko 11 baje" is the coming 12 December, never today.
        today = tu.today_local()
        dec12 = date(today.year if date(today.year, 12, 12) >= today else today.year + 1, 12, 12).isoformat()
        for words in ("12 December ko 11 baje", "12 tarikh December ko 11 baje", "December 12 at 11", "12 Dec 11 am", "12 दिसंबर को ग्यारह बजे", "12/12 11 baje"):
            self.assertEqual(livevoice.when_from_words(words), {"due_date": dec12, "due_time": "11:00"}, words)
        self.assertEqual(livevoice.when_from_words("12 December 2027 ko 11 baje"), {"due_date": "2027-12-12", "due_time": "11:00"})
        # "25 tarikh" is this month's 25th, or next month's once it has passed; "agle mahine 25 tarikh" is next month's.
        nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=25)
        this = today.replace(day=25) if today.replace(day=25) >= today else nxt
        self.assertEqual(livevoice.when_from_words("25 tarikh ko 4 baje")["due_date"], this.isoformat())
        self.assertEqual(livevoice.when_from_words("agle mahine 25 tarikh")["due_date"], nxt.isoformat())
        # A bare clock time names no day: the model's date stands (the server used to replace it with today).
        self.assertEqual(livevoice.when_from_words("11 baje"), {"due_date": "", "due_time": "11:00"})
        cmd = livevoice.cmd_for(self.md, "set_meeting", {"with_person": "", "people": ["Aman"], "title": "Meeting with Aman", "when_words": "11 baje",
                                                         "due_date": dec12, "due_time": "11:00", "said": "x"})
        self.assertEqual((cmd["due_date"], cmd["due_time"]), (dec12, "11:00"))

    def test_the_servers_reading_beats_the_models_guess(self):
        parso = (tu.today_local() + timedelta(days=2)).isoformat()
        cmd = livevoice.cmd_for(self.md, "give_work", {"items": [{"to": "Priya", "what": "Vendor report", "when_words": "parso 4 baje",
                                                                  "due_date": tu.today_local().isoformat(), "due_time": "16:00"}],
                                                       "said": "Priya ko bolo parso 4 baje vendor report"})
        self.assertEqual((cmd["intent"], cmd["staff_name"], cmd["recipients"]), ("assign_to_person", "Priya", ["Priya"]))
        self.assertEqual((cmd["due_date"], cmd["due_time"]), (parso, "16:00"))  # not today, as the model guessed
        self.assertEqual(cmd["reply_language"], "en")
        # No time words: the model's own date stands; a span from now wins over both.
        cmd = livevoice.cmd_for(self.md, "own_item", {"what": "Pay the EMI", "when_words": "", "due_date": parso, "due_time": "11:30", "said": "x"})
        self.assertEqual((cmd["intent"], cmd["due_date"], cmd["due_time"]), ("create_task", parso, "11:30"))
        cmd = livevoice.cmd_for(self.md, "remind_me", {"what": "Call Aman", "in_minutes": 10, "when_words": "10 minute baad", "said": "x"})
        self.assertEqual((cmd["kind"], cmd["due_in_minutes"], cmd["due_date"]), ("REMINDER", 10, ""))


class FormToCommandTests(BaseCase):
    def test_every_tool_becomes_a_known_intent(self):
        forms = {
            "give_work": {"items": [{"to": "everyone", "what": "Office closed tomorrow", "when_words": ""}], "said": "s"},
            "set_meeting": {"with_person": "Priya", "title": "", "when_words": "kal 4 baje", "said": "s"},
            "own_item": {"what": "Go to the bank", "when_words": "", "personal": True, "said": "s"},
            "remind_me": {"what": "Water", "when_words": "", "in_minutes": 5, "said": "s"},
            "send_message": {"to": ["Priya"], "text": "Vendor is here", "said": "s"},
            "show": {"what": "today", "who": "me", "said": "s"},
            "change": {"which_id": "abc", "which_words": "", "when_words": "kal", "said": "s"},
            "complete": {"which_words": "bank wala", "said": "s"},
            "cancel_item": {"which_words": "bank wala", "said": "s"},
            "answer": {"reply": "yes", "said": "haan"},
            "undo": {"said": "undo"}, "remember": {"fact": "Mehta is our CA", "said": "s"},
            "organise": {"instruction": "Make a Farm department", "names": ["Priya"], "said": "s"},
            "setting": {"key": "snoozeMinutes", "value": "15", "said": "s"},
            "screen": {"action": "export_pdf", "said": "s"}, "briefing": {"said": "s"}, "dismiss": {"said": "s"}, "end": {"said": "bas"},
        }
        want = {"give_work": "assign_to_team", "set_meeting": "meet_with_person", "own_item": "create_task", "remind_me": "create_task",
                "send_message": "send_alert", "show": "query", "change": "change_task", "complete": "complete_task",
                "cancel_item": "delete_task", "answer": "confirm_pending", "undo": "undo", "remember": "remember", "organise": "organise",
                "setting": "set_setting", "screen": "screen_action", "briefing": "briefing", "dismiss": "dismiss_alert", "end": "end_conversation"}
        for name, args in forms.items():
            cmd = livevoice.cmd_for(self.md, name, args)
            self.assertEqual(cmd["intent"], want[name], name)
            self.assertIn("_said", cmd)
        self.assertEqual(livevoice.cmd_for(self.md, "give_work", forms["give_work"])["team_name"], "all")
        self.assertEqual(livevoice.cmd_for(self.md, "set_meeting", forms["set_meeting"])["kind"], "MEETING")
        # "Isko 4 baje rakho, baaki aage kar do": this one keeps its time, the rest move (the brain's push).
        pushed = livevoice.cmd_for(self.md, "set_meeting", {**forms["set_meeting"], "with_person": "", "people": ["Sharma"], "push_others": True})
        self.assertEqual((pushed["intent"], pushed["setting_value"]), ("create_task", "push"))
        self.assertTrue(livevoice.cmd_for(self.md, "own_item", forms["own_item"])["personal"])
        self.assertEqual(livevoice.cmd_for(self.md, "send_message", forms["send_message"])["notes"], "Vendor is here")
        self.assertEqual(livevoice.cmd_for(self.md, "change", forms["change"])["changed_fields"], ["due_date", "due_time"])
        self.assertEqual(livevoice.cmd_for(self.md, "change", forms["change"])["target_refs"], ["abc"])
        self.assertEqual(livevoice.cmd_for(self.md, "setting", forms["setting"])["setting_key"], "snoozeMinutes")
        # Several things in one breath: the first is the command, the rest ride along as more_items.
        cmd = livevoice.cmd_for(self.md, "give_work", {"items": [
            {"to": "Priya", "what": "Hotel book karo", "when_words": "aaj"},
            {"to": "Priya, Sunil", "what": "Vendor list", "when_words": "kal tak", "priority": "HIGH"}], "said": "s"})
        self.assertEqual(len(cmd["more_items"]), 1)
        self.assertEqual((cmd["more_items"][0]["staff_name"], cmd["more_items"][0]["recipients"]), ("Priya", ["Priya", "Sunil"]))
        self.assertEqual(cmd["more_items"][0]["priority"], "HIGH")

    def test_a_filled_form_runs_through_the_brain_without_a_model(self):
        parso = tu.today_local() + timedelta(days=2)
        with mock.patch("core.ai_brain.interpret", side_effect=AssertionError("no model must be asked")):
            d = livevoice.run_tool(self.md, "give_work", json.dumps({
                "items": [{"to": "Priya", "what": "Vendor report bhejna", "when_words": "परसों चार बजे", "priority": "HIGH"}],
                "said": "Priya ko bolo parso 4 baje vendor report bhej de"}))
        self.assertTrue(d.get("booked"), d.get("spoken_reply"))   # clear and complete: done at once, no "shall I?"
        task = SharedTask.objects.get(title="Vendor report bhejna")
        self.assertEqual((task.assignee_user, task.priority), (self.pa, Priority.HIGH))
        self.assertEqual((tu.to_local(task.due_at).date(), tu.to_local(task.due_at).strftime("%H:%M")), (parso, "16:00"))
        self.assertIn("Priya", d["say"])
        self.assertEqual(d["tool"], "give_work")
        self.assertTrue(Alert.objects.filter(recipient=self.pa, ref_id=str(task.id), ring=True).exists())

    def test_a_question_comes_back_with_the_card_and_the_servers_numbers(self):
        services.create_private_task(self.md, {"title": "Call the auditor", "kind": Kind.CALL, "priority": Priority.NORMAL,
                                               "due_date": tu.today_local()})
        with mock.patch("core.ai_brain.interpret", side_effect=AssertionError("no model must be asked")):
            d = livevoice.run_tool(self.md, "show", {"what": "today", "who": "me", "said": "aaj mera plan kya hai"})
        self.assertEqual(d["action"], "answer")
        self.assertIsNotNone(d["view"])
        self.assertIn("Call the auditor", json.dumps(d["view"]))
        self.assertTrue(d["say"])

    def test_an_end_and_an_undo(self):
        with mock.patch("core.ai_brain.interpret", side_effect=AssertionError("no model must be asked")):
            d = livevoice.run_tool(self.md, "end", {"said": "bas thank you"})
            self.assertEqual(d["action"], "end")
            d = livevoice.run_tool(self.md, "own_item", {"what": "Buy flowers", "when_words": "", "personal": True, "said": "mujhe flowers lene hain"})
            self.assertTrue(d.get("booked"), d.get("spoken_reply"))
            # Personal titles are encrypted at random: compared in Python, never looked up in SQL.
            self.assertIn("Buy flowers", [t.title for t in PrivateTask.objects.filter(owner=self.md)])
            d = livevoice.run_tool(self.md, "undo", {"said": "undo"})
        self.assertNotIn("Buy flowers", [t.title for t in PrivateTask.objects.filter(owner=self.md)])
        self.assertIn("Undone", d["spoken_reply"])


class LiveApiTests(BaseCase):
    def test_the_phone_gets_a_session_but_never_the_key(self):
        secret = {"value": "xai-short-lived-token", "expires_at": 1795081648}
        with mock.patch("core.xai.configured", return_value=True), mock.patch("core.xai.client_secret", return_value=secret):
            r = self.md_client.get("/api/v1/assistant/live/session/")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["token"], "xai-short-lived-token")
        self.assertTrue(body["url"].startswith("wss://") and "realtime" in body["url"])
        sess = body["session"]
        self.assertIn("JARVIS", sess["instructions"])
        self.assertIn("Priya", sess["instructions"])                # the team is in the briefing
        self.assertIn("Sir", sess["instructions"])
        self.assertNotIn("sk-", json.dumps(body))
        self.assertEqual({t["name"] for t in sess["tools"] if t.get("type") == "function"}, livevoice.TOOL_NAMES)
        self.assertEqual(sess["audio"]["input"]["format"]["rate"], livevoice.RATE)
        self.assertIn("Priya", sess["audio"]["input"]["transcription"]["keyterms"])
        # The PA's briefing never carries Sir's personal item (the fixture's secret meeting).
        with mock.patch("core.xai.configured", return_value=True), mock.patch("core.xai.client_secret", return_value=secret):
            r = self.pa_client.get("/api/v1/assistant/live/session/")
        self.assert_no_secret(r)
        self.assertIn("goes straight onto the MD's list", r.json()["session"]["instructions"])

    def test_without_the_voice_service_the_phone_is_told_to_use_the_usual_way(self):
        with mock.patch("core.xai.configured", return_value=False):
            r = self.md_client.get("/api/v1/assistant/live/session/")
        self.assertEqual(r.status_code, 503)

    def test_a_tool_call_from_the_phone_runs_the_usual_pipeline(self):
        with mock.patch("core.ai_brain.interpret", side_effect=AssertionError("no model must be asked")):
            r = self.pa_client.post("/api/v1/assistant/live/tool/", {
                "name": "give_work", "arguments": json.dumps({"items": [{"to": "Sir", "what": "Sign the lease", "when_words": "kal 11 baje"}],
                                                              "said": "Sir ko bolo kal 11 baje lease sign kar dein"})}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        self.assertTrue(d["booked"], d["spoken_reply"])
        task = SharedTask.objects.get(title="Sign the lease")
        self.assertEqual((task.assignee_user, task.assignee_type), (self.md, "MD"))  # straight onto Sir's list
        self.assertIn("Sir", d["spoken_reply"])
        self.assertTrue(d["say"])  # the line the voice reads (the Hindi twin when the reply is Hindi)
        # A question with a waiting card: "haan" through the answer tool saves it.
        with mock.patch("core.ai_brain.interpret", side_effect=AssertionError("no model must be asked")):
            r = self.md_client.post("/api/v1/assistant/live/tool/", {
                "name": "set_meeting", "arguments": {"with_person": "", "people": ["Mehta"], "title": "Meeting with Mehta", "when_words": "",
                                                     "said": "Mehta ke saath meeting rakho"}}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        self.assertTrue(d["needs_clarification"] or d["action"] in ("private_task", "answer"), d)
        # The phone's closed-app conversation (LiveLoop.kt) sends the waiting card back without describing it: the
        # server describes it itself, so "haan" through the answer tool still lands on that card.
        card = {k: v for k, v in d.items() if k not in ("categories", "staff_candidates", "target_candidates", "say", "say_lang", "tool")}
        with mock.patch("core.ai_brain.interpret", side_effect=AssertionError("no model must be asked")), \
                mock.patch("core.conversation._describe", wraps=__import__("core.conversation", fromlist=["_describe"])._describe) as described:
            r = self.md_client.post("/api/v1/assistant/live/tool/", {"name": "answer", "arguments": {"said": "haan", "yes": True},
                                                                      "pending": card, "awaiting": ""}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(described.called)
        self.assertEqual(described.call_args.args[0].get("action"), d["action"])
        # An unknown tool is refused; a bad argument blob is not.
        r = self.md_client.post("/api/v1/assistant/live/tool/", {"name": "launch_rockets", "arguments": {}}, format="json")
        self.assertEqual(r.status_code, 422)
        r = self.md_client.post("/api/v1/assistant/live/usage/", {"seconds": 95}, format="json")
        self.assertEqual(r.json()["seconds_today"], 95)
