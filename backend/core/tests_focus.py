"""Conversation focus (Nov 2026): the list JARVIS answered with is remembered with its items in the order shown and the
person it was about, so "the second one", "doosra wala", "uska doosra task" and "what tasks does she have?" work with
no card on the screen (the live voice line, the background listener); the answer to "which one?" is read by its place
or by its words, so the question is never asked twice; and a remembered place whose item has since closed points at
nothing, so the wrong record is never touched."""
from datetime import timedelta
from unittest import mock

from core import assistant, services
from core import timeutils as tu
from core.models import SharedTask, Status
from core.tests import BaseCase, _cmd, _workday


class OrdinalWordsTests(BaseCase):
    def test_places_in_english_hinglish_and_devanagari(self):
        cases = {
            "move her second task to Friday": 1, "the second one": 1, "doosra wala delete kar do": 1, "उसका दूसरा task kal kar do": 1,
            "pehla wala ho gaya": 0, "the first one": 0, "पहला वाला": 0, "teesra": 2, "chautha wala": 3, "5th one": 4,
            "aakhri wala hata do": -1, "the last one": -1, "आख़िरी वाला": -1, "number 3 kar do": 2, "3 number wala": 2,
        }
        for text, want in cases.items():
            self.assertEqual(assistant._ordinal_index(text), want, text)

    def test_words_that_are_not_a_place(self):
        for text in ("ye pehle karna hai", "last month ke tasks dikhao", "first and second dono hata do", "Pune wala task",
                     "secondary school", "kal 3 baje meeting", "nikki ke tasks"):
            self.assertIsNone(assistant._ordinal_index(text), text)


class FocusTests(BaseCase):
    """The MD asks about Nikki, then points at her tasks by place, pronoun and words."""

    def setUp(self):
        super().setUp()
        self.nikki = services.team_add(self.md, {"name": "Nikki", "title": "Accountant", "username": "nikki", "password": "nikki1"})
        day = _workday(2)
        for title in ("Reconcile the bank statement", "File the GST return", "Chase the Pune vendor invoice"):
            services.create_person_task(self.md, self.nikki, {"title": title, "due_date": day})
        self.friday = day + timedelta(days=(4 - day.weekday()) % 7 or 7)

    def interpret(self, text, cmd, **extra):
        with mock.patch("core.ai_brain.interpret", return_value=cmd) as fake:
            r = self.md_client.post("/api/v1/assistant/interpret/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.last_ctx = fake.call_args.args[1]
        return r.json()

    def turn(self, text, cmd):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = self.md_client.post("/api/v1/assistant/turn/", {"text": text}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def her_list(self, via="interpret", expect=3):
        cmd = _cmd(intent="query", view_kind="tasks", view_who="Nikki")
        d = self.interpret("Nikki ke tasks dikhao", cmd) if via == "interpret" else self.turn("Nikki ke tasks dikhao", cmd)
        rows = (d.get("view") or {}).get("rows") if via == "interpret" else None
        if rows is None:
            rows = [{"id": i["id"], "title": i["title"]} for i in services.last_view(self.md)["items"]]
        self.assertEqual(len(rows), expect)
        return rows

    def test_the_list_is_remembered_with_its_items_in_order_and_who_it_was_about(self):
        rows = self.her_list()
        last = services.last_view(self.md)
        self.assertEqual([i["id"] for i in last["items"]], [r["id"] for r in rows])
        self.assertEqual([i["title"] for i in last["items"]], [r["title"] for r in rows])
        self.assertEqual(last["about"], "Nikki")
        # The brain sees the same on the next sentence, so "the second one" has an id to point at.
        self.interpret("hmm", _cmd(intent="small_talk", spoken_reply="Ji Sir?"))
        self.assertEqual([i["id"] for i in self.last_ctx["last_list"]["items"]], [r["id"] for r in rows])
        self.assertEqual(self.last_ctx["last_list"]["about"], "Nikki")

    def test_she_means_the_person_answered_about_a_moment_ago(self):
        rows = self.her_list()
        d = self.interpret("what tasks does she have", _cmd(intent="query", view_kind="tasks"))
        self.assertEqual([r["id"] for r in d["view"]["rows"]], [r["id"] for r in rows])
        self.assertEqual(d["view"]["request"]["who"], "Nikki")
        # Without a pronoun the question stands on its own: "my tasks" is not hers.
        d = self.interpret("mere tasks", _cmd(intent="query", view_kind="tasks", view_who="me"))
        self.assertNotIn(rows[0]["id"], [r["id"] for r in d["view"]["rows"]])

    def test_her_second_task_moves_by_its_place_with_no_card_on_screen(self):
        rows = self.her_list(via="turn")  # the voice turn: no card is ever sent back
        d = self.turn("move her second task to Friday",
                      _cmd(intent="change_task", due_date=self.friday.isoformat(), changed_fields=["due_date"]))
        self.assertEqual(d["action"], "change_task")
        self.assertIn(rows[1]["title"], d["show"])
        self.assertIn(tu.fmt_day(self.friday).split(",")[0], d["show"])
        d = self.turn("haan", _cmd(intent="confirm_pending"))
        self.assertEqual(d["action"], "saved")
        self.assertEqual(SharedTask.objects.get(id=rows[1]["id"]).due_date, self.friday)
        for other in (rows[0], rows[2]):
            self.assertNotEqual(SharedTask.objects.get(id=other["id"]).due_date, self.friday)

    def test_doosra_wala_is_deleted_in_hindi_and_nothing_else(self):
        rows = self.her_list(via="turn")
        d = self.turn("doosra wala delete kar do", _cmd(intent="delete_task", reply_language="hi"))
        self.assertTrue(d["action"] in ("saved", "cancel_pending") or "हटा" in d["show"] or "Deleted" in d["show"], d)
        states = {r["title"]: SharedTask.objects.get(id=r["id"]).status for r in rows}
        self.assertEqual(states[rows[1]["title"]], Status.CANCELLED)
        self.assertEqual(states[rows[0]["title"]], Status.OPEN)
        self.assertEqual(states[rows[2]["title"]], Status.OPEN)

    def test_the_first_one_is_marked_done_by_its_place(self):
        rows = self.her_list()
        d = self.interpret("mark the first one done", _cmd(intent="complete_task"))
        self.assertEqual(d["action"], "complete")
        self.assertEqual(d["target"]["id"], rows[0]["id"])
        self.assertFalse(d["needs_clarification"])

    def test_a_place_whose_item_has_closed_points_at_nothing(self):
        rows = self.her_list(via="turn")
        SharedTask.objects.filter(id=rows[1]["id"]).update(status=Status.COMPLETED)  # finished meanwhile, by Nikki
        d = self.turn("doosra wala delete kar do", _cmd(intent="delete_task", reply_language="hi"))
        # JARVIS asks instead of deleting the item that now sits second among the open ones.
        self.assertTrue(d["show"].endswith("?"), d["show"])
        self.assertEqual(SharedTask.objects.filter(id__in=[r["id"] for r in rows], status=Status.CANCELLED).count(), 0)

    def test_the_answer_to_which_one_is_read_by_its_place(self):
        rows = self.her_list(via="turn")
        d = self.turn("delete kar do", _cmd(intent="delete_task"))
        self.assertIn("which one", d["show"].lower())
        d = self.turn("doosra", _cmd(intent="unknown"))  # the model made nothing of a one-word answer
        self.assertNotIn("which one", d["show"].lower())
        self.assertEqual(SharedTask.objects.get(id=rows[1]["id"]).status, Status.CANCELLED)
        self.assertEqual(SharedTask.objects.filter(id__in=[rows[0]["id"], rows[2]["id"]], status=Status.OPEN).count(), 2)

    def test_the_answer_to_which_one_is_read_by_its_words(self):
        rows = self.her_list(via="turn")
        d = self.turn("delete kar do", _cmd(intent="delete_task"))
        self.assertIn("which one", d["show"].lower())
        d = self.turn("Pune wala", _cmd(intent="unknown"))
        self.assertNotIn("which one", d["show"].lower())
        pune = next(r for r in rows if "Pune" in r["title"])  # by its words, wherever it sits in the list
        self.assertEqual(SharedTask.objects.get(id=pune["id"]).status, Status.CANCELLED, d)
        self.assertEqual(SharedTask.objects.filter(id__in=[r["id"] for r in rows if r is not pune], status=Status.OPEN).count(), 2)
        # "Nahi" to "which one?" cancels and touches nothing (no place, no words).
        rows = self.her_list(via="turn", expect=2)  # the two still open
        self.turn("delete kar do", _cmd(intent="delete_task"))
        d = self.turn("nahi rehne do", _cmd(intent="cancel_pending"))
        self.assertNotIn("Deleted", d["show"])
        self.assertEqual(SharedTask.objects.filter(id__in=[rows[0]["id"], rows[1]["id"]], status=Status.OPEN).count(), 2)

    def test_a_locked_phone_is_not_told_the_remembered_personal_items(self):
        d = self.interpret("mere personal tasks", _cmd(intent="query", view_kind="private"))
        self.assertTrue(d["view"]["rows"])
        self.assertTrue(services.last_view(self.md, include_private=True).get("items"))
        self.assertIsNone(services.last_view(self.md, include_private=False).get("items"))
