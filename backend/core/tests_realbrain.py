"""Whole conversations with the REAL brain (Claude Haiku), checked on the database (Nov 2026).

The ordinary suite fakes the model, so it proves the server's side only. These scenarios let the real model read every
sentence and then look at what the server did: the rows that changed, the lines that were spoken. They run only on
demand, because each costs a few paise and takes seconds:

    set JARVIS_REAL_BRAIN_TESTS=1   (PowerShell: $env:JARVIS_REAL_BRAIN_TESTS = "1")
    python manage.py test core.tests_realbrain -v 2

Django's test runner builds and destroys its own database, so nothing here can touch real data. When a scenario fails,
the whole transcript is printed with the failure, and the fix gets a faked regression test in the ordinary suite."""
import os
import re
from datetime import time, timedelta
from unittest import skipUnless

from django.conf import settings
from django.test import override_settings

from core import services
from core import timeutils as tu
from core.models import Kind, PrivateTask, SharedTask, Status
from core.tests import BaseCase, _workday

ON = os.environ.get("JARVIS_REAL_BRAIN_TESTS") == "1" and bool(settings.ANTHROPIC_API_KEY)
_SIR = re.compile(r"\b(Sir|Ma'am|Madam)\b|सर|मैडम")
_FILLER = re.compile(r"^\s*(Ji|Okay|Ok|Theek hai|Thik hai|Haan)\s*,?\s*(Sir|Ma'am)\b", re.IGNORECASE)


@skipUnless(ON, "real-brain scenarios run only with JARVIS_REAL_BRAIN_TESTS=1 and an API key")
@override_settings(JARVIS_AI_ENABLED=True)
class RealBrainScenarios(BaseCase):
    """Each test is one conversation, spoken turn by turn to the voice endpoint (no card is ever sent back, so the
    server's own memory of the conversation is what carries the follow-ups)."""

    def setUp(self):
        super().setUp()
        self.nikki = services.team_add(self.md, {"name": "Nikki", "title": "Accountant", "username": "nikki", "password": "nikki1"})
        self.transcript = []

    def say(self, text: str) -> dict:
        r = self.md_client.post("/api/v1/assistant/turn/", {"text": text}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        self.transcript.append((text, d.get("show", ""), d.get("action", "")))
        return d

    def shown(self) -> str:
        return "\n".join(f"  > {t}\n  < [{a}] {s}" for t, s, a in self.transcript)

    def check(self, condition, message):
        self.assertTrue(condition, f"{message}\n{self.shown()}")

    def assert_brief(self, d: dict):
        """One address at most, never as a filler, never the same line twice in a row."""
        show = d.get("show", "")
        self.check(len(_SIR.findall(show)) <= 1, f"the address is used more than once: {show!r}")
        self.check(not _FILLER.search(show), f"a filler opens the line: {show!r}")
        if len(self.transcript) >= 2 and show:
            self.check(show != self.transcript[-2][1], f"the previous line was said again: {show!r}")

    # ---------------------------------------------------------------- the conversations

    def test_staff_then_nikki_then_her_tasks_then_her_second_task(self):
        day = _workday(2)
        titles = ["Reconcile the bank statement", "File the GST return", "Chase the Pune vendor invoice"]
        for title in titles:
            services.create_person_task(self.md, self.nikki, {"title": title, "due_date": day})
        tomorrow = tu.today_local() + timedelta(days=1)

        d = self.say("meri staff ki list dikhao")
        self.assert_brief(d)
        d = self.say("tell me about Nikki")
        self.assert_brief(d)
        self.check("Nikki" in d["show"], "the answer about Nikki does not name her")
        d = self.say("what tasks does she have?")
        self.assert_brief(d)
        self.check("which" not in d["show"].lower(), "JARVIS asked instead of answering about her")
        order = [i["title"] for i in services.last_view(self.md)["items"]]
        self.check(set(order) == set(titles), f"her list was not the one answered: {order}")
        d = self.say("uska doosra task kal kar do")
        self.assert_brief(d)
        second = SharedTask.objects.get(title=order[1], assignee_user=self.nikki)
        if d.get("action") != "saved":
            self.check("?" in d["show"], "neither saved nor asked")
            self.check("which" not in d["show"].lower(), "asked which one although the second was named")
            d = self.say("haan")
        self.check(SharedTask.objects.get(id=second.id).due_date == tomorrow, "her second task did not move to tomorrow")
        for other in (order[0], order[2]):
            self.check(SharedTask.objects.get(title=other, assignee_user=self.nikki).due_date == day, f"{other} moved too")

    def test_a_clash_then_which_meeting(self):
        day = _workday(1)
        services.create_private_task(self.md, {"title": "Meeting with Khanna – Loan Papers", "kind": Kind.MEETING,
                                               "due_date": day, "due_time": time(11, 0), "duration_minutes": 60})
        when = "kal" if day == tu.today_local() + timedelta(days=1) else day.strftime("%d %B")
        d = self.say(f"{when} 11 baje Sharma ji ke saath meeting rakho")
        self.assert_brief(d)
        self.check("Khanna" in d["show"], "the clash question does not name the meeting already there")
        d = self.say("kaun si meeting?")
        self.assert_brief(d)
        self.check("Khanna" in d["show"], "asked which meeting, JARVIS did not name it from the context")
        self.check("which" not in d["show"].lower() and "kaun" not in d["show"].lower(), "JARVIS asked back instead of answering")
        self.check(not PrivateTask.objects.filter(title__icontains="Sharma").exists(), "the new meeting was saved before a yes")

    def test_an_ambiguous_delete_is_clarified_once_and_hits_the_right_record(self):
        day = _workday(2)
        a = services.create_person_task(self.md, self.nikki, {"title": "Vendor payment follow-up", "due_date": day})
        b = services.create_person_task(self.md, self.nikki, {"title": "Vendor agreement review", "due_date": day})
        d = self.say("Nikki ka vendor wala task hata do")
        self.assert_brief(d)
        self.check("?" in d["show"], "two vendor tasks fit and JARVIS did not ask which")
        d = self.say("agreement wala")
        self.assert_brief(d)
        self.check("?" not in d["show"] or "undo" in d["show"].lower(), "JARVIS asked the same question again")
        self.check(SharedTask.objects.get(id=b.id).status == Status.CANCELLED, "the agreement task was not cancelled")
        self.check(SharedTask.objects.get(id=a.id).status == Status.OPEN, "the wrong task was touched")

    def test_a_long_hinglish_sentence_with_three_instructions(self):
        tomorrow = tu.today_local() + timedelta(days=1)
        d = self.say("Nikki ko bolo kal tak GST return file kar de, Priya ko bolo kal subah 11 baje Sharma ji ki meeting arrange kare, "
                     "aur mujhe parso 4 baje dentist ke liye yaad dilana")
        self.assert_brief(d)
        if d.get("action") not in ("saved", "answer"):
            d = self.say("haan")
        gst = SharedTask.objects.filter(assignee_user=self.nikki, title__icontains="GST").first()
        self.check(gst is not None and gst.due_date == tomorrow, "Nikki's GST task is missing or on the wrong day")
        self.check(SharedTask.objects.filter(assignee_user=self.pa, title__icontains="Sharma").exists(), "Priya's arrange task is missing")
        self.check(PrivateTask.objects.filter(owner=self.md, title__icontains="entist").exists(), "the dentist reminder is missing")
        self.check(not SharedTask.objects.filter(assignee_user=self.pa, title__icontains="GST").exists(), "GST went to the wrong person")

    def test_an_unfamiliar_way_of_asking_for_her_work(self):
        day = _workday(2)
        services.create_person_task(self.md, self.nikki, {"title": "Reconcile the bank statement", "due_date": day})
        d = self.say("Nikki ki plate pe abhi kya kya hai?")
        self.assert_brief(d)
        self.check("bank" in d["show"].lower() or "1" in d["show"], "her one task was not answered")

    def test_the_same_request_twice_is_done_once(self):
        tomorrow = tu.today_local() + timedelta(days=1)
        first = self.say("Nikki ko bolo kal tak bank reconciliation kar de")
        self.assert_brief(first)
        if first.get("action") not in ("saved", "answer"):
            self.say("haan")
        again = self.say("Nikki ko bolo kal tak bank reconciliation kar de")
        self.check(SharedTask.objects.filter(assignee_user=self.nikki, due_date=tomorrow).count() == 1, "the task was saved twice")
        self.check("Nikki has it" not in again["show"], "the second time was announced as a new save")
