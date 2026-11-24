"""Brief, natural replies (Nov 2026): a confirmation states the fact without "Sir" in every line; the same sentence
heard twice within moments is done once and said once; "nahi" to "which one?" withdraws the question instead of leaving
it hanging; a card that waits for a yes always asks (a model line that calls it done is never read out); the live
voice line says nothing before a form and nothing of its own after the server's line."""
from datetime import timedelta
from unittest import mock

from core import ai_brain, livevoice, services
from core import timeutils as tu
from core.models import SharedTask, StaffMember, Status
from core.tests import BaseCase, _cmd, _workday


class ReplyTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.nikki = services.team_add(self.md, {"name": "Nikki", "title": "Accountant", "username": "nikki", "password": "nikki1"})
        self.tomorrow = (tu.today_local() + timedelta(days=1)).isoformat()

    def interpret(self, text, cmd, **extra):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = self.md_client.post("/api/v1/assistant/interpret/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def turn(self, text, cmd):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = self.md_client.post("/api/v1/assistant/turn/", {"text": text}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def give_nikki(self, title="Bank ka reconciliation"):
        return self.interpret(f"Nikki ko bolo kal tak {title.lower()} kar de",
                              _cmd(intent="assign_to_person", recipients=["Nikki"], staff_name="Nikki", title=title,
                                   due_date=self.tomorrow, spoken_reply="Ji Sir, kar dunga Sir.", done_reply="Ho gaya Sir."))

    def test_a_done_line_states_the_fact_without_the_address(self):
        d = self.give_nikki()
        self.assertTrue(d["booked"], d)
        self.assertTrue(d["spoken_reply"].startswith("Given to Nikki for tomorrow"), d["spoken_reply"])
        self.assertNotIn("Sir", d["spoken_reply"])
        self.assertNotIn("सर", d["reply_hi"])
        self.assertNotIn("Ji Sir", d["spoken_reply"])  # the model's own filler line is never what is said
        self.assertEqual(SharedTask.objects.filter(assignee_user=self.nikki).count(), 1)

    def test_the_same_sentence_twice_within_moments_is_done_once_and_said_once(self):
        first = self.give_nikki()
        self.assertTrue(first["spoken_reply"])
        again = self.give_nikki()
        self.assertEqual((again["spoken_reply"], again["speak_text"]), ("", ""), again)
        self.assertEqual(SharedTask.objects.filter(assignee_user=self.nikki).count(), 1)

    def test_said_again_after_a_while_it_is_told_it_is_already_done(self):
        self.give_nikki()
        with mock.patch("core.assistant.ECHO_SECONDS", -1):  # the echo window has passed
            again = self.give_nikki()
        self.assertEqual(again["spoken_reply"], "That's already done.")
        self.assertEqual(SharedTask.objects.filter(assignee_user=self.nikki).count(), 1)

    def test_nahi_to_which_one_withdraws_the_question(self):
        day = _workday(2)
        for title in ("Reconcile the bank statement", "File the GST return"):
            services.create_person_task(self.md, self.nikki, {"title": title, "due_date": day})
        self.turn("Nikki ke tasks dikhao", _cmd(intent="query", view_kind="tasks", view_who="Nikki"))
        d = self.turn("delete kar do", _cmd(intent="delete_task"))
        self.assertIn("which one", d["show"].lower())
        d = self.turn("nahi rehne do", _cmd(intent="cancel_pending"))
        self.assertEqual((d["action"], d["show"]), ("cancel_pending", "Okay, leaving it."), d)
        # The question is gone: a stray "doosra" afterwards deletes nothing.
        d = self.turn("doosra", _cmd(intent="unknown"))
        self.assertNotIn("Deleted", d["show"])
        self.assertEqual(SharedTask.objects.filter(assignee_user=self.nikki, status=Status.OPEN).count(), 2)

    def test_a_waiting_card_always_asks_never_says_done(self):
        staff = StaffMember.objects.filter(active=True).exclude(name="").first()
        self.assertIsNotNone(staff)
        d = self.interpret(f"{staff.name} ko bank ka kaam de do",
                           _cmd(intent="assign_to_staff", staff_name=staff.name, title="Bank Reconciliation",
                                spoken_reply="Kar diya Sir, de diya."))
        self.assertEqual(d["action"], "delegation", d)
        self.assertFalse(d.get("booked"))
        self.assertTrue(d["spoken_reply"].rstrip().endswith("?"), d["spoken_reply"])
        self.assertNotIn("Kar diya", d["spoken_reply"])

    def test_a_confirmed_card_is_reported_without_the_address(self):
        day = _workday(2)
        task = services.create_person_task(self.md, self.nikki, {"title": "File the GST return", "due_date": day})
        d = self.turn("GST wala done", _cmd(intent="complete_task", target_refs=[str(task.id)]))
        self.assertIn("mark", d["show"].lower())
        d = self.turn("haan", _cmd(intent="confirm_pending"))
        self.assertEqual((d["action"], d["show"]), ("saved", "Marked as done."), d)
        self.assertEqual(SharedTask.objects.get(id=task.id).status, Status.COMPLETED)

    def test_the_live_voice_line_says_nothing_of_its_own(self):
        text = livevoice.instructions_for(self.md, services.ai_context(self.md))
        self.assertIn("Before a tool call say NOTHING", text)
        self.assertNotIn("at most two words", text)
        self.assertIn("never twice in a row", text)
        self.assertIn("Never say the same thing twice", text)

    def test_the_brain_is_told_to_address_once_and_state_facts(self):
        prompt = ai_brain.COMMAND_SYSTEM_PROMPT
        self.assertIn("at most ONCE in a reply", prompt)
        self.assertIn("Never repeat what the previous turn already said", prompt)
        for filler in ("Theek hai Sir, 2 minute", "Ji Sir, PDF", "Done Sir, switching", "Ho gaya Sir, kal", "Done, Sir. Ramesh"):
            self.assertNotIn(filler, prompt, filler)
