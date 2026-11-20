"""Tasks as the heart of the app (Nov 2026): strangers in an instruction never block it, alerts are news that
expires while tasks and their history stay, the month report is counted from the history, and nobody reads another
person's work or personal list — by screen or by voice."""
import json
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from rest_framework.test import APIClient

from core import services
from core.models import Alert, Contact, Kind, PrivateTask, Priority, SharedTask, Status, TaskEvent, TaskRequest
from core.tests import BaseCase, _cmd, _workday

IST = dt_timezone(timedelta(hours=5, minutes=30))


def at(y, m, d, hh=10, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


class TaskCoreCase(BaseCase):
    def setUp(self):
        super().setUp()
        self.nikki = services.team_add(self.md, {"name": "Nikki", "title": "Accountant", "username": "nikki", "password": "nikki1"})
        self.nikki_client = APIClient()
        self.nikki_client.force_authenticate(self.nikki)

    def interpret(self, client, text, cmd, **extra):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = client.post("/api/v1/assistant/interpret/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def given(self, giver, to, title, created=None, **fields):
        """Work given by one person to another, with its GIVEN step at `created`."""
        t = SharedTask.objects.create(title=title, assignee_type=SharedTask.Assignee.USER, assignee_user=to,
                                      origin=SharedTask.Origin.GIVEN, created_by=giver, **fields)
        self.step(t, TaskEvent.Kind.GIVEN, giver, created or at(2026, 10, 1), to=to.id)
        if created:
            SharedTask.objects.filter(id=t.id).update(created_at=created)
        return t

    def step(self, task, kind, actor, when, **data):
        e = TaskEvent.objects.create(task=task, kind=kind, actor=actor, data=data)
        TaskEvent.objects.filter(id=e.id).update(at=when)
        if kind == TaskEvent.Kind.DONE:
            SharedTask.objects.filter(id=task.id).update(status=Status.COMPLETED, completed_at=when)
        elif kind == TaskEvent.Kind.REOPENED:
            SharedTask.objects.filter(id=task.id).update(status=Status.OPEN, completed_at=None)
        elif kind == TaskEvent.Kind.CANCELLED:
            SharedTask.objects.filter(id=task.id).update(status=Status.CANCELLED)
        return e


class StrangersInAnInstructionTests(TaskCoreCase):
    def test_a_name_inside_work_for_someone_else_is_never_questioned(self):
        # "Nikki ko bolo ki Aman ko bole mujhe call kare abhi": Aman is Nikki's business, not JARVIS's.
        d = self.interpret(self.md_client, "Nikki ko bolo ki Aman ko bole mujhe call kare abhi", _cmd(
            intent="assign_to_person", staff_name="Nikki", recipients=["Nikki"], title="Tell Aman to call Sir now",
            people=["Aman"], priority="HIGH", spoken_reply="Ji Sir, Nikki ko bol diya."))
        self.assertFalse(d["needs_clarification"], d)
        self.assertTrue(d["booked"])
        self.assertNotIn("don't know", d["spoken_reply"])
        task = SharedTask.objects.get(title="Tell Aman to call Sir now")
        self.assertEqual(task.assignee_user_id, self.nikki.id)
        self.assertEqual(Contact.objects.count(), 0)  # nothing invented about Aman either
        # The same for a message, and for the PA asking something of Sir with an outsider in it.
        d = self.interpret(self.md_client, "Nikki ko message bhejo ki Aman aayega", _cmd(
            intent="send_alert", staff_name="Nikki", recipients=["Nikki"], title="Aman is coming", people=["Aman"]))
        self.assertFalse(d["needs_clarification"], d)
        day = _workday()
        d = self.interpret(self.pa_client, "Sir ke liye Mehta ke saath meeting", _cmd(
            intent="request_for_md", title="Meeting with Mehta", kind="MEETING", due_date=day.isoformat(), due_time="15:00", people=["Mehta"]))
        self.assertFalse(d["needs_clarification"], d)
        self.assertNotIn("Mehta ji", json.dumps(d))  # Sir's private Mehta meeting is not hinted at to her

    def test_a_stranger_in_my_own_item_is_saved_then_mentioned_softly(self):
        day = _workday()
        aman = _cmd(intent="create_task", title="Meeting with Aman", kind="MEETING", due_date=day.isoformat(), due_time="17:00",
                    people=["Aman"], reply_language="hi", spoken_reply="Sir, Aman ke saath meeting 5 baje. Save kar doon?",
                    spoken_reply_hi="सर, अमन के साथ मीटिंग पाँच बजे। सेव कर दूँ?")
        before = PrivateTask.objects.filter(owner=self.md).count()
        first = self.interpret(self.md_client, "Aman ke saath 5 baje meeting book karo", aman)
        self.assertFalse(first["needs_clarification"], first)
        self.assertEqual(PrivateTask.objects.filter(owner=self.md).count(), before + 1)  # the intention comes first
        self.assertIn("I don't know Aman yet", first["spoken_reply"])
        self.assertIn("Aman को मैं अभी नहीं जानता", first["speak_text"])   # softly, in his language
        self.assertEqual(first["speak_lang"], "hi")
        self.assertTrue(any("Aman" in w for w in first["warnings"]))
        # He explains who Aman is, in passing: remembered; the model is told next time; the soft word stops.
        history = [{"role": "user", "text": "Aman ke saath 5 baje meeting book karo"}, {"role": "jarvis", "text": first["spoken_reply"]}]
        second = self.interpret(self.md_client, "bahar ka hai, hamara vendor",
                                _cmd(intent="unknown", new_contacts=[{"name": "aman", "note": "vendor"}]), history=history)
        self.assertEqual(second["contacts_saved"], ["Aman"])
        self.assertEqual([(c["name"], c["note"]) for c in services.list_contacts(self.md)], [("Aman", "vendor")])
        with mock.patch("core.ai_brain.interpret", return_value=aman) as fake:
            third = self.md_client.post("/api/v1/assistant/interpret/", {"text": "Aman ko kal call karna hai"}, format="json").json()
        self.assertNotIn("don't know", third["spoken_reply"])
        self.assertEqual(fake.call_args.args[1]["contacts"], [{"name": "Aman", "who": "vendor"}])
        # Sir's contact is his alone.
        self.assertEqual(services.list_contacts(self.pa), [])

    def test_carrying_on_without_answering_keeps_the_name_and_asks_no_more(self):
        day = _workday()
        aman = _cmd(intent="create_task", title="Meeting with Aman", kind="MEETING", due_date=day.isoformat(), due_time="17:00", people=["Aman"])
        first = self.interpret(self.md_client, "Aman ke saath meeting", aman)
        self.assertIn("I don't know Aman yet", first["spoken_reply"])
        history = [{"role": "user", "text": "Aman ke saath meeting"}, {"role": "jarvis", "text": first["spoken_reply"]}]
        second = self.interpret(self.md_client, "aur Aman ko kal call bhi karna hai", _cmd(
            intent="create_task", title="Call Aman", kind="CALL", due_date=day.isoformat(), due_time="11:00", people=["Aman"]), history=history)
        self.assertFalse(second["needs_clarification"])
        self.assertNotIn("don't know", second["spoken_reply"])       # never the same word twice
        # Known from now on: the name is in his own saved meeting (no contact needed for that).
        self.assertEqual(services.resolve_person(self.md, "Aman")["status"], "known")
        # A near miss still gets the nearest known name offered, softly.
        d = self.interpret(self.md_client, "Kavit ke saath meeting", _cmd(
            intent="create_task", title="Meeting with Kavit", kind="MEETING", due_date=day.isoformat(), due_time="15:00", people=["Kavit"]))
        self.assertFalse(d["needs_clarification"])
        self.assertIn("did you mean Kavita Singh", d["spoken_reply"])

    def test_the_person_who_must_do_the_work_is_still_asked_about(self):
        services.team_add(self.md, {"name": "Sunil Verma", "title": "Director", "username": "sunilv", "password": "sunil2"})
        services.team_add(self.md, {"name": "Sunil Jain", "title": "Farm Manager", "username": "sunilj", "password": "sunil3"})
        d = self.interpret(self.md_client, "Sunil ko bolo Aman ko call kare",
                           _cmd(intent="assign_to_person", staff_name="Sunil", recipients=["Sunil"], title="Call Aman", people=["Aman"]))
        self.assertIn("which Sunil", d["question"])      # the doer matters; Aman inside the instruction does not
        self.assertNotIn("Aman", d["question"])


class AlertsExpireTasksStayTests(TaskCoreCase):
    def test_weekly_cleanup_deletes_old_alerts_only(self):
        now = datetime.now(dt_timezone.utc)
        old = Alert.objects.create(recipient=self.pa, kind=Alert.Kind.INFO, priority=Priority.LOW, title="Old news")
        Alert.objects.filter(id=old.id).update(created_at=now - timedelta(days=8))
        fresh = Alert.objects.create(recipient=self.pa, kind=Alert.Kind.INFO, priority=Priority.LOW, title="Fresh news")
        # A task due in three months, with its history, keeps living through every cleanup until then.
        far = self.given(self.md, self.pa, "Renew the trade licence", created=now, due_kind="DATE", due_date=(now + timedelta(days=90)).date())
        self.assertEqual(services.cleanup_alerts(now), 1)                    # the old one goes…
        self.assertFalse(Alert.objects.filter(id=old.id).exists())
        self.assertTrue(Alert.objects.filter(id=fresh.id).exists())          # …the fresh one stays (not a week old)
        self.assertEqual(services.cleanup_alerts(now + timedelta(days=8)), 1)  # and goes once it is
        for week in range(2, 14):                                             # three months of weekly cleanups
            services.cleanup_alerts(now + timedelta(days=7 * week))
        self.assertTrue(SharedTask.objects.filter(id=far.id, status=Status.OPEN).exists())
        self.assertEqual(far.events.count(), 1)
        self.assertEqual(services.cleanup_alerts(now + timedelta(days=100)), 0)


class MonthReportTests(TaskCoreCase):
    """The six-task fixture: four unique tasks completed in November 2026, three still completed at month end."""

    def build_fixture(self):
        pa, md = self.pa, self.md
        a = self.given(md, pa, "A: October task finished in November", at(2026, 10, 15), due_kind="DATE", due_date=at(2026, 11, 12).date())
        self.step(a, TaskEvent.Kind.DONE, pa, at(2026, 11, 10), due="2026-11-12", due_kind="DATE")
        b = self.given(md, pa, "B: done, reopened, done again", at(2026, 11, 1), due_kind="DATE", due_date=at(2026, 11, 4).date())
        self.step(b, TaskEvent.Kind.DONE, pa, at(2026, 11, 5), due="2026-11-04", due_kind="DATE")       # a day late
        self.step(b, TaskEvent.Kind.REOPENED, md, at(2026, 11, 6))
        self.step(b, TaskEvent.Kind.DONE, pa, at(2026, 11, 20), due="2026-11-04", due_kind="DATE")
        c = self.given(md, pa, "C: done in November, reopened in December", at(2026, 11, 2))
        self.step(c, TaskEvent.Kind.DONE, pa, at(2026, 11, 12))
        self.step(c, TaskEvent.Kind.REOPENED, md, at(2026, 12, 3))
        d = self.given(md, pa, "D: done then reopened, still open at month end", at(2026, 11, 3))
        self.step(d, TaskEvent.Kind.DONE, pa, at(2026, 11, 8))
        self.step(d, TaskEvent.Kind.REOPENED, md, at(2026, 11, 25))
        e = self.given(md, pa, "E: November task finished in December", at(2026, 11, 20))
        self.step(e, TaskEvent.Kind.DONE, pa, at(2026, 12, 2))
        f = self.given(md, pa, "F: cancelled in November", at(2026, 11, 4))
        self.step(f, TaskEvent.Kind.CANCELLED, md, at(2026, 11, 15))
        return a, b, c, d, e, f

    def november(self, client, **extra):
        return self.interpret(client, "November mein maine kaunse tasks complete kiye", _cmd(
            intent="query", view_kind="done", view_who="me", view_from="2026-11-01", view_to="2026-11-30", **extra))

    def test_november_counts_once_per_task_and_survives_every_cleanup(self):
        a, b, c, d, e, f = self.build_fixture()
        # February 2027: every alert has long been cleaned up; the report comes from the tasks and their history.
        for week in range(16):
            services.cleanup_alerts(at(2026, 11, 1) + timedelta(days=7 * week))
        r = self.november(self.pa_client)
        view = r["view"]
        self.assertEqual(view["kind"], "done")
        self.assertEqual(view["title"], "Completed · November 2026")
        titles = [row["title"][:1] for row in view["rows"]]
        self.assertEqual(sorted(titles), ["A", "B", "C", "D"], view["rows"])
        per = view["period"]
        self.assertEqual((per["completed"], per["at_end"], per["cancelled"], per["late"]), (4, 3, 1, 1))
        rows = {row["title"][:1]: row for row in view["rows"]}
        self.assertEqual(rows["A"]["outcome"], "On time")
        self.assertIn("12 Nov", rows["A"]["due"])
        self.assertTrue(rows["B"]["outcome"].startswith("Late 1 d"))
        self.assertIn("5 Nov", rows["B"]["day"])                         # the first completion in the month
        self.assertIn("Reopened later", rows["C"]["outcome"])              # December's reopening does not erase November
        self.assertTrue(rows["C"]["still_done"])
        self.assertIn("Reopened before period end", rows["D"]["outcome"])
        self.assertFalse(rows["D"]["still_done"])
        self.assertIn("in November 2026 you completed 4 tasks, 3 on time", r["spoken_reply"])
        self.assertIn("1 cancelled", r["spoken_reply"])
        self.assertIn("नवंबर 2026 में आपने 4 काम पूरे किए", r["speak_text"] if r["speak_lang"] == "hi" else view["summary_hi"])
        # December, for the same person: C's reopening is not a completion; only E.
        r = self.interpret(self.pa_client, "December mein kya complete hua", _cmd(
            intent="query", view_kind="done", view_who="me", view_from="2026-12-01", view_to="2026-12-31"))
        self.assertEqual([row["title"][:1] for row in r["view"]["rows"]], ["E"])
        # The list can be downloaded like any other card.
        self.assertTrue(view["file_name"].startswith("jarvis-done-"))
        self.assertIn("A: October task", view["text"])

    def test_the_md_sees_a_persons_month_and_the_work_he_gave(self):
        self.build_fixture()
        r = self.interpret(self.md_client, "Priya ne November mein kya kya complete kiya", _cmd(
            intent="query", view_kind="done", view_who="Priya", view_from="2026-11-01", view_to="2026-11-30"))
        self.assertEqual(r["view"]["period"]["completed"], 4)
        self.assertIn("Priya completed 4 tasks", r["spoken_reply"])
        r = self.interpret(self.md_client, "jo kaam maine diye the unme se November mein kya complete hua", _cmd(
            intent="query", view_kind="done", view_from="2026-11-01", view_to="2026-11-30", view_from_whom="me"))
        self.assertEqual(r["view"]["period"]["completed"], 4)

    def test_without_a_period_the_card_is_still_the_last_14_days(self):
        r = self.interpret(self.md_client, "kya kya complete hua", _cmd(intent="query", view_kind="done"))
        self.assertEqual(r["view"]["title"], "Completed")
        self.assertIsNone(r["view"]["period"]["from"])


class EveryTaskIsEncryptedAtRestTests(TaskCoreCase):
    def test_work_task_text_is_unreadable_in_the_database_but_lookups_still_work(self):
        from django.db import connection
        task = SharedTask.objects.create(title="Pay the Pune vendor", notes="₹40,000 by cheque", description="Pune vendor ko 40 hazaar dena hai",
                                         announcement="Pune vendor payment", assignee_type=SharedTask.Assignee.USER, assignee_user=self.pa,
                                         origin=SharedTask.Origin.GIVEN, created_by=self.md)
        with connection.cursor() as cur:
            cur.execute("SELECT title, notes, description, announcement FROM core_sharedtask WHERE id = %s", [str(task.id)])
            row = cur.fetchone()
        self.assertTrue(row[0].startswith("enc2:"), row[0][:12])        # the title: deterministic form
        self.assertTrue(row[1].startswith("enc1:"), row[1][:12])        # notes: randomised
        for cell in row:
            self.assertNotIn("Pune", cell or "")
            self.assertNotIn("40", cell or "")
        # Reading back and exact lookups by title are unchanged.
        self.assertEqual(SharedTask.objects.get(id=task.id).title, "Pay the Pune vendor")
        self.assertTrue(SharedTask.objects.filter(title="Pay the Pune vendor").exists())
        self.assertFalse(SharedTask.objects.filter(title="Pay the Pune vendors").exists())
        # The same title twice is stored the same way (that, and only that, is what the title form gives away).
        twin = SharedTask.objects.create(title="Pay the Pune vendor", assignee_type=SharedTask.Assignee.USER, assignee_user=self.pa,
                                         origin=SharedTask.Origin.GIVEN, created_by=self.md)
        with connection.cursor() as cur:
            cur.execute("SELECT title FROM core_sharedtask WHERE id IN (%s, %s)", [str(task.id), str(twin.id)])
            titles = [r[0] for r in cur.fetchall()]
        self.assertEqual(titles[0], titles[1])
        # The API still shows the words to the people allowed to see them.
        self.assertEqual(self.pa_client.get(f"/api/v1/shared-tasks/{task.id}/").json()["title"], "Pay the Pune vendor")


class NobodyReadsAnotherPersonsWorkTests(TaskCoreCase):
    def setUp(self):
        super().setUp()
        self.pa_own = SharedTask.objects.create(title="Priya's own confidential errand", assignee_type=SharedTask.Assignee.USER,
                                                assignee_user=self.pa, origin=SharedTask.Origin.SELF, created_by=self.pa)
        self.pa_personal = services.create_private_task(self.pa, {"title": "Priya personal dentist", "kind": Kind.TASK, "priority": Priority.NORMAL})
        self.pa_done = self.given(self.md, self.pa, "Priya's finished report for Sir")
        self.step(self.pa_done, TaskEvent.Kind.DONE, self.pa, datetime.now(dt_timezone.utc) - timedelta(days=1))

    def assert_nothing_of_priya(self, payload):
        body = json.dumps(payload)
        for secret in ("confidential errand", "personal dentist", "finished report for Sir"):
            self.assertNotIn(secret, body)

    def test_by_voice_nikki_gets_none_of_priyas_or_sirs_items(self):
        asks = [
            ("Sir ke private tasks ki list do", dict(view_kind="private", view_who="sir")),
            ("Priya ke tasks batao", dict(view_kind="tasks", view_who="Priya")),
            ("Priya ne kya complete kiya", dict(view_kind="done", view_who="Priya")),
            ("Priya ka November", dict(view_kind="done", view_who="Priya", view_from="2026-11-01", view_to="2026-11-30")),
            ("Priya ke paas kya hai", dict(view_kind="person", view_who="Priya")),
            ("sab ka kaam dikhao", dict(view_kind="tasks", view_who="all")),
            ("Priya ki history", dict(view_kind="history", view_who="Priya")),
        ]
        for text, fields in asks:
            with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="query", **fields)):
                r = self.nikki_client.post("/api/v1/assistant/interpret/", {"text": text}, format="json")
            self.assertEqual(r.status_code, 200, text)
            self.assert_nothing_of_priya(r.json())
            self.assert_no_secret(r)

    def test_by_screen_nikki_gets_none_of_priyas_items(self):
        for url in ("/api/v1/views/tasks/?who=Priya", "/api/v1/views/tasks/?who=all", "/api/v1/views/tasks/?who=Priya&filter=done",
                    "/api/v1/today/", "/api/v1/shared-tasks/", "/api/v1/private-tasks/"):
            r = self.nikki_client.get(url)
            self.assertIn(r.status_code, (200, 404), url)
            if r.status_code == 200:
                self.assert_nothing_of_priya(r.json())
        self.assertEqual(self.nikki_client.get(f"/api/v1/shared-tasks/{self.pa_own.id}/").status_code, 404)
        self.assertEqual(self.nikki_client.get(f"/api/v1/shared-tasks/{self.pa_done.id}/history/").status_code, 404)
        self.assertEqual(self.nikki_client.get(f"/api/v1/private-tasks/{self.pa_personal.id}/").status_code, 404)

    def test_the_md_sees_work_but_never_a_personal_item(self):
        r = self.interpret(self.md_client, "Priya ke tasks", _cmd(intent="query", view_kind="tasks", view_who="Priya"))
        body = json.dumps(r)
        self.assertIn("confidential errand", body)            # her work: the MD sees all work
        self.assertNotIn("personal dentist", body)            # her personal item: nobody's but hers
        self.assertEqual(self.md_client.get(f"/api/v1/private-tasks/{self.pa_personal.id}/").status_code, 404)


class WorkForTheMdTests(TaskCoreCase):
    """Work for the MD from anyone else (Nov 2026): straight onto his list like work for anyone, ringing until he
    sees it; nothing waits for an acceptance, and he can decline it (the giver is told)."""

    def give_sir(self, client, title="Review the vendor report"):
        d = self.interpret(client, f"Sir ko bolo {title}", _cmd(
            intent="assign_to_person", staff_name="Sir", recipients=["Sir"], title=title, priority="HIGH",
            spoken_reply=f"Ji, Sir ko de diya: {title}."))
        self.assertTrue(d["booked"], d["spoken_reply"])
        return SharedTask.objects.get(title=title)

    def test_a_task_given_to_sir_is_on_his_list_at_once_and_rings(self):
        task = self.give_sir(self.nikki_client)
        self.assertEqual((task.assignee_type, task.assignee_user, task.origin, task.created_by), ("MD", self.md, "GIVEN", self.nikki))
        self.assertFalse(TaskRequest.objects.exists())                      # nothing waits for an acceptance
        ring = Alert.objects.get(recipient=self.md, kind="TASK_ASSIGNED", ref_id=str(task.id))
        self.assertTrue(ring.ring)                                          # rings on his phone until he taps Seen
        self.assertEqual(ring.sender, self.nikki)
        # His day shows it (given today, no date: today's work), and "my tasks" by voice names it.
        today = self.md_client.get("/api/v1/today/").json()
        self.assertIn(task.title, json.dumps(today["today"]))
        r = self.interpret(self.md_client, "aaj mere kya tasks hain", _cmd(intent="query", view_kind="today", view_who="me"))
        self.assertIn(task.title, json.dumps(r))
        # Nikki sees it under what she gave, and under Sir's tasks from her; Priya (who runs his diary) sees it too.
        mine = self.nikki_client.get("/api/v1/today/").json()
        self.assertIn(task.title, json.dumps(mine["given_tasks"]))
        self.assertIn(task.title, json.dumps(mine["md_tasks"]))
        self.assertIn(task.title, json.dumps(self.pa_client.get("/api/v1/today/").json()["md_tasks"]))
        self.assert_no_secret(self.nikki_client.get("/api/v1/today/"))

    def test_what_nikki_puts_on_sirs_list_never_sits_in_her_own_day(self):
        # A task, a call and a meeting for Sir, from Nikki: on his list, in her "given by you", never in her Today
        # (she is not the one doing them). The PA, who runs his diary, still sees his meetings coming.
        from core import timeutils as tu
        day = tu.today_local().isoformat()
        for title, kind, at in (("Sign the lease", "TASK", "16:00"), ("Call the auditor", "CALL", "17:00"), ("Vendor review meeting", "MEETING", "18:00")):
            d = self.interpret(self.nikki_client, f"Sir ko bolo aaj {at[:2]} baje {title}", _cmd(
                intent="assign_to_person", staff_name="Sir", recipients=["Sir"], title=title, kind=kind, due_date=day, due_time=at,
                spoken_reply="Ji."))
            self.assertTrue(d.get("booked"), (title, d["spoken_reply"]))
        hers = self.nikki_client.get("/api/v1/today/").json()
        own_day = json.dumps(hers["today"] + hers["overdue"] + hers["upcoming"] + hers["anytime"])
        for title in ("Sign the lease", "Call the auditor", "Vendor review meeting"):
            self.assertNotIn(title, own_day, title)
            self.assertIn(title, json.dumps(hers["given_tasks"]), title)
        his = self.md_client.get("/api/v1/today/").json()
        self.assertIn("Vendor review meeting", json.dumps(his["today"]))
        self.assertIn("Call the auditor", json.dumps(his["today"]))
        pas = self.pa_client.get("/api/v1/today/").json()
        self.assertIn("Vendor review meeting", json.dumps(pas["today"]))   # the PA runs his diary
        self.assertNotIn("Sign the lease", json.dumps(pas["today"]))       # but his plain tasks are not her day
        # Kinds stay what they are: a meeting on his list is a meeting, with its reminder lead, not a task.
        meeting = SharedTask.objects.get(title="Vendor review meeting")
        self.assertEqual((meeting.kind, meeting.assignee_type), ("MEETING", "MD"))
        self.assertIsNotNone(meeting.due_at)

    def test_sir_declines_and_the_giver_is_told(self):
        task = self.give_sir(self.nikki_client)
        r = self.md_client.patch(f"/api/v1/shared-tasks/{task.id}/", {"expected_version": task.version, "status": "CANCELLED"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        task.refresh_from_db()
        self.assertEqual(task.status, Status.CANCELLED)
        self.assertIsNotNone(task.seen_at)                                  # touching it is seeing it
        self.assertTrue(TaskEvent.objects.filter(task=task, kind=TaskEvent.Kind.SEEN, actor=self.md).exists())
        note = Alert.objects.get(recipient=self.nikki, ref_id=str(task.id), kind="STAFF_UPDATE")
        self.assertEqual(note.title, "Sir declined a task")
        self.assertIn("Sir has declined: Review the vendor report", note.spoken_text)
        # The ring on his phone is over.
        ring = Alert.objects.get(recipient=self.md, kind="TASK_ASSIGNED", ref_id=str(task.id))
        self.assertIsNotNone(ring.seen_at)
        self.assertIsNotNone(ring.acknowledged_at)

    def test_sir_finishes_it_and_the_giver_is_told(self):
        task = self.give_sir(self.nikki_client, "Sign the lease")
        r = self.md_client.post(f"/api/v1/shared-tasks/{task.id}/complete/", {"expected_version": task.version}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        task.refresh_from_db()
        self.assertEqual(task.status, Status.COMPLETED)
        note = Alert.objects.get(recipient=self.nikki, ref_id=str(task.id), kind="STAFF_UPDATE")
        self.assertEqual(note.title, "Sir completed a task")
        self.assertIn("Sir has completed: Sign the lease", note.spoken_text)
        # Moving it to another day tells the giver too; his own work (nobody gave it) tells nobody.
        other = self.give_sir(self.nikki_client, "Call the auditor")
        tomorrow = (datetime.now(IST) + timedelta(days=1)).date().isoformat()
        r = self.md_client.patch(f"/api/v1/shared-tasks/{other.id}/", {"expected_version": other.version, "due_date": tomorrow}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(Alert.objects.filter(recipient=self.nikki, ref_id=str(other.id), title="Sir moved a task").exists())

    def test_a_request_still_waiting_is_moved_onto_his_list(self):
        req = services.create_md_request(self.pa, {"title": "Approve the Pune budget", "kind": Kind.TASK, "priority": Priority.NORMAL})
        self.assertTrue(Alert.objects.filter(recipient=self.md, kind="NEW_REQUEST", ref_id=str(req.id), acknowledged_at__isnull=True).exists())
        self.assertEqual(services.settle_pending_requests(), 1)
        req.refresh_from_db()
        self.assertEqual(req.state, TaskRequest.State.ACCEPTED)
        task = req.resulting_task
        self.assertEqual((task.assignee_type, task.assignee_user, task.created_by, task.origin), ("MD", self.md, self.pa, "GIVEN"))
        self.assertTrue(Alert.objects.filter(recipient=self.md, kind="TASK_ASSIGNED", ref_id=str(task.id), ring=True).exists())
        self.assertFalse(Alert.objects.filter(recipient=self.md, kind="NEW_REQUEST", ref_id=str(req.id), acknowledged_at__isnull=True).exists())
        self.assertEqual(services.settle_pending_requests(), 0)             # nothing left the second time

    def test_the_home_screen_carries_the_latest_messages(self):
        services.send_message_to(self.pa, [self.md], "Vendor is here, Sir")
        services.send_message_to(self.pa, [self.nikki], "Nikki, GST file bhej do")  # between two others: not Sir's
        his = services.dashboard(self.md)
        self.assertEqual([m["text"] for m in his["messages"]], ["Vendor is here, Sir"])
        self.assertEqual((his["messages"][0]["box"], his["messages"][0]["who"], his["messages"][0]["seen"]), ("inbox", "Priya", False))
        self.assertEqual(his["messages_unseen"], 1)
        hers = services.dashboard(self.pa)
        self.assertEqual([m["box"] for m in hers["messages"]], ["sent", "sent"])
        self.assertEqual((hers["messages"][1]["who"], hers["messages"][1]["seen"], hers["messages"][1]["to_count"]), ("Sir", False, 1))
        # He taps Seen: his copy says so, hers says seen too, and nothing is unread for him any more.
        alert = Alert.objects.get(recipient=self.md, ref_type="message")
        services.mark_seen(self.md, alert)
        self.assertTrue(services.dashboard(self.md)["messages"][0]["seen"])
        self.assertEqual(services.dashboard(self.md)["messages_unseen"], 0)
        self.assertTrue(services.dashboard(self.pa)["messages"][1]["seen"])
