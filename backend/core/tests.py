"""Release-blocking tests: privacy boundary, approval flow, idempotency, parser."""
import json
from datetime import datetime, time, timedelta

from django.db import IntegrityError, connection
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

import hashlib
import hmac
from unittest import mock

from core import hinglish_parser, services
from core import timeutils as tu
from core.models import (
    Alert, EventInbox, IntegrationCommand, Kind, Priority, PrivateTask, SharedTask, StaffMember, TaskPanelLink,
    TaskRequest, User,
)
from core.taskpanel import signing, sync as tp_sync
from core.taskpanel.client import MockTaskPanel, TaskPanelError

SECRET = "SECRET-Mehta-Divorce-Lawyer"


@override_settings(JARVIS_AI_ENABLED=False, LARAVEL_MODE="mock")
class BaseCase(TestCase):
    def setUp(self):
        self.md = User.objects.create_user("md", password="md-pass-12345", role=User.Role.MD, display_name="Sir")
        self.pa = User.objects.create_user("pa", password="pa-pass-12345", role=User.Role.PA, display_name="Priya")
        TaskPanelLink.objects.create(user=self.md, actor_key="staff_001", grant="mock-grant-md-0123456789abcdef")
        TaskPanelLink.objects.create(user=self.pa, actor_key="staff_002", grant="mock-grant-pa-0123456789abcdef")
        services.sync_staff(self.md)
        now = tu.now_local() + timedelta(hours=1)
        if now.date() != tu.today_local():
            # Late in the evening "in an hour" is tomorrow; the fixture's meeting must stay today (and ahead of now).
            now = tu.now_local().replace(hour=23, minute=58, second=0, microsecond=0)
        self.private = services.create_private_task(self.md, {
            "title": SECRET, "notes": SECRET + " notes", "kind": Kind.MEETING, "priority": Priority.URGENT,
            "due_date": now.date(), "due_time": now.time().replace(second=0, microsecond=0), "duration_minutes": 30,
        })
        self.md_client = APIClient()
        self.md_client.force_authenticate(self.md)
        self.pa_client = APIClient()
        self.pa_client.force_authenticate(self.pa)

    def assert_no_secret(self, response):
        body = json.dumps(response.json()) if response.content else ""
        self.assertNotIn(SECRET, body)
        self.assertNotIn(str(self.private.id), body)


class PrivacyBoundaryTests(BaseCase):
    def test_pa_cannot_list_or_read_private_tasks(self):
        self.assertEqual(self.pa_client.get("/api/v1/private-tasks/").status_code, 403)
        r = self.pa_client.get(f"/api/v1/private-tasks/{self.private.id}/")
        self.assertEqual(r.status_code, 403)
        self.assert_no_secret(r)
        r = self.pa_client.patch(f"/api/v1/private-tasks/{self.private.id}/", {"expected_version": 1, "title": "x"},
                                 format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(PrivateTask.objects.get(id=self.private.id).title, SECRET)

    def test_pa_cannot_create_private_task(self):
        r = self.pa_client.post("/api/v1/private-tasks/", {"title": "x"}, format="json", HTTP_IDEMPOTENCY_KEY="k1")
        self.assertEqual(r.status_code, 403)

    def test_pa_endpoints_never_leak_private_content(self):
        for url in ["/api/v1/today/", "/api/v1/busy/", "/api/v1/reminders/", "/api/v1/alerts/",
                    "/api/v1/requests/", "/api/v1/delegations/", "/api/v1/me/", "/api/v1/health/",
                    "/api/v1/assistant/briefing/"]:
            r = self.pa_client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assert_no_secret(r)

    def test_pa_today_has_no_private_counts(self):
        data = self.pa_client.get("/api/v1/today/").json()
        all_items = data["overdue"] + data["today"] + data["upcoming"] + data["anytime"]
        self.assertTrue(all(i["scope"] == "shared" for i in all_items))
        self.assertNotIn("private", json.dumps(data).lower().replace("is_private", ""))

    def test_pa_sees_busy_time_without_title(self):
        data = self.pa_client.get("/api/v1/today/").json()
        self.assertEqual(len(data["md_busy_today"]), 1)
        self.assertNotIn("title", data["md_busy_today"][0])
        self.assertTrue(data["md_busy_today"][0]["label"].startswith("Busy"))

    def test_pa_assistant_context_excludes_private(self):
        ctx = json.dumps(services.ai_context(self.pa), default=str)
        self.assertNotIn(SECRET, ctx)
        r = self.pa_client.post("/api/v1/assistant/interpret/", {"text": "what does MD have today"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assert_no_secret(r)

    def test_md_context_includes_private(self):
        self.assertIn(SECRET, json.dumps(services.ai_context(self.md), default=str))

    def test_private_content_encrypted_at_rest(self):
        with connection.cursor() as cur:
            cur.execute("SELECT title, notes FROM core_privatetask WHERE id = %s", [self.private.id.hex])
            row = cur.fetchone()
            if row is None:  # postgres stores uuid natively
                cur.execute("SELECT title, notes FROM core_privatetask WHERE id = %s", [str(self.private.id)])
                row = cur.fetchone()
        self.assertTrue(row[0].startswith("enc1:"))
        self.assertNotIn("Mehta", row[0] + row[1])

    def test_single_md_only(self):
        with self.assertRaises(IntegrityError):
            User.objects.create_user("md2", password="x-pass-12345", role=User.Role.MD)


class RequestFlowTests(BaseCase):
    def _request(self, key="req-1", **extra):
        body = {"title": "Sign Indore vendor agreement", "priority": "HIGH",
                "due_date": (tu.today_local() + timedelta(days=1)).isoformat(), "due_time": "12:00", **extra}
        return self.pa_client.post("/api/v1/requests/", body, format="json", HTTP_IDEMPOTENCY_KEY=key)

    def test_pa_request_needs_md_acceptance(self):
        r = self._request()
        self.assertEqual(r.status_code, 201)
        self.assertEqual(SharedTask.objects.filter(assignee_type="MD").count(), 0)
        alerts = self.md_client.get("/api/v1/alerts/").json()["results"]
        self.assertEqual(alerts[0]["kind"], "NEW_REQUEST")
        self.assertIn("Priya", alerts[0]["spoken_text"])
        req_id = r.json()["id"]
        # PA cannot accept her own request
        self.assertEqual(self.pa_client.post(f"/api/v1/requests/{req_id}/accept/", HTTP_IDEMPOTENCY_KEY="a").status_code, 403)
        r = self.md_client.post(f"/api/v1/requests/{req_id}/accept/", HTTP_IDEMPOTENCY_KEY="acc-1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(SharedTask.objects.filter(assignee_type="MD").count(), 1)
        # Accepting twice is rejected
        r = self.md_client.post(f"/api/v1/requests/{req_id}/accept/", HTTP_IDEMPOTENCY_KEY="acc-2")
        self.assertEqual(r.status_code, 409)

    def test_pa_edit_becomes_proposal_and_stale_proposal_conflicts(self):
        req_id = self._request().json()["id"]
        self.md_client.post(f"/api/v1/requests/{req_id}/accept/", HTTP_IDEMPOTENCY_KEY="acc-1")
        task = SharedTask.objects.get(assignee_type="MD")
        r = self.pa_client.patch(f"/api/v1/shared-tasks/{task.id}/", {"expected_version": 1, "priority": "URGENT"},
                                 format="json")
        self.assertEqual(r.status_code, 202)
        task.refresh_from_db()
        self.assertEqual(task.priority, "HIGH")  # unchanged until MD accepts
        change_id = r.json()["request"]["id"]
        # MD changes the task directly first -> PA proposal is now stale
        self.md_client.patch(f"/api/v1/shared-tasks/{task.id}/", {"expected_version": 1, "title": "Sign agreement"},
                             format="json")
        r = self.md_client.post(f"/api/v1/requests/{change_id}/accept/", HTTP_IDEMPOTENCY_KEY="acc-3")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(TaskRequest.objects.get(id=change_id).state, "CONFLICT")
        task.refresh_from_db()
        self.assertEqual(task.priority, "HIGH")

    def test_pa_cannot_complete_md_task_directly(self):
        req_id = self._request().json()["id"]
        self.md_client.post(f"/api/v1/requests/{req_id}/accept/", HTTP_IDEMPOTENCY_KEY="acc-1")
        task = SharedTask.objects.get(assignee_type="MD")
        r = self.pa_client.post(f"/api/v1/shared-tasks/{task.id}/complete/", {"expected_version": 1}, format="json")
        self.assertEqual(r.status_code, 202)
        task.refresh_from_db()
        self.assertEqual(task.status, "OPEN")


class DelegationAndIdempotencyTests(BaseCase):
    def test_delegation_is_idempotent(self):
        staff = StaffMember.objects.get(name="Ramesh Kumar")
        body = {"title": "GST reconciliation", "staff_id": staff.id, "priority": "HIGH"}
        r1 = self.pa_client.post("/api/v1/delegations/", body, format="json", HTTP_IDEMPOTENCY_KEY="dlg-1")
        r2 = self.pa_client.post("/api/v1/delegations/", body, format="json", HTTP_IDEMPOTENCY_KEY="dlg-1")
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        self.assertEqual(r2["Idempotent-Replay"], "true")
        self.assertEqual(SharedTask.objects.filter(assignee_type="STAFF").count(), 1)
        self.assertEqual(IntegrationCommand.objects.count(), 1)
        self.assertEqual(r1.json()["result"], "assigned")
        self.assertEqual(r1.json()["sync_state"], "SYNCED")
        self.assertTrue(r1.json()["remote_task_id"].startswith("T-"))
        r3 = self.pa_client.post("/api/v1/delegations/", {**body, "title": "other"}, format="json",
                                 HTTP_IDEMPOTENCY_KEY="dlg-1")
        self.assertEqual(r3.status_code, 409)

    def test_missing_idempotency_key_rejected(self):
        r = self.md_client.post("/api/v1/private-tasks/", {"title": "x"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_version_conflict(self):
        r = self.md_client.patch(f"/api/v1/private-tasks/{self.private.id}/", {"expected_version": 9, "title": "y"},
                                 format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["current_version"], 1)

    def test_reschedule_keeps_original_deadline_and_retires_old_reminders(self):
        before = {r["key"] for r in self.md_client.get("/api/v1/reminders/").json()["reminders"]}
        new_day = (tu.today_local() + timedelta(days=1)).isoformat()
        r = self.md_client.patch(f"/api/v1/private-tasks/{self.private.id}/",
                                 {"expected_version": 1, "due_date": new_day, "due_time": "15:00"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(datetime.fromisoformat(r.json()["original_due_at"]), self.private.due_at)
        self.assertNotEqual(datetime.fromisoformat(r.json()["due_at"]), self.private.due_at)
        after = {r["key"] for r in self.md_client.get("/api/v1/reminders/").json()["reminders"]}
        self.assertFalse(before & after)

    def test_ambiguous_first_name_asks_which_one(self):
        r = self.md_client.post("/api/v1/assistant/interpret/", {"text": "Ramesh ko bolo kal tak GST file bhejo"},
                                format="json")
        draft = r.json()
        self.assertEqual(draft["action"], "delegation")
        self.assertTrue(draft["needs_clarification"])
        self.assertEqual(len(draft["staff_candidates"]), 2)
        self.assertIsNone(draft["staff"])


class ParserTests(TestCase):
    def test_hinglish_meeting(self):
        r = hinglish_parser.parse("kal subah 11 baje Sharma franchise ke saath meeting, urgent")
        self.assertEqual(r["due_date"], (tu.today_local() + timedelta(days=1)).isoformat())
        self.assertEqual(r["due_time"], "11:00")
        self.assertEqual(r["kind"], "MEETING")
        self.assertEqual(r["priority"], "URGENT")
        self.assertEqual(r["title"], "Sharma franchise ke saath meeting")

    def test_assign_to_staff(self):
        r = hinglish_parser.parse("Ramesh Kumar ko bolo parso tak GST file submit kare, high priority",
                                  staff_names=["Ramesh Kumar", "Ramesh Yadav"])
        self.assertEqual(r["intent"], "assign_to_staff")
        self.assertEqual(r["staff_name"].lower(), "ramesh kumar")
        self.assertEqual(r["due_date"], (tu.today_local() + timedelta(days=2)).isoformat())
        self.assertEqual(r["priority"], "HIGH")
        self.assertIn("GST file submit kare", r["title"])

    def test_saade_and_shaam(self):
        r = hinglish_parser.parse("parso shaam saade 5 baje bank manager ko call karna")
        self.assertEqual(r["due_time"], "17:30")
        self.assertEqual(r["kind"], "CALL")

    def test_amount_is_not_a_time(self):
        r = hinglish_parser.parse("kal pay 2.40 lakh to vendor")
        self.assertEqual(r["due_time"], "")
        self.assertEqual(r["kind"], "PAYMENT")


class PaTaskTests(BaseCase):
    """MD gives the PA tasks directly; the PA updates status; the MD is told."""

    def _create(self, **extra):
        when = tu.now_local() + timedelta(hours=2)
        body = {"title": "Book flights for Pune visit", "priority": "HIGH", "due_date": when.date().isoformat(),
                "due_time": when.strftime("%H:%M"), "description": "Priya ko bolo Pune ke flights book kare", **extra}
        return self.md_client.post("/api/v1/pa-tasks/", body, format="json", HTTP_IDEMPOTENCY_KEY=str(extra) + "pa-1")

    def test_md_assigns_and_pa_works_it_through(self):
        r = self._create()
        self.assertEqual(r.status_code, 201)
        task_id, version = r.json()["id"], r.json()["version"]
        self.assertEqual(IntegrationCommand.objects.count(), 0)  # stays inside JARVIS, never TaskPanel
        # PA sees it as her own task and is told
        today = self.pa_client.get("/api/v1/today/").json()
        mine = today["overdue"] + today["today"] + today["upcoming"] + today["anytime"]
        self.assertEqual([i["id"] for i in mine], [task_id])
        alert = self.pa_client.get("/api/v1/alerts/").json()["results"][0]
        self.assertEqual(alert["kind"], "TASK_ASSIGNED")
        self.assertIn("Sir has given you", alert["spoken_text"])
        # PA reminders include it
        keys = [x["key"] for x in self.pa_client.get("/api/v1/reminders/").json()["reminders"]]
        self.assertTrue(any(task_id in k for k in keys))
        # Working
        r = self.pa_client.post(f"/api/v1/pa-tasks/{task_id}/status/", {"status": "IN_PROGRESS", "expected_version": version},
                                format="json")
        self.assertEqual(r.json()["status_label"], "Working")
        # Hold needs a reason
        r = self.pa_client.post(f"/api/v1/pa-tasks/{task_id}/status/",
                                {"status": "BLOCKED", "expected_version": version + 1}, format="json")
        self.assertEqual(r.status_code, 422)
        r = self.pa_client.post(f"/api/v1/pa-tasks/{task_id}/status/",
                                {"status": "BLOCKED", "expected_version": version + 1, "note": "Waiting for dates"},
                                format="json")
        self.assertEqual(r.json()["status_label"], "Hold")
        # Done via the normal complete endpoint (used by reminder "Done" buttons)
        r = self.pa_client.post(f"/api/v1/shared-tasks/{task_id}/complete/", {"expected_version": version + 2},
                                format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status_label"], "Completed")
        spoken = list(Alert.objects.filter(recipient=self.md).values_list("spoken_text", flat=True))
        self.assertTrue(any("Waiting for dates" in s for s in spoken))
        self.assertTrue(any("Priya has completed: Book flights" in s for s in spoken))
        # MD sees progress
        md_view = self.md_client.get("/api/v1/today/").json()["pa_tasks"]
        self.assertEqual(md_view[0]["status_label"], "Completed")

    def test_pa_cannot_create_or_edit_pa_tasks(self):
        r = self.pa_client.post("/api/v1/pa-tasks/", {"title": "x"}, format="json", HTTP_IDEMPOTENCY_KEY="pa-x")
        self.assertEqual(r.status_code, 403)
        task_id = self._create().json()["id"]
        r = self.pa_client.patch(f"/api/v1/shared-tasks/{task_id}/", {"expected_version": 1, "title": "Changed"},
                                 format="json")
        self.assertEqual(r.status_code, 403)
        # MD can edit or cancel it directly
        r = self.md_client.patch(f"/api/v1/shared-tasks/{task_id}/", {"expected_version": 1, "status": "CANCELLED"},
                                 format="json")
        self.assertEqual(r.status_code, 200)

    def test_md_voice_to_pa_becomes_pa_task_not_taskpanel(self):
        r = self.md_client.post("/api/v1/assistant/interpret/", {"text": "Priya ko bolo kal 11 baje vendor ko call kare"},
                                format="json")
        self.assertEqual(r.json()["action"], "pa_task")
        r = self.md_client.post("/api/v1/assistant/interpret/", {"text": "PA ko bolo parso tak hotel book kare"},
                                format="json")
        self.assertEqual(r.json()["action"], "pa_task")

    def test_pa_task_view_never_includes_md_private(self):
        self._create()
        self.assert_no_secret(self.pa_client.get("/api/v1/today/"))
        self.assert_no_secret(self.pa_client.get("/api/v1/pa-tasks/"))


class TaskPanelRulesTests(BaseCase):
    """TaskPanel integration guide §3, §6, §8, §12 acceptance cases (contract mode)."""

    def _interpret(self, client, text):
        r = client.post("/api/v1/assistant/interpret/", {"text": text}, format="json")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def _delegate(self, client, staff_name, key, **extra):
        staff = StaffMember.objects.get(name=staff_name)
        body = {"title": "Resolve login issue", "staff_id": staff.id, "category_key": "general", **extra}
        return client.post("/api/v1/delegations/", body, format="json", HTTP_IDEMPOTENCY_KEY=key)

    def test_qtc_developer_never_matches_developer2(self):
        matches, _ = services.resolve_staff("QTC Developer")
        self.assertEqual([s.external_id for s in matches], ["staff_107"])
        matches, _ = services.resolve_staff("QTC Developer2")
        self.assertEqual([s.external_id for s in matches], ["staff_108"])

    def test_partial_name_is_only_a_suggestion(self):
        matches, suggestions = services.resolve_staff("Ram")
        self.assertEqual(matches, [])
        self.assertEqual({s.name for s in suggestions}, {"Ramesh Kumar", "Ramesh Yadav"})
        draft = self._interpret(self.md_client, "Ram ko bolo kal tak report bheje")
        self.assertTrue(draft["needs_clarification"])
        self.assertIsNone(draft["staff"])

    def test_staff_on_leave_is_flagged_and_rejected(self):
        draft = self._interpret(self.md_client, "Nikki ko bolo parso tak partner addresses update kare")
        self.assertTrue(draft["needs_clarification"])
        self.assertIn("leave", draft["question"].lower())
        r = self._delegate(self.md_client, "Nikki", "leave-1")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["code"], "recipient_on_leave")
        self.assertFalse(SharedTask.objects.filter(staff__name="Nikki").exists())

    def test_category_department_conflict_is_rejected(self):
        r = self._delegate(self.md_client, "Ramesh Kumar", "cat-1", category_key="profile_change")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["code"], "category_recipient_conflict")

    def test_payload_keeps_full_transcript_and_maps_low_priority(self):
        sentence = "Aaj login issue resolve karna hai, ye task QTC Developer ko assign kar do"
        r = self._delegate(self.md_client, "QTC Developer", "full-1", priority="LOW", description=sentence,
                           category_key="login")
        self.assertEqual(r.status_code, 201)
        cmd = IntegrationCommand.objects.get()
        self.assertEqual(cmd.payload["data"]["description"], sentence)
        self.assertEqual(cmd.payload["data"]["priority"], "normal")
        self.assertEqual(cmd.payload["data"]["assignee_key"], "staff_107")
        self.assertEqual(str(cmd.id), cmd.payload["command_id"])

    def test_whole_team_gets_one_task_each_and_skips_leave(self):
        draft = self._interpret(self.md_client, "sab staff ko bolo kal attendance form bhar de")
        self.assertEqual(draft["action"], "team_delegation")
        names = {m["name"] for m in draft["team"]["members"]}
        self.assertIn("Ramesh Kumar", names)
        self.assertNotIn("Nikki", names)  # on leave -> skipped
        self.assertNotIn("Priya", names)  # the PA herself is never part of "the team"
        self.assertEqual([s["name"] for s in draft["team"]["skipped"]], ["Nikki"])
        ids = [m["id"] for m in draft["team"]["members"]]
        r = self.md_client.post("/api/v1/delegations/team/", {"title": "Fill attendance form", "staff_ids": ids,
                                                              "category_key": "general", "description": draft["transcript"]},
                                format="json", HTTP_IDEMPOTENCY_KEY="team-1")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(len(r.json()["assigned"]), len(ids))
        self.assertEqual(SharedTask.objects.filter(assignee_type="STAFF").count(), len(ids))
        self.assertEqual(IntegrationCommand.objects.count(), len(ids))  # one TaskPanel command per person

    def test_department_team(self):
        draft = self._interpret(self.md_client, "Accounts team ko bolo parso tak expense report bheje")
        self.assertEqual(draft["action"], "team_delegation")
        self.assertEqual([m["name"] for m in draft["team"]["members"]], ["Ramesh Kumar"])
        self.assertEqual([s["name"] for s in draft["team"]["skipped"]], ["Nikki"])

    def test_meeting_with_pa_is_md_meeting_and_pa_is_told(self):
        draft = self._interpret(self.md_client, "kal 11 baje meri PA ke saath meeting rakho")
        self.assertEqual(draft["action"], "pa_meeting")
        self.assertIn("inform", draft["spoken_reply"])
        r = self.md_client.post("/api/v1/pa-tasks/", {"title": "Meeting with Priya", "meeting_with_pa": True,
                                                      "due_date": draft["due_date"], "due_time": draft["due_time"]},
                                format="json", HTTP_IDEMPOTENCY_KEY="meet-1")
        self.assertEqual(r.status_code, 201)
        task_id = r.json()["id"]
        # On the MD's own schedule, the PA is alerted, both get reminders
        md_today = self.md_client.get("/api/v1/today/").json()
        self.assertIn(task_id, [i["id"] for i in md_today["upcoming"] + md_today["today"]])
        alert = self.pa_client.get("/api/v1/alerts/").json()["results"][0]
        self.assertIn("scheduled a meeting with you", alert["spoken_text"])
        md_rem = [x for x in self.md_client.get("/api/v1/reminders/").json()["reminders"] if task_id in x["key"]]
        pa_rem = [x for x in self.pa_client.get("/api/v1/reminders/").json()["reminders"] if task_id in x["key"]]
        self.assertTrue(md_rem and pa_rem)
        self.assertIn("meeting with Priya", md_rem[0]["spoken_en"])
        # He gave the instruction in Hinglish, so his reminders now come in Hindi (the English twin stays beside them).
        self.assertEqual(md_rem[0]["lang"], "hi")
        self.assertIn("Priya के साथ आपकी मीटिंग", md_rem[0]["spoken"])

    def test_unclear_owner_asks_where_it_goes(self):
        draft = self._interpret(self.md_client, "kal GST file submit karni hai")
        self.assertEqual(draft["action"], "choose_destination")
        self.assertIn("personal tasks", draft["question"])
        # The answer by voice resolves it
        r = self.md_client.post("/api/v1/assistant/interpret/",
                                {"text": "personal mein daalo", "previous": draft["transcript"]}, format="json")
        self.assertEqual(r.json()["action"], "private_task")
        self.assertEqual(r.json()["due_date"], (tu.today_local() + timedelta(days=1)).isoformat())

    def test_md_is_reminded_of_tasks_given_to_pa(self):
        when = tu.now_local() + timedelta(hours=2)
        r = self.md_client.post("/api/v1/pa-tasks/", {"title": "Courier the contract", "due_date": when.date().isoformat(),
                                                      "due_time": when.strftime("%H:%M")},
                                format="json", HTTP_IDEMPOTENCY_KEY="pa-rem-1")
        task_id = r.json()["id"]
        md_rem = [x for x in self.md_client.get("/api/v1/reminders/").json()["reminders"] if task_id in x["key"]]
        self.assertTrue(md_rem)
        self.assertIn("Priya's task", md_rem[-1]["spoken"])

    def test_time_already_passed_asks_instead_of_moving(self):
        past = (tu.now_local() - timedelta(hours=1)).strftime("%H:%M")
        draft = self._interpret(self.md_client, f"Suresh Patel ko bolo aaj {past} tak stock report bheje")
        self.assertTrue(draft["needs_clarification"])
        self.assertIn("already passed", draft["question"])
        self.assertEqual(draft["due_date"], tu.today_local().isoformat())

    def test_unlinked_account_cannot_delegate(self):
        TaskPanelLink.objects.filter(user=self.pa).update(active=False)
        r = self._delegate(self.pa_client, "Suresh Patel", "unlinked-1")
        self.assertEqual(r.status_code, 403)

    def test_lost_response_is_recovered_by_receipt_not_resent(self):
        """Response lost after TaskPanel committed: the retry finds the receipt, no duplicate task."""
        real = MockTaskPanel()
        calls = {"command": 0}

        class LostResponse(MockTaskPanel):
            def command(self, link, command):
                calls["command"] += 1
                status, result = real.command(link, command)
                self.committed = result
                raise TaskPanelError(0, "timeout", "TaskPanel did not answer in time.", retryable=True,
                                     outcome_unknown=True)

            def command_status(self, link, command_id):
                return {**self.committed, "replayed": True}

        flaky = LostResponse()
        with mock.patch("core.taskpanel.dispatcher.get_client", return_value=flaky):
            r = self._delegate(self.md_client, "Suresh Patel", "lost-1")
            self.assertEqual(r.status_code, 202)
            self.assertEqual(r.json()["result"], "pending_confirmation")
            cmd = IntegrationCommand.objects.get()
            self.assertEqual(cmd.state, "OUTCOME_UNKNOWN")
            IntegrationCommand.objects.filter(id=cmd.id).update(next_attempt_at=tu.now_local())
            from core.taskpanel import dispatcher
            dispatcher.dispatch_due()
        cmd.refresh_from_db()
        self.assertEqual(cmd.state, "SUCCEEDED")
        self.assertEqual(calls["command"], 1)  # never re-sent once the receipt existed
        task = SharedTask.objects.get(assignee_type="STAFF")
        self.assertEqual(task.sync_state, "SYNCED")
        self.assertTrue(task.remote_legacy_id.startswith("T-"))

    def test_change_feed_is_idempotent_and_never_goes_backwards(self):
        r = self._delegate(self.md_client, "Suresh Patel", "feed-1")
        task = SharedTask.objects.get(id=r.json()["id"])
        link = TaskPanelLink.objects.get(user=self.md)

        def event(rev, status, eid):
            return {"schema_version": 1, "event_id": eid, "type": f"task.{status}", "task_uuid": task.remote_task_uuid,
                    "sync_revision": rev, "task_version": rev, "environment": "mock",
                    "task": {"status": status, "assignee_key": "staff_103"}}

        from django.db import transaction
        with transaction.atomic():
            self.assertTrue(tp_sync.apply_event(event(3, "done", "e3"), link))
            self.assertFalse(tp_sync.apply_event(event(2, "working", "e2"), link))  # older: ignored
            self.assertFalse(tp_sync.apply_event(event(3, "done", "e3"), link))  # duplicate: ignored
        task.refresh_from_db()
        self.assertEqual(task.status, "COMPLETED")
        self.assertEqual(task.remote_status, "done")
        self.assertEqual(EventInbox.objects.count(), 2)
        spoken = Alert.objects.filter(kind="STAFF_UPDATE", recipient=self.md).values_list("spoken_text", flat=True)
        self.assertTrue(any("Suresh Patel has completed" in s for s in spoken))

    def test_mock_progress_flows_back_through_pull(self):
        r = self._delegate(self.md_client, "Anil Verma", "progress-1")
        SharedTask.objects.filter(id=r.json()["id"]).update(created_at=tu.now_local() - timedelta(minutes=6))
        applied = tp_sync.pull_changes(TaskPanelLink.objects.get(user=self.md))
        self.assertEqual(applied, 2)  # started + completed
        self.assertEqual(SharedTask.objects.get(id=r.json()["id"]).status, "COMPLETED")

    def test_signature_matches_guide_canonical_string(self):
        body, headers = signing.sign("POST", "/taskPanel/integration/v1/commands", {"command_id": "abc", "x": "₹"},
                                     "django-local-01", "secret", "grant-xyz", stamp="1700000000", nonce="n-1")
        self.assertEqual(body, '{"command_id":"abc","x":"₹"}'.encode("utf-8"))
        canonical = "\n".join([
            "POST", "/taskPanel/integration/v1/commands", "django-local-01", "1700000000", "n-1",
            hashlib.sha256(b"grant-xyz").hexdigest(), hashlib.sha256(body).hexdigest(),
        ]).encode("utf-8")
        expected = hmac.new(b"secret", canonical, hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-Integration-Signature"], "v1=" + expected)
        self.assertEqual(headers["Idempotency-Key"], "abc")
        self.assertEqual(headers["X-Content-SHA256"], hashlib.sha256(body).hexdigest())


def _cmd(**fields) -> dict:
    """A complete model command (COMMAND_SCHEMA shape) with defaults."""
    base = {
        "intent": "unknown", "title": "", "notes": "", "kind": "TASK", "priority": "NORMAL", "due_date": "",
        "due_time": "", "duration_minutes": 0, "remind_before_minutes": 0, "staff_name": "", "team_name": "",
        "category_key": "", "target_ref": "", "target_refs": [], "changed_fields": [], "setting_key": "", "setting_value": "",
        "needs_clarification": False, "clarification_question": "",
        "spoken_reply": "", "spoken_reply_hi": "", "reply_language": "en", "question": "", "memory_text": "",
        "reminder_line": "", "confidence": 0.9, "engine": "ai",
    }
    base.update(fields)
    return base


def _fake_answer_response(text: str, searches: int = 1):
    from types import SimpleNamespace as NS
    return NS(
        content=[NS(type="text", text=text, citations=[NS(url="https://weather.example/indore", title="Weather")])],
        stop_reason="end_turn",
        usage=NS(input_tokens=120, output_tokens=40, cache_creation_input_tokens=0, cache_read_input_tokens=0,
                 server_tool_use=NS(web_search_requests=searches)),
    )


class SmartAssistantTests(BaseCase):
    def interpret(self, client, text, cmd, **extra):
        with mock.patch("core.ai_brain.interpret", return_value=cmd) as fake:
            r = client.post("/api/v1/assistant/interpret/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200)
        return r.json(), fake

    def test_general_question_goes_to_ask_with_hindi_voice(self):
        draft, _ = self.interpret(self.md_client, "aaj Indore ka temperature kya hai", _cmd(
            intent="general_question", question="What is the weather in Indore today?",
            spoken_reply="Ek second Sir, dekh raha hoon.", reply_language="hi", spoken_reply_hi="एक सेकंड सर, देख रहा हूँ।"))
        self.assertEqual(draft["action"], "ask")
        self.assertEqual(draft["ask_text"], "What is the weather in Indore today?")
        self.assertEqual(draft["speak_lang"], "hi")
        self.assertIn("सर", draft["speak_text"])
        self.assertFalse(draft["needs_clarification"])

    def test_server_built_confirmation_has_its_own_hindi_twin(self):
        draft, _ = self.interpret(self.md_client, "Ramesh ko bolo kal GST file bheje", _cmd(
            intent="assign_to_staff", title="GST file bhejna", staff_name="Ramesh Kumar",
            spoken_reply="Sir, Ramesh ko GST file ka task de doon?", reply_language="hi",
            spoken_reply_hi="सर, रमेश को जीएसटी फाइल का टास्क दे दूँ?", engine="rules"))
        self.assertEqual(draft["action"], "delegation")
        # The server rewrote the reply: the model's Devanagari line is stale, the server's own twin is spoken instead.
        self.assertEqual(draft["speak_lang"], "hi")
        self.assertNotEqual(draft["speak_text"], "सर, रमेश को जीएसटी फाइल का टास्क दे दूँ?")
        self.assertIn("Ramesh Kumar को असाइन", draft["speak_text"])
        self.assertTrue(draft["speak_text"].endswith("सेव कर दूँ?"), draft["speak_text"])
        self.assertIn("Assign to Ramesh Kumar", draft["spoken_reply"])  # the screen still shows the English line
        # The same sentence in English is spoken in English.
        draft, _ = self.interpret(self.md_client, "Tell Ramesh to send the GST file tomorrow", _cmd(
            intent="assign_to_staff", title="Send the GST file", staff_name="Ramesh Kumar", engine="rules"))
        self.assertEqual((draft["speak_lang"], draft["speak_text"]), ("en", draft["spoken_reply"]))

    def test_conversation_and_own_memories_reach_the_model(self):
        services.add_memory(self.md, "City is Indore.")
        services.add_memory(self.pa, "Prefers WhatsApp for updates.")
        history = [{"role": "user", "text": "Ramesh ko bolo report bheje"}, {"role": "jarvis", "text": "Saved, Sir."}]
        _, fake = self.interpret(self.md_client, "aur Neha ko bhi", _cmd(intent="small_talk", spoken_reply="Hello Sir"),
                                 history=history)
        ctx = fake.call_args.args[1]
        self.assertEqual(ctx["conversation"][0]["text"], "Ramesh ko bolo report bheje")
        self.assertEqual(ctx["memories"], ["City is Indore."])
        _, fake = self.interpret(self.pa_client, "hello", _cmd(intent="small_talk", spoken_reply="Hi Priya"))
        pa_ctx = json.dumps(fake.call_args.args[1], default=str)
        self.assertNotIn("Indore", pa_ctx)  # the MD's memory never reaches a PA context
        self.assertNotIn(SECRET, pa_ctx)

    def test_remember_and_end_drafts(self):
        draft, _ = self.interpret(self.md_client, "yaad rakhna main 7 baje gym jaata hoon", _cmd(
            intent="remember", memory_text="Goes to the gym at 7 AM.", spoken_reply="Sir, I'll remember that. Okay?"))
        self.assertEqual(draft["action"], "remember")
        self.assertEqual(draft["memory_text"], "Goes to the gym at 7 AM.")
        draft, _ = self.interpret(self.md_client, "bas thank you", _cmd(intent="end_conversation", spoken_reply=""))
        self.assertEqual(draft["action"], "end")
        self.assertTrue(draft["spoken_reply"])

    def test_confirmation_is_understood_from_meaning(self):
        awaiting = "Personal task: Call Mehta ji, tomorrow 11 AM"
        draft, fake = self.interpret(self.md_client, "bilkul kar do yaar", _cmd(intent="confirm_pending"),
                                     previous="Kal 11 baje Mehta ji ko call", awaiting=awaiting)
        self.assertEqual(draft["action"], "confirm_pending")
        self.assertEqual(fake.call_args.args[1]["awaiting_confirmation"],
                         {"draft": awaiting, "original_sentence": "Kal 11 baje Mehta ji ko call"})
        self.assertEqual(fake.call_args.args[0], "bilkul kar do yaar")  # the reply alone, not glued to the draft
        draft, _ = self.interpret(self.md_client, "abhi rehne do", _cmd(intent="cancel_pending", spoken_reply="Theek hai Sir."),
                                  awaiting=awaiting)
        self.assertEqual(draft["action"], "cancel_pending")
        # "yes" with nothing waiting is not a confirmation of anything.
        draft, _ = self.interpret(self.md_client, "haan", _cmd(intent="confirm_pending"))
        self.assertNotIn(draft["action"], {"confirm_pending", "cancel_pending"})

    def test_done_reply_follows_the_speakers_language(self):
        draft, _ = self.interpret(self.md_client, "kal 4 baje Sharma ji ko file bhejni hai", _cmd(
            intent="create_task", title="Send the file to Sharma ji", kind="TASK", reply_language="hi",
            due_date=(tu.today_local() + timedelta(days=1)).isoformat(), due_time="16:00",
            spoken_reply="Sir, kal 4 baje Sharma ji ke saath meeting. Save kar doon?",
            spoken_reply_hi="सर, कल चार बजे शर्मा जी के साथ मीटिंग। सेव कर दूँ?",
            done_reply="Ho gaya Sir, kal 4 baje yaad dila dunga.", done_reply_hi="हो गया सर, कल चार बजे याद दिला दूँगा।"))
        self.assertEqual(draft["action"], "private_task")
        self.assertEqual(draft["done_lang"], "hi")
        self.assertIn("याद", draft["done_speak"])

    def test_offline_confirmation_fallback(self):
        awaiting = "Personal task: Call Mehta ji"
        with mock.patch("core.ai_brain.interpret", side_effect=lambda text, *a, **k: {
                **hinglish_parser.parse(text), "engine": "rules"}):
            yes = self.md_client.post("/api/v1/assistant/interpret/", {"text": "haan ji", "awaiting": awaiting},
                                      format="json").json()
            no = self.md_client.post("/api/v1/assistant/interpret/", {"text": "nahi", "awaiting": awaiting},
                                     format="json").json()
        self.assertEqual(yes["action"], "confirm_pending")
        self.assertEqual(no["action"], "cancel_pending")

    def test_mark_several_done_by_voice(self):
        now = tu.now_local() + timedelta(hours=2)
        a = services.create_private_task(self.md, {"title": "Call Mehta ji", "kind": Kind.CALL, "priority": Priority.NORMAL,
                                                   "due_date": now.date(), "due_time": now.time().replace(second=0, microsecond=0)})
        b = services.create_private_task(self.md, {"title": "Call Mehta ji about the Pune franchise", "kind": Kind.CALL,
                                                   "priority": Priority.NORMAL, "due_date": now.date(),
                                                   "due_time": now.time().replace(second=0, microsecond=0)})
        # "which one?" -> "dono": the answer is sent with the choices, and the model names both ids.
        choices = {"intent": "complete_task", "items": [{"id": str(a.id), "title": "Call Mehta ji"},
                                                        {"id": str(b.id), "title": "Call Mehta ji about the Pune franchise"}]}
        draft, fake = self.interpret(self.md_client, "dono", _cmd(intent="complete_task", target_refs=[str(a.id), str(b.id)]),
                                     previous="Mehta wala call ho gaya", choices=choices)
        self.assertEqual(draft["action"], "complete")
        self.assertEqual([t["id"] for t in draft["targets"]], [str(a.id), str(b.id)])
        self.assertIn("2", draft["spoken_reply"])
        self.assertEqual(fake.call_args.args[0], "dono")
        self.assertEqual(fake.call_args.args[1]["choices"]["intent"], "complete_task")
        # The model picked the items but dropped the intent: the pending intent is restored.
        draft, _ = self.interpret(self.md_client, "the franchise one", _cmd(intent="unknown", target_refs=[str(b.id)]), choices=choices)
        self.assertEqual(draft["action"], "complete")
        self.assertEqual([t["id"] for t in draft["targets"]], [str(b.id)])
        # An id that is not in the context is ignored, never acted on.
        draft, _ = self.interpret(self.md_client, "done", _cmd(intent="complete_task", target_refs=["not-a-real-id"], title="xyz"))
        self.assertNotEqual(draft["action"], "complete") if draft["targets"] else self.assertTrue(draft["needs_clarification"])

    def turn(self, client, text, cmd=None, **extra):
        if cmd is None:
            r = client.post("/api/v1/assistant/turn/", {"text": text, **extra}, format="json")
        else:
            with mock.patch("core.ai_brain.interpret", return_value=cmd):
                r = client.post("/api/v1/assistant/turn/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_background_conversation_creates_a_task_after_yes(self):
        tomorrow = (tu.today_local() + timedelta(days=1)).isoformat()
        r = self.turn(self.md_client, "kal 4 baje Sharma ji ko file bhejni hai", _cmd(
            intent="create_task", title="Send the file to Sharma ji", kind="TASK", due_date=tomorrow, due_time="16:00",
            spoken_reply="Sir, send the file to Sharma ji tomorrow at 4 PM. Shall I save it?",
            done_reply="Ho gaya Sir, kal 4 baje yaad dila dunga."))
        self.assertEqual(r["action"], "private_task")
        self.assertTrue(r["listen"])
        before = PrivateTask.objects.filter(owner=self.md).count()
        r = self.turn(self.md_client, "bilkul kar do", _cmd(intent="confirm_pending"))
        self.assertEqual(r["action"], "saved")
        self.assertEqual(r["show"], "Ho gaya Sir, kal 4 baje yaad dila dunga.")
        self.assertEqual(PrivateTask.objects.filter(owner=self.md).count(), before + 1)
        # Nothing is waiting any more: a second "yes" saves nothing.
        r = self.turn(self.md_client, "haan", _cmd(intent="confirm_pending"))
        self.assertEqual(PrivateTask.objects.filter(owner=self.md).count(), before + 1)
        self.assertNotEqual(r["action"], "saved")

    def test_background_conversation_ends_and_forgets(self):
        self.turn(self.md_client, "Mehta ji ko call", _cmd(intent="create_task", title="Call Mehta ji", spoken_reply="Save?"))
        r = self.turn(self.md_client, "bas, stop listening", _cmd(intent="end_conversation", spoken_reply="Ji Sir."))
        self.assertFalse(r["listen"])
        before = PrivateTask.objects.filter(owner=self.md).count()
        self.turn(self.md_client, "haan", _cmd(intent="confirm_pending"))
        self.assertEqual(PrivateTask.objects.filter(owner=self.md).count(), before)  # the draft was forgotten

    def test_wake_only_greets_and_locked_phone_hides_private_items(self):
        past = tu.now_local() - timedelta(minutes=20)
        services.create_private_task(self.md, {"title": SECRET + " call", "kind": Kind.CALL, "priority": Priority.HIGH,
                                               "due_date": past.date(), "due_time": past.time().replace(second=0, microsecond=0)})
        r = self.turn(self.md_client, "", event="wake", locked=True, private_ok=False)
        self.assertEqual(r["action"], "greet")  # "Hey JARVIS" is listen-only: no reminders, no offers
        self.assertEqual(r["show"], "Ji Sir?")
        self.assertNotIn(SECRET, json.dumps(r))
        # Locked: the model never even sees the private item.
        _, fake = None, None
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="query", spoken_reply="Nothing today.")) as fake:
            self.md_client.post("/api/v1/assistant/turn/", {"text": "aaj kya hai", "locked": True, "private_ok": False}, format="json")
        self.assertNotIn(SECRET, json.dumps(fake.call_args.args[1], default=str))
        # "no, accept it" style replies are commands, not a plain no: a dismiss clears the banner.
        r = self.turn(self.md_client, "ye hatao", _cmd(intent="dismiss_alert"))
        self.assertEqual(r["action"], "dismiss")

    def test_background_turn_never_shows_private_items_to_pa(self):
        r = self.turn(self.pa_client, "Sir ke tasks batao", _cmd(intent="query", spoken_reply="Nothing from Sir today."))
        self.assertNotIn(SECRET, json.dumps(r))
        from core.models import ConversationState
        self.assertNotIn(SECRET, ConversationState.objects.get(user=self.pa).state_json)

    def test_change_task_by_voice_moves_the_time(self):
        tomorrow = tu.today_local() + timedelta(days=1)
        task = services.create_private_task(self.md, {"title": "Meeting with Sharma ji", "kind": Kind.MEETING,
                                                      "priority": Priority.NORMAL, "due_date": tomorrow,
                                                      "due_time": time(9, 0)})
        r = self.turn(self.md_client, "Sharma meeting 5 baje kar do", _cmd(
            intent="change_task", target_refs=[str(task.id)], changed_fields=["due_time"], due_time="17:00",
            spoken_reply="Sir, move the Sharma meeting to 5 PM?"))
        self.assertEqual(r["action"], "change_task")
        task.refresh_from_db()
        self.assertNotEqual(tu.to_local(task.due_at).hour, 17)  # nothing changes before the yes
        r = self.turn(self.md_client, "haan", _cmd(intent="confirm_pending"))
        self.assertEqual(r["action"], "saved")
        task.refresh_from_db()
        self.assertEqual(tu.to_local(task.due_at).hour, 17)
        self.assertEqual(task.version, 2)

    def test_delete_task_by_voice_always_asks_then_cancels(self):
        task = services.create_private_task(self.md, {"title": "Renew club membership", "kind": Kind.TASK,
                                                      "priority": Priority.LOW, "due_date": tu.today_local()})
        r = self.turn(self.md_client, "club wala task hata do", _cmd(intent="delete_task", target_refs=[str(task.id)]))
        self.assertEqual(r["action"], "delete_task")
        self.assertIn("delete", r["show"].lower())
        task.refresh_from_db()
        self.assertEqual(task.status, "OPEN")
        self.turn(self.md_client, "haan hata do", _cmd(intent="confirm_pending"))
        task.refresh_from_db()
        self.assertEqual(task.status, "CANCELLED")

    def test_settings_by_voice_and_api(self):
        r = self.turn(self.md_client, "Hindi mein bolo", _cmd(intent="set_setting", setting_key="voiceAccent",
                                                               setting_value="Hindi", spoken_reply="Done Sir, Hindi voice."))
        self.assertEqual(r["action"], "setting_applied")
        self.assertEqual(self.md_client.get("/api/v1/me/prefs/").json()["voiceAccent"], "hi-IN")
        r = self.turn(self.md_client, "quiet hours 10 baje se 7 tak", _cmd(intent="set_setting", setting_key="quietHours",
                                                                            setting_value="22-7", spoken_reply=""))
        prefs = self.md_client.get("/api/v1/me/prefs/").json()
        self.assertEqual((prefs["quietStartHour"], prefs["quietEndHour"], prefs["quietHoursEnabled"]), (22, 7, True))
        self.assertEqual(self.md_client.patch("/api/v1/me/prefs/", {"meetingMode": "on"}, format="json").json()["meetingMode"], True)
        self.assertEqual(self.md_client.patch("/api/v1/me/prefs/", {"nonsense": 1}, format="json").status_code, 422)
        # The PA's settings are her own.
        self.assertEqual(self.pa_client.get("/api/v1/me/prefs/").json()["voiceAccent"], "hi-IN")

    @override_settings(JARVIS_AI_ENABLED=True, ANTHROPIC_API_KEY="test-key")
    def test_pa_hears_a_composed_professional_note(self):
        from types import SimpleNamespace as NS
        client = mock.Mock()
        note = {"text": "Mansi, please keep 10:00 today free for a meeting with Sir. It is urgent.",
                "text_hi": "मानसी, कृपया आज 10 बजे सर के साथ मीटिंग के लिए समय रखिए। यह अर्जेंट है।"}
        client.messages.create.return_value = NS(
            content=[NS(type="text", text=json.dumps(note, ensure_ascii=False))],
            stop_reason="end_turn", usage=NS(input_tokens=50, output_tokens=20, cache_creation_input_tokens=0,
                                             cache_read_input_tokens=0, server_tool_use=None))
        when = tu.now_local() + timedelta(hours=3)
        with mock.patch("core.ai_brain._get_client", return_value=client):
            r = self.md_client.post("/api/v1/pa-tasks/", {
                "title": "Meeting with Mansi", "kind": "MEETING", "priority": "URGENT", "meeting_with_pa": True,
                "due_date": when.date().isoformat(), "due_time": when.strftime("%H:%M"),
                "description": "Mansi ke saath 10 baje meeting, urgent"}, format="json", HTTP_IDEMPOTENCY_KEY="note-1")
        self.assertEqual(r.status_code, 201)
        alert = self.pa_client.get("/api/v1/alerts/").json()["results"][0]
        self.assertEqual(alert["spoken_text"], note["text"])
        self.assertEqual(alert["spoken_text_hi"], note["text_hi"])  # the Hindi twin travels with it
        sent = json.dumps(client.messages.create.call_args.kwargs, default=str)
        self.assertNotIn(SECRET, sent)  # only the task's own facts go to the model
        # The PA has been speaking Hindi lately: the same alert is spoken to her in Hindi.
        services.set_pref_quiet(self.pa, "langRecent", "hi,hi,en,hi")
        alert = self.pa_client.get("/api/v1/alerts/").json()["results"][0]
        self.assertEqual(alert["spoken_text"], note["text_hi"])
        self.assertEqual(alert["spoken_text_en"], note["text"])

    def test_composed_note_falls_back_to_template_when_ai_is_off(self):
        r = self.md_client.post("/api/v1/pa-tasks/", {"title": "Book the Pune flights", "kind": "TASK"},
                                format="json", HTTP_IDEMPOTENCY_KEY="note-2")
        self.assertEqual(r.status_code, 201)
        alert = self.pa_client.get("/api/v1/alerts/").json()["results"][0]
        self.assertIn("Book the Pune flights", alert["spoken_text"])

    def test_followups_raise_once_and_only_in_the_day(self):
        from core import followups
        from core.models import Alert
        import datetime as dt
        day = tu.now_local().replace(hour=11, minute=0, second=0, microsecond=0)
        # A staff task that has not moved for 3 days.
        task = SharedTask.objects.get(assignee_type=SharedTask.Assignee.STAFF) if SharedTask.objects.filter(
            assignee_type=SharedTask.Assignee.STAFF).exists() else None
        if task is None:
            ramesh = StaffMember.objects.filter(name="Ramesh Kumar").first()
            task, _ = services.delegate_to_staff(self.md, {"title": "Submit GST reconciliation", "kind": Kind.TASK,
                                                           "priority": Priority.HIGH}, ramesh, category_key="accounts",
                                                 description="GST reconciliation")
        SharedTask.objects.filter(id=task.id).update(updated_at=day - dt.timedelta(days=3), created_at=day - dt.timedelta(days=3))
        # A request from Mansi waiting 6 hours.
        req = services.create_md_request(self.pa, {"title": "Sign the vendor agreement", "kind": Kind.TASK, "priority": Priority.HIGH})
        TaskRequest.objects.filter(id=req.id).update(created_at=day - dt.timedelta(hours=6))
        Alert.objects.all().delete()

        self.assertEqual(followups.run(now=day.replace(hour=23)), 0)  # night: silence
        raised = followups.run(now=day)
        self.assertGreaterEqual(raised, 3)
        md_alerts = [a for a in Alert.objects.filter(recipient=self.md, ref_type="followup")]
        spoken = " | ".join(a.spoken_text for a in md_alerts)
        self.assertIn("no update for 3 days", spoken)
        self.assertIn("waiting 6 hours", spoken)
        self.assertTrue(Alert.objects.filter(recipient=self.pa, ref_type="followup").exists())
        self.assertEqual(followups.run(now=day + dt.timedelta(minutes=10)), 0)  # not repeated
        self.assert_no_secret(self.pa_client.get("/api/v1/alerts/"))

    def test_end_of_day_digest_counts_private_items_without_naming_them(self):
        from core import followups
        from core.models import Alert
        today = tu.today_local()
        services.create_private_task(self.md, {"title": SECRET + " errand", "kind": Kind.TASK, "priority": Priority.NORMAL,
                                               "due_date": today})
        Alert.objects.all().delete()
        at = tu.now_local().replace(hour=18, minute=45, second=0, microsecond=0)
        followups.run(now=at)
        digest = Alert.objects.filter(recipient=self.md, ref_id__startswith="digest:").first()
        self.assertIsNotNone(digest)
        self.assertIn("private", digest.spoken_text)
        self.assertNotIn(SECRET, digest.spoken_text + digest.body + digest.title)

    def test_device_token_is_a_separate_login_chain(self):
        self.assertEqual(APIClient().post("/api/v1/auth/device-token/", {}, format="json").status_code, 401)
        r = self.md_client.post("/api/v1/auth/device-token/", {}, format="json")
        self.assertEqual(r.status_code, 200)
        device = r.json()
        # The service refreshes with its own token, and the app's own session is untouched.
        r2 = APIClient().post("/api/v1/auth/refresh/", {"refresh": device["refresh"]}, format="json")
        self.assertEqual(r2.status_code, 200)
        self.assertIn("access", r2.json())
        self.assertEqual(self.md_client.get("/api/v1/me/").status_code, 200)

    def test_memories_are_owner_only_and_encrypted(self):
        r = self.md_client.post("/api/v1/memories/", {"text": SECRET}, format="json", HTTP_IDEMPOTENCY_KEY="mem-1")
        self.assertEqual(r.status_code, 201)
        memory_id = r.json()["id"]
        self.assertEqual(self.md_client.get("/api/v1/memories/").json()["results"][0]["text"], SECRET)
        self.assert_no_secret(self.pa_client.get("/api/v1/memories/"))
        self.assertEqual(self.pa_client.get("/api/v1/memories/").json()["results"], [])
        self.assertEqual(self.pa_client.delete(f"/api/v1/memories/{memory_id}/").status_code, 404)
        with connection.cursor() as cur:
            cur.execute("SELECT text FROM core_memory")
            self.assertTrue(all(row[0].startswith("enc1:") for row in cur.fetchall()))
        self.assertEqual(self.md_client.delete(f"/api/v1/memories/{memory_id}/").status_code, 204)
        self.assertEqual(self.md_client.get("/api/v1/memories/").json()["results"], [])

    def test_retry_receipts_do_not_store_private_text_in_clear(self):
        body = {"title": SECRET, "kind": "TASK"}
        first = self.md_client.post("/api/v1/private-tasks/", body, format="json", HTTP_IDEMPOTENCY_KEY="pt-enc")
        again = self.md_client.post("/api/v1/private-tasks/", body, format="json", HTTP_IDEMPOTENCY_KEY="pt-enc")
        self.assertEqual(first.json(), again.json())
        with connection.cursor() as cur:
            cur.execute("SELECT response FROM core_commandreceipt")
            self.assertNotIn(SECRET, json.dumps([row[0] for row in cur.fetchall()], default=str))

    @override_settings(JARVIS_AI_ENABLED=True, ANTHROPIC_API_KEY="test-key")
    def test_ask_uses_web_search_and_sends_no_private_data(self):
        services.add_memory(self.md, SECRET + " memory")
        client = mock.Mock()
        client.messages.create.return_value = _fake_answer_response(
            "SHOW: Sir, Indore mein abhi 29 degree hai.\nSPEAK_HI: सर, इंदौर में अभी उनतीस डिग्री है।")
        with mock.patch("core.ai_brain._get_answer_client", return_value=client):
            r = self.md_client.post("/api/v1/assistant/ask/", {
                "question": "What is the weather in Indore today?", "lang": "hi",
                "history": [{"role": "user", "text": "aaj ka mausam?"}]}, format="json")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["text"], "Sir, Indore mein abhi 29 degree hai.")
        self.assertEqual(data["speak_lang"], "hi")
        self.assertEqual(data["sources"][0]["url"], "https://weather.example/indore")
        sent = json.dumps(client.messages.create.call_args.kwargs, default=str)
        self.assertNotIn(SECRET, sent)  # no tasks or memories go to the general-question call
        self.assertIn("web_search", sent)
        usage = self.md.ai_usage.get(kind="ANSWER")
        self.assertEqual((usage.requests, usage.web_searches), (1, 1))

    @override_settings(JARVIS_AI_ENABLED=True, ANTHROPIC_API_KEY="test-key", JARVIS_AI_DAILY_ANSWER_LIMIT=1)
    def test_daily_question_limit(self):
        from core.models import AiUsage
        AiUsage.objects.create(user=self.md, day=tu.today_local(), kind="ANSWER", requests=1)
        client = mock.Mock()
        with mock.patch("core.ai_brain._get_answer_client", return_value=client):
            data = self.md_client.post("/api/v1/assistant/ask/", {"question": "Gold price today?"}, format="json").json()
        self.assertEqual(data["engine"], "limit")
        client.messages.create.assert_not_called()
        self.assertEqual(self.md_client.get("/api/v1/assistant/usage/").json()["questions_today"], 1)


@override_settings(XAI_API_KEY="xai-test", JARVIS_GROK_VOICE=True, JARVIS_TTS_DAILY_CHAR_LIMIT=40, JARVIS_TTS_MAX_CHARS=80)
class GrokVoiceTests(BaseCase):
    """JARVIS's spoken replies are voiced by Grok; the phone falls back to its own voice on any refusal."""

    def test_text_in_audio_out_counted_and_capped(self):
        with mock.patch("core.xai.tts", return_value=b"ID3-fake-mp3") as tts:
            r = self.md_client.post("/api/v1/assistant/speak/", {"text": "Saved,   Sir.", "lang": "en"}, format="json")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r["Content-Type"], "audio/mpeg")
            self.assertEqual(r.content, b"ID3-fake-mp3")
            self.assertEqual(tts.call_args.args[0], "Saved, Sir.")           # whitespace tidied
            self.assertEqual(tts.call_args.kwargs["language"], "en")
            # Hindi text is sent as Hindi; a long reply is cut at a sentence end, never mid-word.
            long = "सर, कल चार बजे शर्मा जी के साथ मीटिंग है। " + "यह बहुत लंबा वाक्य है जो सीमा से आगे निकल जाता है और काटा जाना चाहिए।"
            self.md_client.post("/api/v1/assistant/speak/", {"text": long, "lang": "hi-IN"}, format="json")
            self.assertEqual(tts.call_args.kwargs["language"], "hi")
            self.assertEqual(tts.call_args.args[0], "सर, कल चार बजे शर्मा जी के साथ मीटिंग है।")
            # The daily cap (40 characters here) is now passed: the phone is told to use its own voice.
            r = self.md_client.post("/api/v1/assistant/speak/", {"text": "Done, Sir."}, format="json")
            self.assertEqual(r.status_code, 429)
            self.assertEqual(r.json()["code"], "voice_limit")
            # Mansi's allowance is her own.
            self.assertEqual(self.pa_client.post("/api/v1/assistant/speak/", {"text": "Done."}, format="json").status_code, 200)
        usage = self.md_client.get("/api/v1/assistant/usage/").json()
        self.assertEqual(usage["voice_replies"], 2)
        self.assertGreater(usage["voice_chars"], 40)

    def test_phone_gets_a_live_sound_stream(self):
        from core import xai

        def fake_stream(text, **kwargs):
            yield b"\x01\x02" * 100
            yield b"\x03\x04" * 100

        with mock.patch("core.xai.tts_stream", side_effect=fake_stream) as stream:
            r = self.md_client.post("/api/v1/assistant/speak/", {"text": "जी सर?", "lang": "hi", "format": "pcm"}, format="json")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r["X-Sample-Rate"], "24000")
            self.assertEqual(b"".join(r.streaming_content), b"\x01\x02" * 100 + b"\x03\x04" * 100)
            self.assertEqual(stream.call_args.args[0], "जी सर?")

        def broken(text, **kwargs):
            raise xai.XaiError(502, "no route")
            yield b""  # pragma: no cover

        # Unreachable before any sound: a clean error, so the phone uses its own voice for this sentence.
        with mock.patch("core.xai.tts_stream", side_effect=broken):
            r = self.md_client.post("/api/v1/assistant/speak/", {"text": "Done.", "format": "pcm"}, format="json")
            self.assertEqual(r.status_code, 502)

    def test_voice_off_or_failing_tells_the_phone_to_fall_back(self):
        from core import xai
        self.assertEqual(self.md_client.post("/api/v1/assistant/speak/", {"text": ""}, format="json").status_code, 422)
        with mock.patch("core.xai.tts", side_effect=xai.XaiError(500, "boom")):
            self.assertEqual(self.md_client.post("/api/v1/assistant/speak/", {"text": "Hello"}, format="json").status_code, 502)
        with override_settings(XAI_API_KEY=""):
            r = self.md_client.post("/api/v1/assistant/speak/", {"text": "Hello"}, format="json")
            self.assertEqual((r.status_code, r.json()["code"]), (503, "voice_off"))
        self.assertEqual(APIClient().post("/api/v1/assistant/speak/", {"text": "Hello"}, format="json").status_code, 401)


class MeetingOverrideTests(BaseCase):
    def test_switching_meeting_mode_off_during_a_diary_meeting_sticks_until_it_ends(self):
        # The fixture meeting (now + 1 h, 30 min) is in the diary; make one that is running right now too.
        now = tu.now_local()
        # Started 50 minutes ago, so it ends well before the fixture meeting even late in the evening.
        start = (now - timedelta(minutes=50)).replace(second=0, microsecond=0)
        running = services.create_private_task(self.md, {"title": "Running meeting", "kind": Kind.MEETING, "priority": Priority.NORMAL,
                                                         "due_date": start.date(), "due_time": start.time(), "duration_minutes": 60})
        windows = self.md_client.get("/api/v1/reminders/").json()["busy_windows"]
        self.assertTrue(any(datetime.fromisoformat(w["start"]) <= running.scheduled_start <= datetime.fromisoformat(w["end"]) for w in windows))
        # He switches meeting mode off while it runs: that meeting stops counting, the later one still does.
        r = self.md_client.patch("/api/v1/me/prefs/", {"meetingMode": False}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["meetingOverrideUntil"])
        windows = self.md_client.get("/api/v1/reminders/").json()["busy_windows"]
        self.assertFalse(any(datetime.fromisoformat(w["start"]) <= running.scheduled_start < datetime.fromisoformat(w["end"]) for w in windows))
        self.assertTrue(any(datetime.fromisoformat(w["start"]) == self.private.scheduled_start for w in windows))
        # Mansi still sees Sir as busy (his override is about his own phone's manners, not his availability).
        pa_busy = self.pa_client.get("/api/v1/today/").json()["md_busy_today"]
        self.assertTrue(any(datetime.fromisoformat(w["start"]) <= running.scheduled_start < datetime.fromisoformat(w["end"]) for w in pa_busy))
        # Switching it on again clears the override; the AI never sees this internal setting.
        self.assertEqual(self.md_client.patch("/api/v1/me/prefs/", {"meetingMode": True}, format="json").json()["meetingOverrideUntil"], "")
        self.assertNotIn("meetingOverrideUntil", services.ai_context(self.md)["settings"])
        health = self.md_client.get("/api/v1/health/").json()
        self.assertIn("push_devices", health)


class DataViewTests(BaseCase):
    """Answers as data: timeline / chart / table views, scoped exactly like everything else."""

    def test_query_answers_carry_a_view_and_the_pa_never_sees_private_items(self):
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="query", view_kind="today", spoken_reply="Sir, 1 meeting today.")):
            draft = self.md_client.post("/api/v1/assistant/interpret/", {"text": "aaj ka plan kya hai"}, format="json").json()
        self.assertEqual(draft["action"], "answer")
        self.assertEqual(draft["view"]["layout"], "timeline")
        self.assertTrue(any(t["title"] == SECRET for t in draft["view"]["timeline"]))  # Sir sees his own private meeting
        self.assertEqual(draft["view"]["stats"][0]["label"], "Items")
        # The model forgot view_kind: the words decide.
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="query", spoken_reply="Nothing is late.")):
            draft = self.md_client.post("/api/v1/assistant/interpret/", {"text": "kya kuch late hai?"}, format="json").json()
        self.assertEqual(draft["view"]["kind"], "late")
        # Mansi asks the same: her view has her items only, and Sir's private meeting is only "Busy".
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="query", view_kind="busy", spoken_reply="Sir is busy at 1 PM.")):
            r = self.pa_client.post("/api/v1/assistant/interpret/", {"text": "Sir aaj free kab hain"}, format="json")
        self.assert_no_secret(r)
        self.assertEqual(r.json()["view"]["layout"], "busy")
        self.assertEqual(len(r.json()["view"]["timeline"]), 1)
        # Locked phone: Sir's private items stay off the screen.
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="query", view_kind="today", spoken_reply="x")):
            r = self.md_client.post("/api/v1/assistant/turn/", {"text": "aaj kya hai", "locked": True, "private_ok": False}, format="json")
        self.assertNotIn(SECRET, json.dumps(r.json()))

    def test_views_endpoint_week_stats_and_scope(self):
        week = self.md_client.get("/api/v1/views/week/").json()
        self.assertEqual(len(week["series"]), 7)
        self.assertTrue(week["series"][0]["today"])
        stats = self.md_client.get("/api/v1/views/stats/").json()
        self.assertEqual([s["label"] for s in stats["stats"]][:3], ["Today", "Late", "Done today"])
        self.assertEqual(self.pa_client.get("/api/v1/views/private/").status_code, 404)
        for kind in ("today", "week", "stats", "pa", "staff", "requests", "busy", "late", "upcoming", "nodate"):
            r = self.pa_client.get(f"/api/v1/views/{kind}/")
            self.assertEqual(r.status_code, 200, kind)
            self.assert_no_secret(r)
        self.assertEqual(self.md_client.get("/api/v1/views/nonsense/").status_code, 404)
        # Downloads are built from the rows on the phone; the share text is ready-made here.
        self.assertIn("Late", self.md_client.get("/api/v1/views/late/").json()["text"])
        self.assertIn("view", self.md_client.get("/api/v1/assistant/briefing/?kind=now").json())


class PushTests(BaseCase):
    """Live push: every new alert goes to the recipient's phones, and only theirs."""

    def setUp(self):
        super().setUp()
        self.sent = []  # (tokens, data)

        def fake_deliver(tokens, data, urgent):
            self.sent.append((list(tokens), dict(data)))
            return [(t != "dead-token", "" if t != "dead-token" else "UNREGISTERED") for t in tokens]

        patches = [
            mock.patch("core.push._deliver", side_effect=fake_deliver),
            mock.patch("core.push.configured", return_value=True),
            mock.patch("core.push.SYNC", True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_phone_registers_and_alert_is_pushed_to_the_right_person_only(self):
        r = self.pa_client.post("/api/v1/me/push-token/", {"token": "pa-phone-1"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.md_client.post("/api/v1/me/push-token/", {"token": "md-phone-1"}, format="json")
        with self.captureOnCommitCallbacks(execute=True):
            services.create_pa_task(self.md, {"title": "Courier the contract", "kind": Kind.TASK, "priority": Priority.HIGH})
        self.assertEqual(len(self.sent), 1)
        tokens, data = self.sent[0]
        self.assertEqual(tokens, ["pa-phone-1"])  # Mansi's phone, not Sir's
        self.assertEqual((data["type"], data["kind"], data["priority"]), ("alert", "TASK_ASSIGNED", "HIGH"))
        self.assertIn("Courier the contract", data["spoken"])
        self.assertNotIn(SECRET, json.dumps(data))

    def test_token_moves_with_the_login_and_dead_tokens_are_dropped(self):
        from core.models import PushDevice
        self.md_client.post("/api/v1/me/push-token/", {"token": "shared-phone"}, format="json")
        self.pa_client.post("/api/v1/me/push-token/", {"token": "shared-phone"}, format="json")  # Mansi signs in on it
        self.assertEqual(PushDevice.objects.get(token="shared-phone").user, self.pa)
        self.md_client.post("/api/v1/me/push-token/", {"token": "dead-token"}, format="json")
        with self.captureOnCommitCallbacks(execute=True):
            services.create_md_request(self.pa, {"title": "Sign the vendor agreement", "kind": Kind.TASK, "priority": Priority.NORMAL})
        self.assertEqual(self.sent[-1][0], ["dead-token"])  # Sir's only phone
        self.assertFalse(PushDevice.objects.filter(token="dead-token").exists())  # FCM said it is gone for good
        # Signing out forgets the phone, so nothing is spoken on a phone he no longer uses.
        self.pa_client.post("/api/v1/me/push-token/", {"token": "pa-phone-2"}, format="json")
        r = self.pa_client.delete("/api/v1/me/push-token/", {"token": "pa-phone-2"}, format="json")
        self.assertEqual(r.json()["removed"], 1)
        self.assertEqual(self.md_client.post("/api/v1/me/push-token/", {"token": ""}, format="json").status_code, 422)

    def test_push_is_silent_when_not_configured(self):
        with mock.patch("core.push.configured", return_value=False):
            self.md_client.post("/api/v1/me/push-token/", {"token": "md-phone"}, format="json")
            with self.captureOnCommitCallbacks(execute=True):
                services.create_md_request(self.pa, {"title": "Book the Pune flights", "kind": Kind.TASK, "priority": Priority.NORMAL})
            self.assertEqual(self.md_client.get("/api/v1/health/").json()["push"], "off")
        self.assertEqual(self.sent, [])


def _workday(min_offset: int = 1):
    """A Monday-to-Friday date at least ``min_offset`` days ahead (so office hours and the Sunday off never interfere)."""
    d = tu.today_local() + timedelta(days=min_offset)
    while d.weekday() > 4:
        d += timedelta(days=1)
    return d


class SchedulingTests(BaseCase):
    """JARVIS looks at the diary before saving: clashes, free slots, full days, routine, reminder leads, briefing."""

    def turn(self, client, text, cmd, **extra):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = client.post("/api/v1/assistant/turn/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def interpret(self, client, text, cmd, **extra):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = client.post("/api/v1/assistant/interpret/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def _new_private(self, exclude):
        return PrivateTask.objects.filter(owner=self.md).exclude(id__in=[self.private.id, *exclude]).get()

    def _meeting_cmd(self, day, due_time="16:00", **extra):
        return _cmd(intent="create_task", title="Meeting with Sharma ji – Pune Franchise", kind="MEETING",
                    due_date=day.isoformat(), due_time=due_time, reply_language="hi",
                    spoken_reply="Sir, Sharma ji ke saath meeting 4 baje. Save kar doon?",
                    spoken_reply_hi="सर, शर्मा जी के साथ मीटिंग चार बजे। सेव कर दूँ?",
                    done_reply="Ho gaya Sir, 4 baje yaad dila dunga.", **extra)

    def test_clash_offers_the_next_free_slot_and_yes_takes_it(self):
        day = _workday()
        call = services.create_private_task(self.md, {"title": "Call Mehta ji – GST Notice", "kind": Kind.CALL,
                                                      "priority": Priority.NORMAL, "due_date": day, "due_time": time(16, 0),
                                                      "duration_minutes": 60})
        r = self.turn(self.md_client, "us din 4 baje Sharma ji ke saath meeting", self._meeting_cmd(day))
        self.assertEqual(r["action"], "private_task")
        self.assertIn("Call Mehta ji – GST Notice", r["show"])
        self.assertIn("or put this at 5 PM?", r["show"])
        self.assertEqual(r["say_lang"], "hi")
        self.assertIn("5 बजे रख दूँ?", r["say"])
        self.assertEqual(PrivateTask.objects.filter(owner=self.md).count(), 2)  # nothing saved yet
        r = self.turn(self.md_client, "isko 5 baje rakh do", _cmd(intent="confirm_pending", setting_value="move"))
        self.assertEqual(r["action"], "saved")
        task = self._new_private([call.id])
        self.assertEqual(tu.to_local(task.due_at).hour, 17)
        self.assertIn("5 PM", r["show"])  # the AI's "4 baje" done-line is dropped once the time moved

    def test_keeping_the_original_time_is_respected(self):
        day = _workday()
        call = services.create_private_task(self.md, {"title": "Call Mehta ji", "kind": Kind.CALL, "priority": Priority.NORMAL,
                                                      "due_date": day, "due_time": time(16, 0), "duration_minutes": 60})
        self.turn(self.md_client, "4 baje Sharma meeting", self._meeting_cmd(day))
        # Two meetings at one time are never saved, even on "keep it there": JARVIS asks again and saves nothing.
        r = self.turn(self.md_client, "nahi 4 hi rakho", _cmd(intent="confirm_pending", keep_original=True))
        self.assertNotEqual(r["action"], "saved")
        self.assertIn("cannot be at the same time", r["show"])
        self.assertEqual(PrivateTask.objects.filter(owner=self.md).exclude(id__in=[self.private.id, call.id]).count(), 0)

    def test_a_new_time_that_also_clashes_is_questioned_once_more(self):
        day = _workday()
        a = services.create_private_task(self.md, {"title": "Call Mehta ji", "kind": Kind.CALL, "priority": Priority.NORMAL,
                                                   "due_date": day, "due_time": time(16, 0), "duration_minutes": 60})
        b = services.create_private_task(self.md, {"title": "Call Verma ji", "kind": Kind.CALL, "priority": Priority.NORMAL,
                                                   "due_date": day, "due_time": time(18, 0)})
        self.turn(self.md_client, "4 baje Sharma meeting", self._meeting_cmd(day))
        r = self.turn(self.md_client, "6 baje rakho", _cmd(intent="confirm_pending", due_time="18:00"))
        self.assertEqual(r["action"], "private_task")  # asked again, same card
        self.assertIn("Call Verma ji", r["show"])
        self.assertEqual(PrivateTask.objects.filter(owner=self.md).count(), 3)
        r = self.turn(self.md_client, "theek hai", _cmd(intent="confirm_pending"))  # takes the slot offered this time
        self.assertEqual(r["action"], "saved")
        task = self._new_private([a.id, b.id])
        self.assertEqual(tu.to_local(task.due_at).hour, 10)  # no room left that day: first slot of the next working day
        self.assertEqual(tu.to_local(task.due_at).date(), day + timedelta(days=1))

    def test_meeting_without_a_time_is_offered_a_free_slot(self):
        day = _workday()
        early = services.create_private_task(self.md, {"title": "Meeting with Bank", "kind": Kind.MEETING, "priority": Priority.NORMAL,
                                                       "due_date": day, "due_time": time(10, 0)})
        r = self.turn(self.md_client, "us din Sharma ji ke saath meeting", self._meeting_cmd(day, due_time=""))
        self.assertIn("free 11 AM", r["show"])
        self.assertIn("Shall I put it at 11 AM?", r["show"])
        r = self.turn(self.md_client, "haan", _cmd(intent="confirm_pending"))
        self.assertEqual(r["action"], "saved")
        self.assertEqual(tu.to_local(self._new_private([early.id]).due_at).hour, 11)
        # "No time needed": kept exactly as said.
        self.turn(self.md_client, "parso Sharma meeting", self._meeting_cmd(day + timedelta(days=1), due_time=""))
        r = self.turn(self.md_client, "bina time ke rakho", _cmd(intent="confirm_pending", keep_original=True))
        self.assertEqual(r["action"], "saved")
        latest = PrivateTask.objects.filter(owner=self.md).order_by("-created_at").first()
        self.assertIsNone(latest.due_at)

    def test_full_day_is_mentioned_but_not_blocked(self):
        day = _workday(2)  # two days ahead: late in the evening the fixture's "in an hour" item can land on tomorrow
        for i in range(7):
            services.create_private_task(self.md, {"title": f"Errand {i}", "kind": Kind.TASK, "priority": Priority.NORMAL,
                                                   "due_date": day})
        draft = self.interpret(self.md_client, "us din 3 baje GST file", _cmd(
            intent="create_task", title="File GST Return", kind="TASK", due_date=day.isoformat(), due_time="15:00",
            spoken_reply="Sir, personal task: File GST Return at 3 PM. Shall I save it?"))
        self.assertEqual(draft["action"], "private_task")
        self.assertFalse(draft["needs_clarification"])
        self.assertTrue(draft["schedule"]["load"]["heavy"])
        self.assertIn("already has 7 items. Shall I save it?", draft["spoken_reply"])
        self.assertTrue(any("7 items" in w for w in draft["warnings"]))

    def test_routine_is_set_by_voice_and_keeps_time_free(self):
        r = self.turn(self.md_client, "office 10 se 7", _cmd(intent="set_setting", setting_key="officeHours", setting_value="10-19"))
        self.assertEqual(r["action"], "setting_applied")
        prefs = self.md_client.get("/api/v1/me/prefs/").json()
        self.assertEqual((prefs["officeStart"], prefs["officeEnd"]), ("10:00", "19:00"))
        self.assertEqual(self.md_client.patch("/api/v1/me/prefs/", {"officeHours": "9 to 6"}, format="json").json()["officeEnd"], "18:00")
        self.assertEqual(self.md_client.patch("/api/v1/me/prefs/", {"weeklyOff": "sat,sun"}, format="json").json()["weeklyOff"], [5, 6])
        self.assertEqual(self.md_client.patch("/api/v1/me/prefs/", {"weeklyOff": "none"}, format="json").json()["weeklyOff"], [])
        self.turn(self.md_client, "main roz 7 se 8 gym jaata hoon",
                  _cmd(intent="set_setting", setting_key="routineAdd", setting_value="Gym 07:00-08:00 daily"))
        blocks = self.md_client.get("/api/v1/me/prefs/").json()["routineBlocks"]
        self.assertEqual((blocks[0]["label"], blocks[0]["start"], blocks[0]["end"], len(blocks[0]["days"])), ("Gym", "07:00", "08:00", 7))
        blocks = self.md_client.patch("/api/v1/me/prefs/", {"routineAdd": "Market 11:00-13:00 wed"}, format="json").json()["routineBlocks"]
        self.assertEqual([b["label"] for b in blocks], ["Gym", "Market"])
        self.assertEqual(blocks[1]["days"], [2])
        # The gym block counts as busy time.
        day = _workday()
        r = self.turn(self.md_client, "us din saade 7 baje Mehta ji ko call", _cmd(
            intent="create_task", title="Call Mehta ji", kind="CALL", due_date=day.isoformat(), due_time="07:30",
            spoken_reply="Sir, call Mehta ji at 7:30 AM. Shall I save it?"))
        self.assertIn("you already have Gym then", r["show"])
        self.assertEqual(self.md_client.patch("/api/v1/me/prefs/", {"routineRemove": "gym"}, format="json").json()["routineBlocks"][0]["label"], "Market")
        self.assertEqual(self.md_client.patch("/api/v1/me/prefs/", {"routineRemove": "yoga"}, format="json").status_code, 422)
        self.assertEqual(self.md_client.patch("/api/v1/me/prefs/", {"weeklyOff": "funday"}, format="json").status_code, 422)
        # The PA's routine is her own.
        self.assertEqual(self.pa_client.get("/api/v1/me/prefs/").json()["officeEnd"], "19:00")

    def test_reminder_lead_times_come_from_settings(self):
        day = _workday()
        meeting = services.create_private_task(self.md, {"title": "Board Meeting", "kind": Kind.MEETING, "priority": Priority.NORMAL,
                                                         "due_date": day, "due_time": time(16, 0)})
        fixed = services.create_private_task(self.md, {"title": "Call Mehta ji", "kind": Kind.CALL, "priority": Priority.NORMAL,
                                                       "due_date": day, "due_time": time(12, 0), "remind_before_minutes": 45})
        dated = services.create_private_task(self.md, {"title": "Pay electricity bill", "kind": Kind.PAYMENT,
                                                       "priority": Priority.NORMAL, "due_date": day})
        self.assertIsNone(meeting.remind_before_minutes)  # not frozen at creation: the setting decides

        def fire(task_id, label):
            rems = self.md_client.get("/api/v1/reminders/?hours=168").json()["reminders"]
            item = next(r for r in rems if str(task_id) in r["key"] and r["label"] == label)
            return tu.to_local(datetime.fromisoformat(item["fire_at"])).strftime("%H:%M")

        self.assertEqual(fire(meeting.id, "before"), "15:45")
        self.assertEqual(fire(fixed.id, "before"), "11:15")
        self.assertEqual(fire(dated.id, "day"), "09:30")
        self.turn(self.md_client, "meeting reminder 30 minute pehle do",
                  _cmd(intent="set_setting", setting_key="remindBeforeMeeting", setting_value="30"))
        self.md_client.patch("/api/v1/me/prefs/", {"dayReminderTime": "8 am"}, format="json")
        self.assertEqual(fire(meeting.id, "before"), "15:30")   # every meeting follows the new setting
        self.assertEqual(fire(fixed.id, "before"), "11:15")     # an explicit lead stays as asked
        self.assertEqual(fire(dated.id, "day"), "08:00")

    def test_morning_briefing_is_raised_once_without_naming_private_items(self):
        from core import followups
        Alert.objects.all().delete()
        self.md_client.patch("/api/v1/me/prefs/", {"briefingTime": "08:00"}, format="json")
        self.pa_client.patch("/api/v1/me/prefs/", {"briefingEnabled": "off"}, format="json")
        at = tu.now_local().replace(hour=8, minute=10, second=0, microsecond=0)
        self.assertEqual(followups.run(now=at.replace(hour=7, minute=50)), 0)  # not yet
        raised = followups.run(now=at)
        self.assertEqual(raised, 1)
        alert = Alert.objects.get(recipient=self.md, ref_id__startswith="morning:")
        self.assertIn("Good morning, Sir", alert.spoken_text)
        self.assertIn("a personal item", alert.spoken_text)  # today's private meeting is counted, not named
        self.assertNotIn(SECRET, alert.spoken_text + alert.body + alert.title)
        self.assertFalse(Alert.objects.filter(recipient=self.pa, ref_id__startswith="morning:").exists())
        self.assertEqual(followups.run(now=at + timedelta(minutes=30)), 0)  # once a day
        self.assertEqual(followups.run(now=at.replace(hour=13)), 0)         # too late to be a morning briefing
        self.assertEqual(self.md_client.get("/api/v1/alerts/").json()["results"][0]["spoken_text"], alert.spoken_text)

    def test_pa_hears_only_that_sir_is_busy(self):
        day = _workday()
        services.create_private_task(self.md, {"title": SECRET + " hearing", "kind": Kind.MEETING, "priority": Priority.URGENT,
                                               "due_date": day, "due_time": time(16, 0)})
        r = self.turn(self.pa_client, "Sir se us din 4 baje meeting chahiye", _cmd(
            intent="request_for_md", title="Meeting with Sir – Vendor Review", kind="MEETING",
            due_date=day.isoformat(), due_time="16:00", spoken_reply="Priya, request to Sir: meeting at 4 PM. Send it?"))
        self.assertEqual(r["action"], "md_request")
        self.assertIn("Sir is busy then", r["show"])
        self.assertIn("5 PM instead?", r["show"])
        self.assertNotIn(SECRET, json.dumps(r))
        r = self.turn(self.pa_client, "haan", _cmd(intent="confirm_pending"))
        self.assertEqual(r["action"], "saved")
        req = TaskRequest.objects.filter(requested_by=self.pa).latest("created_at")
        self.assertEqual(req.payload["due_time"], "17:00")
        self.assert_no_secret(self.pa_client.get("/api/v1/alerts/"))

    def test_sir_is_told_when_mansi_is_already_busy(self):
        day = _workday()
        services.create_pa_task(self.md, {"title": "Prepare vendor file", "kind": Kind.TASK, "priority": Priority.NORMAL,
                                          "due_date": day, "due_time": time(15, 0), "duration_minutes": 60})
        r = self.turn(self.md_client, "Mansi ko bolo us din 3 baje courier bhej de", _cmd(
            intent="assign_to_pa", title="Courier the Contract", kind="TASK", due_date=day.isoformat(), due_time="15:00",
            spoken_reply="Sir, task for Priya: courier the contract at 3 PM. Shall I save it?"))
        self.assertEqual(r["action"], "pa_task")
        self.assertIn("Priya already has Prepare vendor file then", r["show"])

    def test_app_flow_returns_the_offered_time_as_overrides(self):
        day = _workday()
        services.create_private_task(self.md, {"title": "Call Mehta ji", "kind": Kind.CALL, "priority": Priority.NORMAL,
                                               "due_date": day, "due_time": time(16, 0), "duration_minutes": 60})
        draft = self.interpret(self.md_client, "4 baje Sharma meeting", self._meeting_cmd(day))
        self.assertEqual(draft["schedule"]["suggested"]["due_time"], "17:00")
        self.assertTrue(draft["schedule"]["question"])
        yes = self.interpret(self.md_client, "baad mein rakh do", _cmd(intent="confirm_pending", setting_value="move"),
                             previous="4 baje Sharma meeting", awaiting="Personal task: Meeting with Sharma ji", pending=draft)
        self.assertEqual(yes["action"], "confirm_pending")
        self.assertEqual(yes["overrides"], {"due_date": day.isoformat(), "due_time": "17:00"})
        keep = self.interpret(self.md_client, "4 hi rakho", _cmd(intent="confirm_pending", keep_original=True),
                              previous="4 baje Sharma meeting", awaiting="Personal task: Meeting with Sharma ji", pending=draft)
        self.assertEqual(keep["overrides"], {})
        # The model sees what JARVIS asked and the two times, so it can read the answer by meaning.
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="confirm_pending")) as fake:
            self.md_client.post("/api/v1/assistant/interpret/", {"text": "haan", "awaiting": "x", "pending": draft}, format="json")
        waiting = fake.call_args.args[1]["awaiting_confirmation"]
        self.assertEqual(waiting["offered_time"]["due_time"], "17:00")
        self.assertEqual(waiting["original_time"]["due_time"], "16:00")
        self.assertIn("or put this at", waiting["jarvis_asked"])


class SmartBrainTests(BaseCase):
    """JARVIS asks before it assumes: who a person is, whose task it is, and official vs personal for Sir."""

    def interpret(self, client, text, cmd, **extra):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = client.post("/api/v1/assistant/interpret/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def turn(self, client, text, cmd, **extra):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = client.post("/api/v1/assistant/turn/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def _aman(self, day, **extra):
        return _cmd(intent="create_task", title="Meeting with Aman", kind="MEETING", due_date=day.isoformat(),
                    due_time="17:00", people=["Aman"], reply_language="hi",
                    spoken_reply="Sir, Aman ke saath meeting 5 baje. Save kar doon?",
                    spoken_reply_hi="सर, अमन के साथ मीटिंग पाँच बजे। सेव कर दूँ?", **extra)

    # ---------------------------------------------------------------- who is this person?
    def test_unknown_person_is_questioned_then_remembered_for_the_speaker_only(self):
        from core.models import Contact
        day = _workday()
        first = self.interpret(self.md_client, "Aman ke saath 5 baje meeting book karo", self._aman(day))
        self.assertTrue(first["needs_clarification"])
        self.assertIn("I don't know any Aman", first["question"])
        self.assertEqual(first["spoken_reply"], first["question"])
        self.assertEqual(first["speak_lang"], "hi")          # asked in his language
        self.assertIn("Aman", first["speak_text"])
        self.assertEqual(first["people"], ["Aman"])
        self.assertEqual(Contact.objects.count(), 0)
        self.assertEqual(PrivateTask.objects.filter(owner=self.md).count(), 1)  # nothing booked with a stranger
        # He explains who Aman is: remembered, and the meeting carries on.
        history = [{"role": "user", "text": "Aman ke saath 5 baje meeting book karo"}, {"role": "jarvis", "text": first["question"]}]
        second = self.interpret(self.md_client, "bahar ka hai, hamara vendor", self._aman(
            day, new_contacts=[{"name": "aman", "note": "vendor"}]), previous=first["transcript"], history=history)
        self.assertFalse(second["needs_clarification"])
        self.assertEqual(second["action"], "answer")
        self.assertTrue(second["booked"])
        self.assertEqual(second["contacts_saved"], ["Aman"])
        self.assertEqual([(c["name"], c["note"]) for c in services.list_contacts(self.md)], [("Aman", "vendor")])
        # Next time nobody is asked, and the model is told who Aman is.
        with mock.patch("core.ai_brain.interpret", return_value=self._aman(day)) as fake:
            third = self.md_client.post("/api/v1/assistant/interpret/", {"text": "Aman ko kal call karna hai"}, format="json").json()
        self.assertFalse(third["needs_clarification"])
        self.assertEqual(fake.call_args.args[1]["contacts"], [{"name": "Aman", "who": "vendor"}])
        # Sir's contact is his alone: Mansi's JARVIS still does not know Aman, and her context has no trace of him.
        self.assertEqual(services.list_contacts(self.pa), [])
        self.assertNotIn("vendor", json.dumps(services.ai_context(self.pa), default=str))
        pa = self.interpret(self.pa_client, "Sir ke liye Aman ke saath meeting", _cmd(
            intent="request_for_md", title="Meeting with Aman", kind="MEETING", people=["Aman"]))
        self.assertIn("I don't know any Aman", pa["question"])

    def test_asked_once_then_taken_as_a_contact_even_if_the_model_forgets(self):
        day = _workday()
        first = self.interpret(self.md_client, "Aman ke saath meeting", self._aman(day))
        history = [{"role": "user", "text": "Aman ke saath meeting"}, {"role": "jarvis", "text": first["question"]}]
        second = self.interpret(self.md_client, "haan wahi", self._aman(day), previous=first["transcript"], history=history)
        self.assertFalse(second["needs_clarification"])      # never the same question twice
        self.assertEqual([c["name"] for c in services.list_contacts(self.md)], ["Aman"])
        self.assertTrue(any("Aman" in w for w in second["warnings"]))

    def test_known_people_pass_and_near_names_are_offered(self):
        day = _workday()
        services.create_private_task(self.md, {"title": "Call Sharma ji – Pune Franchise", "kind": Kind.CALL,
                                               "priority": Priority.NORMAL, "due_date": day, "due_time": time(11, 0)})
        for people in (["Neha"], ["Priya"], ["Sharma ji"], ["Anil Verma", "Suresh"], []):
            d = self.interpret(self.md_client, "meeting rakho", _cmd(
                intent="create_task", title="Review Meeting", kind="MEETING", due_date=day.isoformat(), due_time="15:00",
                people=people, spoken_reply="Sir, review meeting at 3 PM. Shall I save it?"))
            self.assertFalse(d["needs_clarification"], people)
        # Two people with that name on the staff: which one?
        d = self.interpret(self.md_client, "Ramesh ke saath meeting", _cmd(
            intent="create_task", title="Meeting with Ramesh", kind="MEETING", due_date=day.isoformat(), due_time="15:00",
            people=["Ramesh"]))
        self.assertEqual(d["question"], "Sir, which Ramesh: Ramesh Kumar from Accounts or Ramesh Yadav from Logistics?")
        # A near miss gets the closest known name offered.
        d = self.interpret(self.md_client, "Kavit ke saath meeting", _cmd(
            intent="create_task", title="Meeting with Kavit", kind="MEETING", due_date=day.isoformat(), due_time="15:00",
            people=["Kavit"]))
        self.assertIn("I don't know any Kavit", d["question"])
        self.assertIn("did you mean Kavita Singh from Franchise", d["question"])
        # The person who must DO the work is the delegation step's question, not a second one here.
        d = self.interpret(self.md_client, "Ramesh ko bolo Sharma ji ko call kare", _cmd(
            intent="assign_to_staff", staff_name="Ramesh", title="Call Sharma ji", people=["Ramesh", "Sharma ji"]))
        self.assertEqual(d["action"], "delegation")
        self.assertEqual(d["people"], ["Sharma ji"])
        self.assertIn("which Ramesh", d["question"])

    def test_sirs_private_titles_never_make_a_name_known_to_the_pa(self):
        # "Mehta" appears only in Sir's private meeting. For him the name is known; for Mansi it is a stranger,
        # and nothing in her answer shows that Sir has anything with a Mehta.
        day = _workday()
        cmd = _cmd(intent="create_task", title="Meeting with Mehta", kind="MEETING", due_date=day.isoformat(),
                   due_time="15:00", people=["Mehta"])
        self.assertFalse(self.interpret(self.md_client, "Mehta ke saath meeting", cmd)["needs_clarification"])
        with mock.patch("core.ai_brain.interpret", return_value={**cmd, "intent": "request_for_md"}):
            r = self.pa_client.post("/api/v1/assistant/interpret/", {"text": "Sir ke liye Mehta ke saath meeting"}, format="json")
        self.assertIn("I don't know any Mehta", r.json()["question"])
        self.assert_no_secret(r)

    # ---------------------------------------------------------------- whose task is it?
    def test_pa_task_is_never_assumed_to_be_for_sir(self):
        day = _workday()
        base = dict(title="Meeting with Neha", kind="MEETING", due_date=day.isoformat(), due_time="15:00", people=["Neha"])
        # She did not say whose it is, and the model guessed "for Sir": it is her own, never sent to Sir, and
        # nothing is asked (12 Nov 2026: the "whose is it?" question was unwanted).
        d = self.interpret(self.pa_client, "Neha ke saath 3 baje meeting book karo", _cmd(intent="request_for_md", **base))
        self.assertTrue(d["booked"])          # her own meeting, clear and free: booked at once
        self.assertTrue(SharedTask.objects.filter(title="Meeting with Neha", origin=SharedTask.Origin.PA_SELF).exists())
        self.assertFalse(TaskRequest.objects.filter(requested_by=self.pa).exists())
        d = self.interpret(self.pa_client, "kal bank jaana hai", _cmd(intent="create_task", title="Go to the Bank"))
        self.assertEqual(d["action"], "pa_self_task")
        # She said it: for Sir -> a request he accepts; her own -> her own list.
        d = self.interpret(self.pa_client, "Sir ke liye Neha ke saath 3 baje meeting", _cmd(intent="request_for_md", **base))
        self.assertEqual(d["action"], "md_request")
        d = self.interpret(self.pa_client, "mujhe kal bank jaana hai yaad dilana", _cmd(intent="create_task", title="Go to the Bank"))
        self.assertEqual(d["action"], "pa_self_task")
        self.assertIn("Your own task", d["spoken_reply"])
        # "Sir ki cab book karni hai" is HER job (the model judged it so, and she named Sir).
        d = self.interpret(self.pa_client, "Sir ki cab book karni hai kal", _cmd(intent="create_task", title="Book Cab for Sir"))
        self.assertEqual(d["action"], "pa_self_task")
        # A stranger AND nobody named as the owner: one combined question.
        d = self.interpret(self.pa_client, "Aman ke saath meeting book karo", _cmd(
            intent="needs_destination", title="Meeting with Aman", kind="MEETING", people=["Aman"],
            needs_clarification=True, clarification_question="Priya, is this for Sir, for you, or for someone in the team?"))
        self.assertEqual(d["action"], "pa_self_task")
        self.assertIn("I don't know any Aman", d["question"])
        self.assertNotIn("is this for Sir", d["question"])
        # Offline parser: her words decide, and the answer said last wins (no question loop).
        d = self.interpret(self.pa_client, "mere liye", _cmd(intent="create_task", kind="MEETING", title="Meeting", engine="rules"),
                           previous="Sir ke office mein meeting")
        self.assertEqual(d["action"], "pa_self_task")
        d = self.interpret(self.pa_client, "Sir ke liye", _cmd(intent="create_task", kind="MEETING", title="Meeting", engine="rules"),
                           previous="mera ek meeting hai")
        self.assertEqual(d["action"], "md_request")
        # Sir's side is unchanged: his own meeting is his personal item, nothing for him is ever a "request".
        before = PrivateTask.objects.filter(owner=self.md).count()
        d = self.interpret(self.md_client, "Neha ke saath 3 baje meeting", _cmd(intent="create_task", **base))
        self.assertTrue(d["booked"])  # clear and free: his personal meeting, booked at once
        self.assertEqual(PrivateTask.objects.filter(owner=self.md).count(), before + 1)
        self.assertFalse(TaskRequest.objects.exists())

    def test_pa_own_task_lives_on_her_list_and_sir_is_not_nagged(self):
        when = tu.now_local() + timedelta(minutes=20)
        body = {"title": "Collect courier from reception", "due_date": when.date().isoformat(), "due_time": when.strftime("%H:%M")}
        self.assertEqual(self.md_client.post("/api/v1/pa-tasks/self/", body, format="json", HTTP_IDEMPOTENCY_KEY="s0").status_code, 403)
        alerts_before = Alert.objects.filter(recipient=self.md).count()
        r = self.pa_client.post("/api/v1/pa-tasks/self/", body, format="json", HTTP_IDEMPOTENCY_KEY="s1")
        self.assertEqual(r.status_code, 201, r.content)
        task_id, version = r.json()["id"], r.json()["version"]
        self.assertEqual(r.json()["origin"], "PA_SELF")
        again = self.pa_client.post("/api/v1/pa-tasks/self/", body, format="json", HTTP_IDEMPOTENCY_KEY="s1")
        self.assertEqual(again.json()["id"], task_id)            # a retry never makes a second one
        self.assertEqual(SharedTask.objects.filter(origin="PA_SELF").count(), 1)
        self.assertEqual(IntegrationCommand.objects.count(), 0)  # stays inside JARVIS
        # It is on her list and she is reminded; Sir can see it but is not reminded or alerted.
        mine = self.pa_client.get("/api/v1/today/").json()
        self.assertIn(task_id, [i["id"] for i in mine["overdue"] + mine["today"] + mine["upcoming"] + mine["anytime"]])
        pa_keys = [x["key"] for x in self.pa_client.get("/api/v1/reminders/").json()["reminders"]]
        md_keys = [x["key"] for x in self.md_client.get("/api/v1/reminders/").json()["reminders"]]
        self.assertTrue(any(task_id in k for k in pa_keys))
        self.assertFalse(any(task_id in k for k in md_keys))
        self.assertIn(task_id, [t["id"] for t in self.md_client.get("/api/v1/today/").json()["pa_tasks"]])
        # Her own task: she changes it herself (no request to Sir) and finishes it without an alert to him.
        r = self.pa_client.patch(f"/api/v1/shared-tasks/{task_id}/", {"expected_version": version, "priority": "HIGH"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(TaskRequest.objects.count(), 0)
        r = self.pa_client.post(f"/api/v1/shared-tasks/{task_id}/complete/", {"expected_version": r.json()["version"]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Alert.objects.filter(recipient=self.md).count(), alerts_before)

    def test_pa_own_task_by_voice_and_done_by_voice(self):
        day = _workday()
        r = self.turn(self.pa_client, "mujhe us din 11 baje bank jaana hai", _cmd(
            intent="create_task", title="Go to the Bank", due_date=day.isoformat(), due_time="11:00"))
        self.assertEqual(r["action"], "pa_self_task")
        self.assertEqual(SharedTask.objects.filter(origin="PA_SELF").count(), 0)   # nothing before the yes
        r = self.turn(self.pa_client, "haan", _cmd(intent="confirm_pending"))
        self.assertEqual(r["action"], "saved")
        task = SharedTask.objects.get(origin="PA_SELF")
        self.assertEqual((task.title, task.created_by, task.assignee_type), ("Go to the Bank", self.pa, "PA"))
        self.assertEqual(TaskRequest.objects.count(), 0)         # it never went to Sir for approval
        r = self.turn(self.pa_client, "bank wala ho gaya", _cmd(intent="complete_task", target_refs=[str(task.id)]))
        self.assertEqual(r["action"], "complete")
        r = self.turn(self.pa_client, "haan", _cmd(intent="confirm_pending"))
        task.refresh_from_db()
        self.assertEqual(task.status, "COMPLETED")

    # ---------------------------------------------------------------- official vs personal
    def _official_task_for_sir(self):
        r = self.pa_client.post("/api/v1/requests/", {
            "title": "Sign Indore vendor agreement", "due_date": (tu.today_local() + timedelta(days=1)).isoformat(),
            "due_time": "12:00"}, format="json", HTTP_IDEMPOTENCY_KEY="off-1")
        self.md_client.post(f"/api/v1/requests/{r.json()['id']}/accept/", HTTP_IDEMPOTENCY_KEY="off-2")
        return SharedTask.objects.get(assignee_type="MD")

    def test_pending_is_split_into_official_and_personal_for_sir_only(self):
        official = self._official_task_for_sir()
        summary = services.ai_context(self.md)["pending_summary"]
        self.assertEqual((summary["total"], summary["official"], summary["personal"]), (2, 1, 1))
        locked = services.ai_context(self.md, include_private=False)["pending_summary"]
        self.assertEqual((locked["total"], locked["official"]), (1, 1))
        self.assertNotIn("personal", locked)                     # a locked phone does not even count them
        # The screen: both kinds, each row labelled; the official view has no personal row at all.
        view = self.md_client.get("/api/v1/views/pending/").json()
        self.assertEqual([s["label"] for s in view["stats"]], ["Official", "Personal", "Late", "Due today"])
        self.assertEqual({r["title"]: r["who"] for r in view["rows"]}, {SECRET: "Personal", official.title: "Official"})
        only = self.md_client.get("/api/v1/views/official/")
        self.assert_no_secret(only)
        self.assertEqual([r["title"] for r in only.json()["rows"]], [official.title])
        self.assertNotIn("Personal", [s["label"] for s in only.json()["stats"]])
        # The words pick the view when the model does not say.
        for text, kind in (("kya koi mere task pending hai", "pending"), ("official task kitne baaki hain", "official"),
                           ("mere personal task pending hain kya", "private")):
            d = self.interpret(self.md_client, text, _cmd(intent="query", spoken_reply="Sir, 2 pending hain."))
            self.assertEqual(d["view"]["kind"], kind, text)
        # Locked phone: the pending answer carries official rows only.
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="query", view_kind="pending", spoken_reply="x")) as fake:
            r = self.md_client.post("/api/v1/assistant/turn/", {"text": "kya pending hai", "locked": True, "private_ok": False}, format="json")
        self.assertNotIn(SECRET, json.dumps(r.json()))
        self.assertNotIn(SECRET, json.dumps(fake.call_args.args[1], default=str))

    def test_pa_has_no_personal_category_and_never_sees_sirs(self):
        self._official_task_for_sir()
        self.md_client.post("/api/v1/pa-tasks/", {"title": "Book flights for Pune visit"}, format="json", HTTP_IDEMPOTENCY_KEY="p1")
        self.pa_client.post("/api/v1/pa-tasks/self/", {"title": "Renew my ID card"}, format="json", HTTP_IDEMPOTENCY_KEY="p2")
        ctx = services.ai_context(self.pa)
        self.assertEqual(ctx["pending_summary"], {"total": 2, "late": 0, "due_today": 2, "later_or_no_date": 0, "from_sir": 1, "own": 1})
        self.assertNotIn(SECRET, json.dumps(ctx, default=str))
        for kind in ("pending", "official"):
            r = self.pa_client.get(f"/api/v1/views/{kind}/")
            self.assertEqual(r.status_code, 200, kind)
            self.assert_no_secret(r)
            self.assertNotIn("Personal", [s["label"] for s in r.json()["stats"]])
        view = self.pa_client.get("/api/v1/views/pending/").json()
        self.assertEqual({r["title"]: r["who"] for r in view["rows"]},
                         {"Book flights for Pune visit": "From Sir", "Renew my ID card": "Your own"})
        self.assertEqual(self.pa_client.get("/api/v1/views/private/").status_code, 404)
        # Sir's context marks which of her tasks are her own, and does not count them as work he gave.
        md_ctx = services.ai_context(self.md)
        self.assertEqual(md_ctx["pending_summary"]["with_pa_open"], 1)
        self.assertEqual(sorted(bool(t.get("her_own_task")) for t in md_ctx["tasks_given_to_pa"]), [False, True])

    def test_contacts_are_owner_only(self):
        r = self.md_client.post("/api/v1/contacts/", {"name": "sharma ji", "note": "our CA"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        cid = r.json()["id"]
        self.assertEqual(r.json()["name"], "Sharma Ji")
        self.md_client.post("/api/v1/contacts/", {"name": "Sharma", "note": "CA, Indore"}, format="json")  # same person: note updated
        mine = self.md_client.get("/api/v1/contacts/").json()["results"]
        self.assertEqual([(c["name"], c["note"]) for c in mine], [("Sharma Ji", "CA, Indore")])
        self.assertEqual(self.pa_client.get("/api/v1/contacts/").json()["results"], [])
        self.assertEqual(self.pa_client.delete(f"/api/v1/contacts/{cid}/").status_code, 404)
        self.assertEqual(self.md_client.post("/api/v1/contacts/", {"name": "  "}, format="json").status_code, 422)
        self.assertEqual(self.md_client.delete(f"/api/v1/contacts/{cid}/").status_code, 204)
        self.assertEqual(services.list_contacts(self.md), [])


class ListViewTests(BaseCase):
    """Any list he asks for: the people themselves, and anybody's tasks for any day. Fixed templates, real data."""

    def ask(self, client, text, **fields):
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="query", **fields)):
            r = client.post("/api/v1/assistant/interpret/", {"text": text}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def _give(self, name, title, day=None, key="g1"):
        staff = StaffMember.objects.get(name=name)
        body = {"title": title, "staff_id": staff.id, "priority": "NORMAL"}
        if day:
            body["due_date"] = day.isoformat()
        r = self.md_client.post("/api/v1/delegations/", body, format="json", HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["id"]

    # ---------------------------------------------------------------- the people
    def test_staff_list_is_the_people_not_their_work(self):
        self._give("Ramesh Kumar", "GST reconciliation")
        d = self.ask(self.md_client, "meri staff ki list dikhao", view_kind="people")
        view = d["view"]
        self.assertEqual((view["kind"], view["layout"]), ("people", "people"))
        names = [r["title"] for r in view["rows"]]
        self.assertIn("Neha Gupta", names)
        self.assertNotIn("GST reconciliation", names)                 # people, not the work given to them
        self.assertEqual(len(names), StaffMember.objects.filter(active=True).count())
        ramesh = next(r for r in view["rows"] if r["title"] == "Ramesh Kumar")
        self.assertEqual((ramesh["who"], ramesh["kind"], ramesh["assigned"]), ("Accounts", "Senior Accountant", 1))
        self.assertEqual([s["label"] for s in view["stats"]], ["People", "Teams", "On leave", "With work"])
        self.assertEqual(sum(p["value"] for p in view["series"]), len(names))   # head-count chart, team by team
        # The sentence comes from the real numbers, not from the model.
        self.assertEqual(d["spoken_reply"], view["summary"])
        self.assertTrue(d["spoken_reply"].startswith(f"Sir, you have {len(names)} staff in {len(view['series'])} teams"))
        # The words alone are enough when the model names no template.
        self.assertEqual(self.ask(self.md_client, "show me the list of my staff")["view"]["kind"], "people")
        self.assertEqual(self.ask(self.md_client, "staff ko kya kya diya hai")["view"]["kind"], "staff")
        # Mansi can ask too; nothing of Sir's is in it.
        r = self.pa_client.get("/api/v1/views/people/")
        self.assertEqual(r.status_code, 200)
        self.assert_no_secret(r)

    def test_people_by_team_on_leave_and_details_the_list_does_not_have(self):
        d = self.ask(self.md_client, "Accounts team mein kaun hai", view_kind="people", view_group="accounts team")
        self.assertEqual(d["view"]["title"], "Accounts team")
        self.assertEqual({r["who"] for r in d["view"]["rows"]}, {"Accounts"})
        self.assertIn("Accounts has", d["spoken_reply"])
        self.assertIn("Ramesh Kumar", d["spoken_reply"])
        # Who is on leave.
        StaffMember.objects.filter(name="Neha Gupta").update(on_leave=True, leave_until=tu.today_local() + timedelta(days=3))
        d = self.ask(self.md_client, "kaun chhutti par hai", view_kind="people", view_filter="on_leave")
        self.assertIn("Neha Gupta", [r["title"] for r in d["view"]["rows"]])
        self.assertTrue(all(r["on_leave"] for r in d["view"]["rows"]))
        self.assertIn("on leave: ", d["spoken_reply"])
        # "Show me the females": the list has no such detail. JARVIS says so and never guesses from names.
        d = self.ask(self.md_client, "staff mein females kaun hain", view_kind="people", view_missing="gender",
                     reply_language="hi", spoken_reply="Sir, ye rahi 3 mahilayen.", spoken_reply_hi="सर, ये रहीं तीन महिलाएँ।")
        self.assertIn("the staff list has no gender detail", d["spoken_reply"])
        self.assertNotIn("mahilayen", d["spoken_reply"])              # the model's own guess is not used
        self.assertEqual(len(d["view"]["rows"]), StaffMember.objects.filter(active=True).count())
        self.assertEqual(d["speak_lang"], "hi")
        self.assertIn("gender की जानकारी नहीं है", d["speak_text"])
        d = self.ask(self.md_client, "Marketing team dikhao", view_kind="people", view_group="Marketing")
        self.assertIn("couldn't find a team called Marketing", d["spoken_reply"])

    # ---------------------------------------------------------------- anybody's tasks, any day
    def test_anyones_tasks_for_any_day_come_from_one_template(self):
        today = tu.today_local()
        tomorrow = today + timedelta(days=1)
        self._give("Ramesh Kumar", "GST reconciliation", tomorrow, "g1")
        self._give("Neha Gupta", "Send offer letters", today + timedelta(days=3), "g2")
        one = self.ask(self.md_client, "Ramesh Kumar kal kya kar raha hai", view_kind="tasks", view_who="Ramesh Kumar",
                       view_from=tomorrow.isoformat(), view_to=tomorrow.isoformat())
        self.assertEqual(one["view"]["kind"], "tasks")
        self.assertEqual(one["view"]["title"], "Ramesh Kumar · Tomorrow")
        self.assertEqual([r["title"] for r in one["view"]["rows"]], ["GST reconciliation"])
        self.assertEqual(one["spoken_reply"], "Sir, Ramesh Kumar has one item tomorrow: GST reconciliation.")  # one item: said in full
        # Another person, another period: the same template (same numbers row, a bar for each day), other data.
        week = self.ask(self.md_client, "Neha is hafte kya kar rahi hai", view_kind="tasks", view_who="Neha",
                        view_from=today.isoformat(), view_to=(today + timedelta(days=6)).isoformat())
        self.assertEqual([r["title"] for r in week["view"]["rows"]], ["Send offer letters"])
        self.assertEqual([s["label"] for s in week["view"]["stats"]], [s["label"] for s in one["view"]["stats"]])
        self.assertEqual((len(week["view"]["series"]), sum(p["value"] for p in week["view"]["series"])), (7, 1))
        # "What did I give Ramesh today / yesterday": the day the work was assigned, not the day it is due.
        given = self.ask(self.md_client, "aaj maine Ramesh Kumar ko kya diya", view_kind="tasks", view_who="Ramesh Kumar",
                         view_from=today.isoformat(), view_to=today.isoformat(), view_by="given")
        self.assertEqual(len(given["view"]["rows"]), 1)
        self.assertIn("Ramesh Kumar was given one item today: GST reconciliation", given["spoken_reply"])
        before = today - timedelta(days=1)
        none = self.ask(self.md_client, "kal kya diya tha", view_kind="tasks", view_who="Ramesh Kumar",
                        view_from=before.isoformat(), view_to=before.isoformat(), view_by="given")
        self.assertEqual(none["view"]["rows"], [])
        self.assertEqual(none["spoken_reply"], "Sir, I found no items for Ramesh Kumar yesterday.")
        # All staff together: each row says whose it is. A standard view asked "for staff" lands on the same template.
        everyone = self.ask(self.md_client, "staff ke saare kaam", view_kind="tasks", view_who="staff")
        self.assertEqual({r["who"] for r in everyone["view"]["rows"]}, {"Ramesh Kumar", "Neha Gupta"})
        team = self.ask(self.md_client, "Accounts team ka kaam", view_kind="staff", view_who="Accounts team")
        self.assertEqual((team["view"]["title"], len(team["view"]["rows"])), ("Accounts team", 1))
        # One person's report: the same template again, with the on-time share.
        report = self.ask(self.md_client, "Neha ka performance", view_kind="performance", view_who="Neha")
        self.assertTrue(report["view"]["title"].startswith("Neha Gupta"))
        self.assertEqual([s["label"] for s in report["view"]["stats"]], ["Given", "Done", "Late", "On time"])
        self.assertIn("in the last 30 days", report["spoken_reply"])
        self.assertEqual(self.ask(self.md_client, "staff performance", view_kind="performance")["view"]["kind"], "performance")
        # Two people called Ramesh: ask which. Nobody by that name: say so and show who there is.
        which = self.ask(self.md_client, "Ramesh ke tasks", view_kind="tasks", view_who="Ramesh")
        self.assertTrue(which["needs_clarification"])
        self.assertIsNone(which["view"])
        self.assertEqual(which["question"], "Sir, which Ramesh: Ramesh Kumar from Accounts or Ramesh Yadav from Logistics?")
        self.assertEqual(self.md_client.get("/api/v1/views/tasks/?who=Ramesh").status_code, 422)
        nobody = self.ask(self.md_client, "Zubin ke tasks", view_kind="tasks", view_who="Zubin")
        self.assertEqual(nobody["view"]["kind"], "people")
        self.assertIn("I couldn't find Zubin on the staff list", nobody["spoken_reply"])

    def test_own_and_pa_lists_keep_the_privacy_wall(self):
        day = tu.to_local(self.private.due_at).date()
        span = {"view_from": day.isoformat(), "view_to": day.isoformat()}
        self.md_client.post("/api/v1/pa-tasks/", {"title": "Book flights for Pune visit", "due_date": day.isoformat()},
                            format="json", HTTP_IDEMPOTENCY_KEY="lv-1")
        # Sir's own day: his personal meeting is there for him, not in the official-only list, not on a locked phone.
        mine = self.ask(self.md_client, "us din mera kya hai", view_kind="tasks", view_who="me", **span)
        self.assertEqual(mine["view"]["title"].split(" · ")[0], "My tasks")
        self.assertIn(SECRET, [r["title"] for r in mine["view"]["rows"]])
        official = self.ask(self.md_client, "official kaam", view_kind="tasks", view_who="me", view_filter="official", **span)
        self.assertNotIn(SECRET, json.dumps(official))
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="query", view_kind="tasks", view_who="me", **span)):
            r = self.md_client.post("/api/v1/assistant/turn/", {"text": "mera kya hai", "locked": True, "private_ok": False}, format="json")
        self.assertNotIn(SECRET, json.dumps(r.json()))
        r = self.md_client.get(f"/api/v1/views/tasks/?who=me&from={day.isoformat()}&private=0")
        self.assertNotIn(SECRET, json.dumps(r.json()))
        # Mansi's list, as Sir sees it.
        hers = self.ask(self.md_client, "Priya us din kya kar rahi hai", view_kind="tasks", view_who="pa", **span)
        self.assertEqual(hers["view"]["title"].split(" · ")[0], "Priya's tasks")
        self.assertEqual([r["title"] for r in hers["view"]["rows"]], ["Book flights for Pune visit"])
        # Mansi asking: her own list; about Sir only his official work; "everyone" never opens his personal items.
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="query", view_kind="tasks", view_who="me", **span)):
            r = self.pa_client.post("/api/v1/assistant/interpret/", {"text": "us din mera kya hai"}, format="json")
        self.assertEqual([x["title"] for x in r.json()["view"]["rows"]], ["Book flights for Pune visit"])
        for who in ("sir", "all", "md", "boss"):
            with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="query", view_kind="tasks", view_who=who, **span)):
                r = self.pa_client.post("/api/v1/assistant/interpret/", {"text": "Sir us din kya kar rahe hain"}, format="json")
            self.assertEqual(r.status_code, 200, who)
            self.assert_no_secret(r)
            self.assert_no_secret(self.pa_client.get(f"/api/v1/views/tasks/?who={who}&from={day.isoformat()}"))
        self.assertEqual(self.pa_client.get(f"/api/v1/views/private/?from={day.isoformat()}").status_code, 404)
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="query", view_kind="tasks", view_who="sir", **span)):
            r = self.pa_client.post("/api/v1/assistant/interpret/", {"text": "Sir ka kya hai"}, format="json")
        self.assertEqual(r.json()["view"]["title"].split(" · ")[0], "Sir's official tasks")


class PersonAndScreenTests(BaseCase):
    """One person's own card (even when the name is heard a little wrong), and voice working the screen."""

    def ask(self, client, text, intent="query", **fields):
        extra = {k: fields.pop(k) for k in ("screen", "history") if k in fields}
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent=intent, **fields)) as fake:
            r = client.post("/api/v1/assistant/interpret/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json(), fake

    def _give(self, name, title, key):
        staff = StaffMember.objects.get(name=name)
        r = self.md_client.post("/api/v1/delegations/", {"title": title, "staff_id": staff.id, "priority": "NORMAL"},
                                format="json", HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(r.status_code, 201, r.content)

    def test_details_of_one_person_is_their_own_card_not_the_whole_list(self):
        self._give("Kavita Singh", "Call the Pune franchise owner", "pk-1")
        d, _ = self.ask(self.md_client, "Kavita Singh ki detail do", view_kind="person", view_who="Kavita Singh")
        view = d["view"]
        self.assertEqual((view["kind"], view["title"]), ("person", "Kavita Singh"))
        self.assertEqual(view["profile"], {"name": "Kavita Singh", "department": "Franchise",
                                           "designation": "Franchise Coordinator", "on_leave": False, "leave": ""})
        self.assertEqual([r["title"] for r in view["rows"]], ["Call the Pune franchise owner"])
        self.assertEqual([s["label"] for s in view["stats"]], ["Open", "Done", "Late", "On time"])
        self.assertEqual(d["spoken_reply"], "Sir, Kavita Singh is Franchise Coordinator in Franchise. 1 open; 0 done.")
        # The microphone heard the name a little wrong: still her, and the reply says whose card it is.
        for heard in ("Avitha Singh", "kavitha sinngh", "Kavitha"):
            d, _ = self.ask(self.md_client, f"give me detail of {heard}", view_kind="person", view_who=heard)
            self.assertEqual(d["view"]["title"], "Kavita Singh", heard)
        # The model filed it under "people" (the bug he hit): the person named still gets their own card.
        d, _ = self.ask(self.md_client, "give me detail of Kavita Singh", view_kind="people", view_who="Kavita Singh")
        self.assertEqual(d["view"]["kind"], "person")
        # A team name there is the team's list; no name at all is everyone.
        d, _ = self.ask(self.md_client, "Accounts ki detail", view_kind="person", view_who="Accounts")
        self.assertEqual((d["view"]["kind"], d["view"]["title"]), ("people", "Accounts team"))
        self.assertEqual(self.ask(self.md_client, "staff", view_kind="person")[0]["view"]["kind"], "people")
        # Someone on leave: said, and shown.
        d, _ = self.ask(self.md_client, "Nikki ke baare mein batao", view_kind="person", view_who="Nikki")
        self.assertTrue(d["view"]["profile"]["on_leave"])
        self.assertIn("on leave until", d["spoken_reply"])
        # Two people called Ramesh: which one. Nobody like that: the list, with a note.
        d, _ = self.ask(self.md_client, "Ramesh ki detail", view_kind="person", view_who="Ramesh")
        self.assertIn("which Ramesh", d["question"])
        d, _ = self.ask(self.md_client, "Zubin ki detail", view_kind="person", view_who="Zubin")
        self.assertEqual(d["view"]["kind"], "people")
        self.assertIn("couldn't find Zubin", d["spoken_reply"])
        # A tap on a name in the app asks the same thing.
        r = self.md_client.get("/api/v1/views/person/?who=Kavita%20Singh")
        self.assertEqual(r.json()["profile"]["designation"], "Franchise Coordinator")
        # Mansi's own card for Sir: her staff entry, and the work he gave her.
        self.md_client.post("/api/v1/pa-tasks/", {"title": "Book flights for Pune visit"}, format="json", HTTP_IDEMPOTENCY_KEY="pk-2")
        d, _ = self.ask(self.md_client, "Priya ki detail", view_kind="person", view_who="pa")
        self.assertEqual(d["view"]["kind"], "person")
        self.assertEqual([r["title"] for r in d["view"]["rows"]], ["Book flights for Pune visit"])

    def test_voice_works_the_screen(self):
        screen = {"kind": "people", "title": "Your staff", "items": [{"id": "7", "title": "Kavita Singh"}], "junk": "x" * 50}
        # What is on screen reaches the model (so "is list ka PDF" or a bare name makes sense), cleaned up.
        d, fake = self.ask(self.md_client, "is list ka PDF bana do", intent="screen_action", screen_action="export_pdf",
                           spoken_reply="Ji Sir, PDF bana raha hoon.", screen=screen)
        self.assertEqual(fake.call_args.args[1]["on_screen"], {"kind": "people", "title": "Your staff", "items": [{"id": "7", "title": "Kavita Singh"}]})
        self.assertEqual((d["action"], d["screen_action"]), ("screen", "export_pdf"))
        self.assertFalse(d["needs_clarification"])
        for act in ("export_csv", "share"):
            d, _ = self.ask(self.md_client, "share karo", intent="screen_action", screen_action=act, screen=screen)
            self.assertEqual((d["action"], d["screen_action"]), ("screen", act))
        # Nothing on screen: nothing to download, and JARVIS says so.
        d, _ = self.ask(self.md_client, "PDF bana do", intent="screen_action", screen_action="export_pdf")
        self.assertEqual((d["action"], d["screen_action"]), ("answer", ""))
        self.assertIn("no list on screen", d["spoken_reply"])
        # Going somewhere needs no card.
        d, _ = self.ask(self.md_client, "settings kholo", intent="screen_action", screen_action="open_settings")
        self.assertEqual((d["action"], d["screen_action"]), ("screen", "open_settings"))
        # "Open that one": only a task that really exists for him can be opened.
        d, _ = self.ask(self.md_client, "wo meeting kholo", intent="screen_action", screen_action="open_item",
                        target_ref=str(self.private.id))
        self.assertEqual((d["action"], d["screen_action"], d["target"]["id"]), ("screen", "open_item", str(self.private.id)))
        d, _ = self.ask(self.md_client, "wo kholo", intent="screen_action", screen_action="open_item", target_ref="made-up-id")
        self.assertEqual(d["action"], "answer")
        d, _ = self.ask(self.pa_client, "wo kholo", intent="screen_action", screen_action="open_item", target_ref=str(self.private.id))
        self.assertEqual(d["action"], "answer")                  # Sir's personal item is not hers to open
        self.assertNotIn(SECRET, json.dumps(d))
        # An action the app does not have is refused politely, never guessed.
        d, _ = self.ask(self.md_client, "screen ghumao", intent="screen_action", screen_action="")
        self.assertEqual(d["action"], "answer")
        # With the app closed (voice only) there is no screen to work.
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="screen_action", screen_action="open_settings")):
            r = self.md_client.post("/api/v1/assistant/turn/", {"text": "settings kholo"}, format="json")
        self.assertIn("please open JARVIS", r.json()["show"])

from django.utils import timezone  # noqa: E402


class ToldOnceTests(BaseCase):
    """Every message is told once, then tracked until seen: the rules both the app and the background part follow."""

    def _request(self, key="to-1"):
        r = self.pa_client.post("/api/v1/requests/", {"title": "Sign Indore vendor agreement"}, format="json", HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["id"]

    def test_alert_told_once_and_old_news_is_not_news(self):
        self._request()
        a = self.md_client.get("/api/v1/alerts/").json()["results"][0]
        self.assertEqual((a["kind"], a["fresh"], a["delivered_at"]), ("NEW_REQUEST", True, None))
        # The phone says it and reports so: it stays in the list, but nobody announces it again.
        r = self.md_client.post("/api/v1/told/", {"ids": [a["id"]]}, format="json")
        self.assertEqual(r.json()["alerts"], 1)
        again = self.md_client.get("/api/v1/alerts/").json()["results"][0]
        self.assertEqual(again["id"], a["id"])
        self.assertFalse(again["fresh"])
        self.assertIsNotNone(again["delivered_at"])
        self.assertEqual(self.md_client.post("/api/v1/told/", {"ids": [a["id"]]}, format="json").json()["alerts"], 0)
        # Mansi cannot mark Sir's alerts as told.
        Alert.objects.filter(id=a["id"]).update(delivered_at=None)
        self.assertEqual(self.pa_client.post("/api/v1/told/", {"ids": [a["id"]]}, format="json").json()["alerts"], 0)
        # Older than a day: never news. Older than a week: not even listed.
        Alert.objects.filter(id=a["id"]).update(created_at=timezone.now() - timedelta(hours=30))
        self.assertFalse(self.md_client.get("/api/v1/alerts/").json()["results"][0]["fresh"])
        Alert.objects.filter(id=a["id"]).update(created_at=timezone.now() - timedelta(days=8))
        self.assertEqual(self.md_client.get("/api/v1/alerts/").json()["results"], [])

    def test_reminder_told_once(self):
        when = tu.now_local() + timedelta(minutes=20)
        services.create_private_task(self.md, {"title": "Call the bank", "kind": Kind.CALL, "priority": Priority.NORMAL,
                                               "due_date": when.date(), "due_time": when.time().replace(second=0, microsecond=0)})
        rems = self.md_client.get("/api/v1/reminders/").json()["reminders"]
        due = next(r for r in rems if r["label"] == "due" and r["title"] == "Call the bank")
        self.assertIsNone(due["told_at"])
        self.assertEqual(self.md_client.post("/api/v1/told/", {"keys": [due["key"]]}, format="json").json()["reminders"], 1)
        rems = self.md_client.get("/api/v1/reminders/").json()["reminders"]
        again = next(r for r in rems if r["key"] == due["key"])
        self.assertIsNotNone(again["told_at"])             # still listed (not seen), but marked as told
        self.md_client.post("/api/v1/reminders/ack/", {"key": due["key"]}, format="json")
        self.assertNotIn(due["key"], [r["key"] for r in self.md_client.get("/api/v1/reminders/").json()["reminders"]])

    def test_no_reminder_for_a_time_already_past_when_the_task_was_made(self):
        # "Kal 4 baje" said at 3:55: no "15 minutes before" (it is already gone), only "it's time" and the check-in.
        when = tu.now_local() + timedelta(minutes=5)
        services.create_private_task(self.md, {"title": "Review the deck", "kind": Kind.MEETING, "priority": Priority.NORMAL,
                                               "due_date": when.date(), "due_time": when.time().replace(second=0, microsecond=0)})
        labels = [r["label"] for r in self.md_client.get("/api/v1/reminders/").json()["reminders"] if r["title"] == "Review the deck"]
        self.assertNotIn("before", labels)
        self.assertIn("due", labels)
        # A task written down after its time has passed is not "missed": no reminder at all for that moment.
        gone = tu.now_local() - timedelta(minutes=30)
        services.create_private_task(self.md, {"title": "Already Done Meeting", "kind": Kind.MEETING, "priority": Priority.NORMAL,
                                               "due_date": gone.date(), "due_time": gone.time().replace(second=0, microsecond=0)})
        labels = [r["label"] for r in self.md_client.get("/api/v1/reminders/").json()["reminders"] if r["title"] == "Already Done Meeting"]
        self.assertNotIn("due", labels)
        self.assertNotIn("before", labels)

    def test_check_in_once_after_a_timed_task_is_still_open(self):
        # Due 45 minutes ago in working hours: one check-in, worded as a choice. Mansi gets it for her own tasks;
        # Sir does not get it about her tasks (only about his, and about meetings with her).
        # The latest full hour whose check-in (45 minutes later) has already passed, is in working hours and is
        # still inside the reminder list's 12-hour look-back; whatever the clock says when the suite runs.
        from core.reminders import CHECKIN_HOURS, LOOKBACK
        now = tu.now_local()
        at = now.replace(minute=0, second=0, microsecond=0)
        while at.hour not in CHECKIN_HOURS or at + timedelta(minutes=45) > now:
            at -= timedelta(hours=1)
        if now - (at + timedelta(minutes=45)) > LOOKBACK:
            self.skipTest("no working-hour check-in inside the look-back at this time of day")
        services.create_private_task(self.md, {"title": "Call Mehta ji", "kind": Kind.CALL, "priority": Priority.NORMAL,
                                               "due_date": at.date(), "due_time": at.time()})
        rems = self.md_client.get("/api/v1/reminders/?hours=48").json()["reminders"]
        check = [r for r in rems if r["label"] == "checkin" and r["title"] == "Call Mehta ji"]
        self.assertEqual(len(check), 1)
        self.assertEqual(datetime.fromisoformat(check[0]["fire_at"]), at + timedelta(minutes=45))
        self.assertEqual(check[0]["spoken"], f"Sir, your call Call Mehta ji was due at {tu.fmt_time(at)} and is still open. Done, move it, or leave it?")
        self.md_client.post("/api/v1/pa-tasks/", {"title": "Book flights", "due_date": at.date().isoformat(), "due_time": "11:00"},
                            format="json", HTTP_IDEMPOTENCY_KEY="ci-1")
        hers = [r["label"] for r in self.pa_client.get("/api/v1/reminders/?hours=48").json()["reminders"] if r["title"] == "Book flights"]
        self.assertIn("checkin", hers)
        his = [r for r in self.md_client.get("/api/v1/reminders/?hours=48").json()["reminders"] if "Book flights" in r["title"]]
        self.assertNotIn("checkin", [r["label"] for r in his])
        # No check-in in the evening or at night (left to the summary and the morning briefing).
        late = tu.now_local().replace(hour=19, minute=30, second=0, microsecond=0)
        if late > tu.now_local():
            late -= timedelta(days=1)
        services.create_private_task(self.md, {"title": "Night Thing", "kind": Kind.TASK, "priority": Priority.NORMAL,
                                               "due_date": late.date(), "due_time": late.time()})
        rems = self.md_client.get("/api/v1/reminders/?hours=48").json()["reminders"]
        self.assertEqual([r["label"] for r in rems if r["title"] == "Night Thing" and r["label"] == "checkin"], [])

    def test_finishing_a_task_clears_everything_about_it(self):
        r = self.md_client.post("/api/v1/pa-tasks/", {"title": "Book flights for Pune visit"}, format="json", HTTP_IDEMPOTENCY_KEY="cl-1")
        task_id, version = r.json()["id"], r.json()["version"]
        self.assertEqual(Alert.objects.filter(recipient=self.pa, ref_id=task_id, acknowledged_at__isnull=True).count(), 1)
        self.pa_client.post(f"/api/v1/shared-tasks/{task_id}/complete/", {"expected_version": version}, format="json")
        # Her "new task for you" alert is gone (nothing left to do), while his "Mansi has completed" news stays.
        left = list(Alert.objects.filter(ref_id=task_id, acknowledged_at__isnull=True))
        self.assertEqual([(a.recipient_id, a.kind) for a in left], [(self.md.id, "STAFF_UPDATE")])
        self.assertEqual([a for a in self.pa_client.get("/api/v1/alerts/").json()["results"] if a["ref_id"] == task_id], [])
        self.assertTrue(any(a["ref_id"] == task_id and a["fresh"] for a in self.md_client.get("/api/v1/alerts/").json()["results"]))

    def test_evening_summary_and_weekly_no_date_note_for_both(self):
        from core import followups
        yesterday = tu.today_local() - timedelta(days=1)
        self.md_client.post("/api/v1/pa-tasks/", {"title": "Chase the courier", "due_date": yesterday.isoformat()},
                            format="json", HTTP_IDEMPOTENCY_KEY="ev-1")
        self.md_client.post("/api/v1/pa-tasks/", {"title": "Someday file"}, format="json", HTTP_IDEMPOTENCY_KEY="ev-2")
        services.create_private_task(self.md, {"title": "No date yet", "kind": Kind.TASK, "priority": Priority.NORMAL})
        evening = tu.now_local().replace(hour=18, minute=45)
        # Make the evening fall on a Monday so the weekly note runs in the same pass.
        while evening.weekday() != 0:
            evening += timedelta(days=1)
        followups.run(evening)
        texts = [a.spoken_text for a in Alert.objects.filter(recipient=self.pa, ref_type="followup")]
        self.assertTrue(any("slipped today: Chase the courier" in t for t in texts), texts)
        self.assertTrue(any("no date: Someday file" in t for t in texts), texts)
        mine = [a.spoken_text for a in Alert.objects.filter(recipient=self.md, ref_type="followup")]
        self.assertTrue(any("no date" in t and "private" in t for t in mine), mine)
        self.assertNotIn("No date yet", " ".join(mine))         # a private title is never written into an alert
        # Run again: nothing is raised twice.
        before = Alert.objects.filter(ref_type="followup").count()
        followups.run(evening + timedelta(minutes=5))
        self.assertEqual(Alert.objects.filter(ref_type="followup").count(), before)


class TodayAndNarrowingTests(BaseCase):
    """"Today" means today (late items are a separate tail), any list narrows down to one item, and the
    sentence for every list is written by the server from the real numbers."""

    def setUp(self):
        super().setUp()
        self.today = tu.today_local()
        self.yesterday = self.today - timedelta(days=1)
        self.tomorrow = self.today + timedelta(days=1)
        soon = tu.now_local() + timedelta(hours=2)
        self.meeting_time = soon.strftime("%H:%M")
        self.meeting_day = soon.date()
        # Sir's meeting with Priya in two hours (60 minutes), a task for her due today, one of hers that slipped yesterday.
        self.md_client.post("/api/v1/pa-tasks/", {"title": "Meeting with Priya – Weekly Review", "meeting_with_pa": True, "kind": "MEETING",
                                                   "due_date": self.meeting_day.isoformat(), "due_time": self.meeting_time, "duration_minutes": 60},
                            format="json", HTTP_IDEMPOTENCY_KEY="tn-meet")
        self.md_client.post("/api/v1/pa-tasks/", {"title": "Book the Pune flights", "due_date": self.today.isoformat()},
                            format="json", HTTP_IDEMPOTENCY_KEY="tn-pune")
        self.md_client.post("/api/v1/pa-tasks/", {"title": "Courier the contract", "due_date": self.yesterday.isoformat()},
                            format="json", HTTP_IDEMPOTENCY_KEY="tn-late")
        self.md_client.post("/api/v1/pa-tasks/", {"title": "Call the auditor", "kind": "CALL", "due_date": self.tomorrow.isoformat(),
                                                   "due_time": "11:00"}, format="json", HTTP_IDEMPOTENCY_KEY="tn-call")
        # Sir's own late payment (personal), beside the urgent private meeting BaseCase makes for today.
        services.create_private_task(self.md, {"title": "Pay the electricity bill", "kind": Kind.PAYMENT, "priority": Priority.NORMAL,
                                               "due_date": self.yesterday})

    def ask(self, client, text, **fields):
        extra = fields.pop("_extra", {})
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="query", **fields)) as fake:
            r = client.post("/api/v1/assistant/interpret/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json(), fake

    def test_today_lists_only_today_and_says_late_as_a_tail(self):
        if self.meeting_day != self.today:
            self.skipTest("too close to midnight for a same-day meeting")
        # Mansi: her two things for today, not the one that slipped yesterday; the late one is one short tail.
        d, _ = self.ask(self.pa_client, "do I have anything to do today", view_kind="today", spoken_reply="Priya, 3 tasks.")
        titles = [r["title"] for r in d["view"]["rows"]]
        self.assertIn("Meeting with Priya – Weekly Review", titles)
        self.assertIn("Book the Pune flights", titles)
        self.assertNotIn("Courier the contract", titles)
        self.assertEqual(next(s["value"] for s in d["view"]["stats"] if s["label"] == "Late"), 1)
        self.assertTrue(d["spoken_reply"].startswith("Priya, 2 items today: Meeting with Priya – Weekly Review at "), d["spoken_reply"])
        self.assertTrue(d["spoken_reply"].endswith(" 1 late from before."), d["spoken_reply"])
        self.assertNotIn("3 tasks", d["spoken_reply"])                                  # the model's own count is never used
        self.assertEqual(d["view"]["request"]["kind"], "today")
        # Sir: his urgent private meeting and the meeting with Priya are today; yesterday's bill is the tail.
        d, _ = self.ask(self.md_client, "aaj kya hai", view_kind="today")
        titles = [r["title"] for r in d["view"]["rows"]]
        self.assertIn(SECRET, titles)
        self.assertNotIn("Pay the electricity bill", titles)
        self.assertIn("1 late from before.", d["spoken_reply"])
        # The same question as a period ("tasks for today") also leaves the late one out; "late wale" lists it.
        span = {"view_from": self.today.isoformat(), "view_to": self.today.isoformat()}
        d, _ = self.ask(self.pa_client, "aaj ke tasks", view_kind="tasks", view_who="me", **span)
        self.assertNotIn("Courier the contract", [r["title"] for r in d["view"]["rows"]])
        d, _ = self.ask(self.pa_client, "late wale dikhao", view_kind="late")
        self.assertEqual([r["title"] for r in d["view"]["rows"]], ["Courier the contract"])
        self.assertEqual(d["spoken_reply"], "Priya, 1 item is late: Courier the contract (yesterday).")

    def test_narrows_to_the_one_meeting_sir_gave(self):
        if self.meeting_day != self.today:
            self.skipTest("too close to midnight for a same-day meeting")
        span = {"view_from": self.today.isoformat(), "view_to": self.today.isoformat()}
        d, _ = self.ask(self.pa_client, "today's meeting that Sir gave me", view_kind="today", view_who="me",
                        view_types=["MEETING"], view_from_whom="sir", **span)
        self.assertEqual([r["title"] for r in d["view"]["rows"]], ["Meeting with Priya – Weekly Review"])
        self.assertTrue(d["spoken_reply"].startswith("Priya, you have one meeting from Sir today: Meeting with Priya – Weekly Review at "), d["spoken_reply"])
        self.assertTrue(d["spoken_reply"].endswith(", 60 minutes."), d["spoken_reply"])
        self.assertEqual(d["view"]["request"]["types"], ["MEETING"])
        self.assertEqual(d["view"]["request"]["from_whom"], "sir")
        # In Hindi the same sentence is spoken from the Hindi twin, never from the model's own text.
        d, _ = self.ask(self.pa_client, "Sir ne aaj jo meeting di", view_kind="tasks", view_who="me", view_types=["MEETING"],
                        view_from_whom="sir", reply_language="hi", spoken_reply="Priya, 7 kaam.", spoken_reply_hi="प्रिया, सात काम।", **span)
        self.assertEqual(d["speak_lang"], "hi")
        self.assertIn("एक मीटिंग है: Meeting with Priya – Weekly Review", d["speak_text"])
        self.assertNotIn("सात काम", d["speak_text"])
        # Words from the title pick one item; the priority filter picks Sir's urgent one; pointing at an item works too.
        d, _ = self.ask(self.pa_client, "Pune wala task", view_kind="tasks", view_who="me", view_words="Pune")
        self.assertEqual(d["spoken_reply"], "Priya, you have one item about Pune: Book the Pune flights today.")
        d, _ = self.ask(self.md_client, "koi urgent kaam hai", view_kind="pending", view_priority="urgent")
        self.assertEqual([r["title"] for r in d["view"]["rows"]], [SECRET])
        self.assertTrue(d["spoken_reply"].startswith("Sir, you have one urgent item: "), d["spoken_reply"])
        pune = SharedTask.objects.get(title="Book the Pune flights")
        screen = {"kind": "tasks", "title": "My tasks", "items": [{"id": str(pune.id), "title": pune.title}]}
        d, _ = self.ask(self.pa_client, "ye wala kab tak hai", view_kind="tasks", view_who="me", target_refs=[str(pune.id)],
                        _extra={"screen": screen})
        self.assertEqual([r["id"] for r in d["view"]["rows"]], [str(pune.id)])
        # An id JARVIS never showed is ignored, never looked up.
        d, _ = self.ask(self.pa_client, "wo wala", view_kind="tasks", view_who="me", target_refs=[str(self.private.id)], _extra={"screen": screen})
        self.assertNotIn(SECRET, json.dumps(d))

    def test_nothing_in_the_period_names_the_next_one(self):
        span = {"view_from": self.today.isoformat(), "view_to": self.today.isoformat()}
        d, _ = self.ask(self.pa_client, "aaj koi call hai", view_kind="today", view_who="me", view_types=["CALL"], **span)
        self.assertEqual(d["view"]["rows"], [])
        self.assertEqual(d["spoken_reply"], "Priya, I found no calls on your list today. The next one is Call the auditor tomorrow at 11 AM.")
        # A past period simply has nothing (no "next one" for yesterday).
        before = {"view_from": self.yesterday.isoformat(), "view_to": self.yesterday.isoformat()}
        d, _ = self.ask(self.pa_client, "kal koi call thi", view_kind="tasks", view_who="me", view_types=["CALL"], **before)
        self.assertEqual(d["spoken_reply"], "Priya, I found no calls on your list yesterday.")

    def test_follow_ups_start_from_the_last_list(self):
        # The first answer is remembered; the next question's context carries its filters as last_list.
        d, _ = self.ask(self.pa_client, "aaj kya hai", view_kind="today")
        self.assertEqual(d["view"]["request"], {"kind": "today", "from": self.today.isoformat(), "to": self.today.isoformat()})
        _, fake = self.ask(self.pa_client, "sirf meetings", view_kind="tasks", view_types=["MEETING"],
                           view_from=self.today.isoformat(), view_to=self.today.isoformat())
        ctx = fake.call_args[0][1]
        self.assertEqual(ctx["last_list"]["filters"]["kind"], "today")
        self.assertTrue(ctx["last_list"]["jarvis_said"].startswith("Priya, "))
        # When the phone sends the card with its filters, those are what the model sees (on_screen.filters).
        screen = {"kind": "tasks", "title": "My meetings · Today", "items": [],
                  "filters": {"kind": "tasks", "from": self.today.isoformat(), "to": self.today.isoformat(), "types": ["MEETING"], "junk": "x"}}
        _, fake = self.ask(self.pa_client, "sab dikhao", view_kind="tasks", _extra={"screen": screen})
        ctx = fake.call_args[0][1]
        self.assertEqual(ctx["on_screen"]["filters"], {"kind": "tasks", "from": self.today.isoformat(), "to": self.today.isoformat(), "types": ["MEETING"]})
        self.assertNotIn("last_list", ctx)
        # The voice-only conversation keeps its own memory beside it.
        from core.models import ConversationState
        self.assertIn("last_view", json.loads(ConversationState.objects.get(user=self.pa).state_json))

    def test_seen_answer_tells_sir_once(self):
        # "Shall I tell Sir you've seen it?" — yes: one note for Sir, spoken only when the task matters.
        self.md_client.post("/api/v1/pa-tasks/", {"title": "Sign the Pune vendor agreement", "priority": "URGENT"},
                            format="json", HTTP_IDEMPOTENCY_KEY="seen-1")
        hers = [a for a in self.pa_client.get("/api/v1/alerts/").json()["results"] if a["kind"] == "TASK_ASSIGNED"
                and "Pune vendor" in (a["body"] + a["title"] + a["spoken_text"])]
        self.assertTrue(hers)
        r = self.pa_client.post(f"/api/v1/alerts/{hers[0]['id']}/ack/", {"seen": True}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(r.json()["told"])
        self.pa_client.post(f"/api/v1/alerts/{hers[0]['id']}/ack/", {"seen": True}, format="json")  # again: no second note
        his = [a for a in self.md_client.get("/api/v1/alerts/").json()["results"] if a["ref_type"] == "seen"]
        self.assertEqual(len(his), 1)
        self.assertEqual(his[0]["spoken_text"], "Sir, Priya has seen: Sign the Pune vendor agreement.")
        self.assertEqual(his[0]["priority"], "NORMAL")   # urgent task: said aloud
        # A plain ack says nothing to Sir; the MD cannot "see" his own alerts into a note either.
        self.md_client.post("/api/v1/pa-tasks/", {"title": "Order water cans"}, format="json", HTTP_IDEMPOTENCY_KEY="seen-2")
        plain = [a for a in self.pa_client.get("/api/v1/alerts/").json()["results"] if "water cans" in a["body"] + a["title"]]
        self.pa_client.post(f"/api/v1/alerts/{plain[0]['id']}/ack/", {}, format="json")
        self.assertEqual(len([a for a in self.md_client.get("/api/v1/alerts/").json()["results"] if a["ref_type"] == "seen"]), 1)
        mine = self.md_client.get("/api/v1/alerts/").json()["results"]
        r = self.md_client.post(f"/api/v1/alerts/{mine[0]['id']}/ack/", {"seen": True}, format="json")
        self.assertIsNone(r.json()["told"])

    def test_names_come_from_the_server_and_clear_data_keeps_the_accounts(self):
        from io import StringIO

        from django.core.management import call_command
        from django.core.management.base import CommandError
        from core.models import ConversationState, Memory, TaskPanelLink

        # Both names ride on the dashboard, so no screen has to carry a typed-in name.
        dash = self.md_client.get("/api/v1/today/").json()
        self.assertEqual((dash["pa_name"], dash["md_name"]), ("Priya", "Sir"))
        self.assertEqual([s["label"] for s in self.md_client.get("/api/v1/views/stats/").json()["stats"]][4], "With Priya")
        # Clearing needs --yes; then every task, request, alert and memory goes, and the accounts, links and settings stay.
        Memory.objects.create(owner=self.md, text="City is Indore.")
        self.md_client.patch("/api/v1/me/prefs/", {"meetingMode": "on"}, format="json")
        self.assertTrue(PrivateTask.objects.exists() and SharedTask.objects.exists() and Alert.objects.exists())
        with self.assertRaises(CommandError):
            call_command("clear_data", stdout=StringIO())
        out = StringIO()
        call_command("clear_data", "--yes", stdout=out)
        self.assertIn("Cleared", out.getvalue())
        for model in (PrivateTask, SharedTask, TaskRequest, Alert, Memory, ConversationState):
            self.assertEqual(model.objects.count(), 0, model.__name__)
        self.assertEqual(User.objects.count(), 2)
        self.assertEqual(TaskPanelLink.objects.filter(active=True).count(), 2)
        self.assertTrue(StaffMember.objects.filter(active=True).exists())
        self.assertTrue(self.md_client.get("/api/v1/me/prefs/").json()["meetingMode"])  # settings kept
        self.assertEqual(self.md_client.get("/api/v1/today/").json()["today"], [])

    def test_the_md_chooses_how_she_is_addressed(self):
        # Default: Sir everywhere. Then the MD says she is Ma'am: every spoken line, label and greeting follows,
        # the PA's JARVIS refers to her the same way, and her own name is understood as "the MD".
        self.assertEqual(self.md_client.get("/api/v1/me/").json()["address"], "Sir")
        self.md_client.patch("/api/v1/me/prefs/", {"addressAs": "Ma'am"}, format="json")
        self.md.display_name = "Nancy"
        self.md.save(update_fields=["display_name"])
        me = self.md_client.get("/api/v1/me/").json()
        self.assertEqual((me["address"], me["address_hi"], me["name"]), ("Ma'am", "मैम", "Nancy"))
        login = APIClient().post("/api/v1/auth/login/", {"username": "md", "password": "md-pass-12345"}, format="json").json()
        self.assertEqual(login["user"]["address"], "Ma'am")
        # Her phone and the PA's both learn the word from the dashboard.
        self.assertEqual(self.pa_client.get("/api/v1/today/").json()["md_address"], "Ma'am")
        # Spoken lines: a reminder for her, the PA's "waiting for" view, the closed-app greeting, the PA's new-task alert.
        rem = self.md_client.get("/api/v1/reminders/?hours=48").json()["reminders"]
        self.assertTrue(rem and all(r["spoken"].startswith("Ma'am, ") for r in rem), rem)
        self.md_client.post("/api/v1/pa-tasks/", {"title": "Courier the Pune file"}, format="json", HTTP_IDEMPOTENCY_KEY="addr-1")
        hers = [a for a in self.pa_client.get("/api/v1/alerts/").json()["results"] if a["kind"] == "TASK_ASSIGNED"]
        self.assertTrue(hers and hers[0]["title"] == "New task from Ma'am" and "Ma'am has given you" in hers[0]["spoken_text"], hers)
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="query", view_kind="requests")):
            r = self.pa_client.post("/api/v1/assistant/interpret/", {"text": "kya waiting hai"}, format="json").json()
        self.assertEqual(r["view"]["title"], "Waiting for Ma'am")
        self.assertIn("waiting for Ma'am", r["spoken_reply"])
        r = self.md_client.post("/api/v1/assistant/turn/", {"text": "", "event": "wake"}, format="json").json()
        self.assertEqual((r["show"], r["say"]), ("Ji Ma'am?", "जी मैम?"))
        # "Nancy ke tasks" / "Ma'am ke tasks" from the PA both mean the MD's official list.
        from core import listviews
        for word in ("nancy", "ma'am", "madam"):
            self.assertEqual(listviews.resolve_subject(self.pa, word)["type"], "md", word)
        self.assertEqual(listviews.resolve_subject(self.pa, "ma'am")["label"], "Ma'am")
        # The AI is told the word on every turn, so its own sentences use it too.
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="small_talk", spoken_reply="Hello Ma'am")) as fake:
            self.md_client.post("/api/v1/assistant/interpret/", {"text": "hello"}, format="json")
        self.assertEqual((fake.call_args[0][1]["md_address"], fake.call_args[0][1]["md_name"]), ("Ma'am", "Nancy"))
        # "By name" works too, and a pronoun follows the word.
        self.md_client.patch("/api/v1/me/prefs/", {"addressAs": "name"}, format="json")
        self.assertEqual(self.md_client.get("/api/v1/me/").json()["address"], "Nancy")
        self.assertEqual(services.md_pronouns()[0], "he")  # a bare name says nothing about gender; Ma'am did

    def test_every_standard_view_speaks_from_the_server(self):
        for client, kinds in ((self.md_client, ("today", "week", "pending", "official", "late", "upcoming", "nodate", "stats", "private",
                                                "pa", "staff", "requests", "performance", "done")),
                              (self.pa_client, ("today", "pending", "late", "upcoming", "busy", "requests", "done"))):
            for kind in kinds:
                d, _ = self.ask(client, "dikhao", view_kind=kind, spoken_reply="MODEL COUNTED 99", spoken_reply_hi="मॉडल ने गिना", reply_language="hi")
                self.assertIsNotNone(d["view"], kind)
                self.assertEqual(d["spoken_reply"], d["view"]["summary"], kind)
                self.assertNotIn("99", d["spoken_reply"], kind)
                self.assertEqual(d["speak_lang"], "hi", kind)
                self.assertEqual(d["speak_text"], d["view"]["summary_hi"], kind)
                self.assertIn("request", d["view"], kind)
        # Mansi never hears Sir's private titles through any of them.
        for kind in ("today", "pending", "late", "upcoming", "busy", "requests", "done", "stats", "week"):
            d, _ = self.ask(self.pa_client, "dikhao", view_kind=kind)
            self.assertNotIn(SECRET, json.dumps(d), kind)


class SnoozeAndQuestionTests(BaseCase):
    """"Later" lives on the server and names its time; "in N minutes" is exact; every question JARVIS asks is a contract
    both the app and the phone's background part read; told-once is keyed by the moment, not the version."""

    def interpret(self, client, text, cmd, **extra):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = client.post("/api/v1/assistant/interpret/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def turn(self, client, text, cmd=None, **extra):
        if cmd is None:
            r = client.post("/api/v1/assistant/turn/", {"text": text, **extra}, format="json")
        else:
            with mock.patch("core.ai_brain.interpret", return_value=cmd):
                r = client.post("/api/v1/assistant/turn/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def reminders(self, client, title=None, hours=48):
        rems = client.get(f"/api/v1/reminders/?hours={hours}").json()["reminders"]
        return [r for r in rems if title is None or r["title"] == title]

    def test_in_n_minutes_is_exact_and_never_asks_tomorrow(self):
        d = self.interpret(self.md_client, "5 minute baad yaad dilana files lene hain", _cmd(
            intent="create_task", title="Take the files", due_in_minutes=5, engine="rules"))
        self.assertEqual(d["action"], "private_task")
        self.assertFalse(d["needs_clarification"])
        self.assertTrue(d["due_at"])
        self.assertIn("in 5 minutes (", d["due_label"])
        self.assertEqual(d["remind_before_minutes"], 0)
        self.assertIn("in 5 minutes", d["spoken_reply"])
        self.assertIn("5 मिनट में", d["speak_text"])
        # Saved with the exact moment: the first reminder is the one JARVIS said, not a lead before it.
        r = self.md_client.post("/api/v1/private-tasks/", {"title": "Take the files", "due_at": d["due_at"]}, format="json",
                                HTTP_IDEMPOTENCY_KEY="exact-1")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(datetime.fromisoformat(r.json()["due_at"]), datetime.fromisoformat(d["due_at"]))
        rems = self.reminders(self.md_client, "Take the files")
        self.assertEqual({x["label"] for x in rems} & {"before", "due"}, {"due"})
        due = next(x for x in rems if x["label"] == "due")
        self.assertEqual(datetime.fromisoformat(due["fire_at"]), datetime.fromisoformat(d["due_at"]))
        self.assertEqual(due["question"]["kind"], "done")  # and it asks its one question

    def test_later_is_kept_on_the_server_and_names_the_exact_time(self):
        when = tu.now_local() + timedelta(minutes=20)
        services.create_private_task(self.md, {"title": "Call the bank", "kind": Kind.CALL, "priority": Priority.NORMAL,
                                               "due_date": when.date(), "due_time": when.time().replace(second=0, microsecond=0)})
        due = next(x for x in self.reminders(self.md_client, "Call the bank") if x["label"] == "due")
        # JARVIS asked "Done, or remind you later?" and he said "10 minute baad": the answer names the time.
        d = self.interpret(self.md_client, "10 minute baad yaad dilana", _cmd(intent="snooze", snooze_minutes=10, engine="rules"),
                           awaiting='JARVIS asked: "Done, or remind you later?" about "Call the bank"')
        self.assertEqual(d["action"], "snooze")
        until = datetime.fromisoformat(d["snooze_until"])
        self.assertLess(abs((until - (tu.now_local() + timedelta(minutes=10))).total_seconds()), 90)
        self.assertIn("I'll remind you at", d["spoken_reply"])
        self.assertIn("याद दिला दूँगा", d["speak_text"])
        self.assertEqual(services.get_prefs(self.md)["snoozeMinutes"], 10)  # learned: his usual "later" is now 10
        # The phone sends it to the server: the reminder leaves the list and comes back under a new key at that time.
        r = self.md_client.post("/api/v1/reminders/snooze/", {"key": due["key"], "until": d["snooze_until"]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn("at ", r.json()["until_label"])
        rems = self.reminders(self.md_client, "Call the bank")
        self.assertNotIn(due["key"], [x["key"] for x in rems])
        back = next(x for x in rems if x["label"] == "due")
        self.assertTrue(back["key"].startswith(due["key"] + ":s"))
        self.assertEqual(datetime.fromisoformat(back["fire_at"]), until)
        self.assertIsNone(back["told_at"])  # announced afresh when it comes back
        # "Baad mein" with nothing on screen means the thing JARVIS said last.
        self.md_client.post("/api/v1/told/", {"keys": [back["key"]]}, format="json")
        r = self.md_client.post("/api/v1/reminders/snooze/", {"last": True, "minutes": 5}, format="json")
        self.assertEqual((r.status_code, r.json()["kind"]), (200, "reminder"), r.content)
        # An alert can wait too: it is out of sight until then, and fresh again when it comes back.
        self.pa_client.post("/api/v1/requests/", {"title": "Sign the vendor agreement"}, format="json", HTTP_IDEMPOTENCY_KEY="sn-req")
        alert = self.md_client.get("/api/v1/alerts/").json()["results"][0]
        self.assertEqual(alert["question"]["kind"], "accept")
        r = self.md_client.post("/api/v1/reminders/snooze/", {"alert_id": alert["id"], "minutes": 30}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertNotIn(alert["id"], [a["id"] for a in self.md_client.get("/api/v1/alerts/").json()["results"]])
        Alert.objects.filter(id=alert["id"]).update(snoozed_until=timezone.now() - timedelta(minutes=1))
        again = next(a for a in self.md_client.get("/api/v1/alerts/").json()["results"] if a["id"] == alert["id"])
        self.assertTrue(again["fresh"])
        self.assertNotEqual(again["announce_key"], f"alert:{alert['id']}")  # a new announcement, not a repeat

    def test_told_once_survives_a_rename_but_not_a_new_time(self):
        when = tu.now_local() + timedelta(hours=2)
        task = services.create_private_task(self.md, {"title": "Review the deck", "kind": Kind.TASK, "priority": Priority.NORMAL,
                                                      "due_date": when.date(), "due_time": when.time().replace(second=0, microsecond=0)})
        keys = {x["key"] for x in self.reminders(self.md_client, "Review the deck")}
        self.md_client.patch(f"/api/v1/private-tasks/{task.id}/", {"expected_version": 1, "title": "Review the Pune deck"}, format="json")
        self.assertEqual({x["key"] for x in self.reminders(self.md_client, "Review the Pune deck")}, keys)  # same moment, same keys
        later = (when + timedelta(hours=1)).time().replace(second=0, microsecond=0)
        self.md_client.patch(f"/api/v1/private-tasks/{task.id}/", {"expected_version": 2, "due_date": when.date().isoformat(),
                                                                   "due_time": later.isoformat()}, format="json")
        self.assertFalse({x["key"] for x in self.reminders(self.md_client, "Review the Pune deck")} & keys)  # a new time: new reminders

    def test_every_question_is_a_contract_from_the_server(self):
        when = tu.now_local() + timedelta(minutes=20)
        services.create_private_task(self.md, {"title": "Review the deck", "kind": Kind.MEETING, "priority": Priority.NORMAL,
                                               "due_date": when.date(), "due_time": when.time().replace(second=0, microsecond=0)})
        meeting_due = next(x for x in self.reminders(self.md_client, "Review the deck") if x["label"] == "due")
        self.assertIsNone(meeting_due["question"])  # a meeting that is starting asks nothing
        self.md_client.post("/api/v1/pa-tasks/", {"title": "Courier the contract"}, format="json", HTTP_IDEMPOTENCY_KEY="q-1")
        hers = self.pa_client.get("/api/v1/alerts/").json()["results"][0]
        self.assertEqual(hers["question"]["kind"], "seen")
        self.assertIn("Sir", hers["question"]["en"])
        self.assertIn("सर", hers["question"]["hi"])

    def test_the_background_part_hears_the_answer_to_its_question(self):
        when = tu.now_local() + timedelta(minutes=20)
        task = services.create_private_task(self.md, {"title": "Call the bank", "kind": Kind.CALL, "priority": Priority.NORMAL,
                                                      "due_date": when.date(), "due_time": when.time().replace(second=0, microsecond=0)})
        due = next(x for x in self.reminders(self.md_client, "Call the bank") if x["label"] == "due")
        about = {"kind": "reminder", "key": due["key"], "title": "Call the bank", "question": due["question"]["en"]}
        r = self.turn(self.md_client, "", event="ask", about=about)
        self.assertEqual((r["action"], r["listen"]), ("asking", True))
        r = self.turn(self.md_client, "ho gaya", _cmd(intent="confirm_pending"))
        self.assertEqual(r["action"], "saved")
        self.assertIn("Marked as done", r["show"])
        self.assertEqual(PrivateTask.objects.get(id=task.id).status, "COMPLETED")
        # "Later" to the same question: the server keeps the snooze.
        when2 = tu.now_local() + timedelta(minutes=25)
        services.create_private_task(self.md, {"title": "Pay the vendor", "kind": Kind.PAYMENT, "priority": Priority.NORMAL,
                                               "due_date": when2.date(), "due_time": when2.time().replace(second=0, microsecond=0)})
        due2 = next(x for x in self.reminders(self.md_client, "Pay the vendor") if x["label"] == "due")
        self.turn(self.md_client, "", event="ask", about={"kind": "reminder", "key": due2["key"], "title": "Pay the vendor", "question": ""})
        r = self.turn(self.md_client, "baad mein", _cmd(intent="snooze", engine="rules"))
        self.assertEqual(r["action"], "snoozed", r)
        from core.models import ReminderSnooze
        self.assertTrue(ReminderSnooze.objects.filter(user=self.md, key=due2["key"]).exists())
        # An alert's question too: "Accept it?" — "haan".
        self.pa_client.post("/api/v1/requests/", {"title": "Sign the vendor agreement"}, format="json", HTTP_IDEMPOTENCY_KEY="bg-req")
        alert = self.md_client.get("/api/v1/alerts/").json()["results"][0]
        self.turn(self.md_client, "", event="ask", about={"kind": "alert", "id": alert["id"], "title": alert["title"], "question": "Accept it?"})
        r = self.turn(self.md_client, "haan", _cmd(intent="confirm_pending"))
        self.assertIn("Accepted", r["show"])
        self.assertEqual(TaskRequest.objects.get(id=alert["ref_id"]).state, "ACCEPTED")

    def test_date_only_items_still_remind_and_staff_dates_get_a_follow_up(self):
        from core.reminders import LATE_DAY_REMINDER_BEFORE_CLOSE
        now = tu.now_local()
        close = now.replace(hour=19, minute=0, second=0, microsecond=0)
        day_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if day_time < now < close - LATE_DAY_REMINDER_BEFORE_CLOSE:
            services.create_private_task(self.md, {"title": "Sign the cheque", "kind": Kind.TASK, "priority": Priority.NORMAL,
                                                   "due_date": now.date()})
            day = [x for x in self.reminders(self.md_client, "Sign the cheque") if x["label"] == "day"]
            self.assertEqual(len(day), 1)
            self.assertEqual(datetime.fromisoformat(day[0]["fire_at"]), close - LATE_DAY_REMINDER_BEFORE_CLOSE)
        # A staff task with only a date: the PA who gave it gets a follow-up that day at her reminder hour.
        staff = StaffMember.objects.filter(active=True).first()
        tomorrow = tu.today_local() + timedelta(days=1)
        services.delegate_to_staff(self.pa, {"title": "Send the GST file", "kind": Kind.TASK, "priority": Priority.NORMAL,
                                             "due_date": tomorrow}, staff, category_key="", description="")
        follow = [x for x in self.reminders(self.pa_client, f"{staff.name}: Send the GST file") if x["label"] == "followup"]
        self.assertEqual(len(follow), 1)
        self.assertEqual(tu.to_local(datetime.fromisoformat(follow[0]["fire_at"])).strftime("%H:%M"), "09:30")
        self.assertIn("is due today", follow[0]["spoken"])


class HabitsAndRulesTests(BaseCase):
    """Names by voice from either phone, rules as settings rather than constants, habits JARVIS learns and shows,
    the history template, and the cancelled list with restore."""

    def interpret(self, client, text, cmd, **extra):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = client.post("/api/v1/assistant/interpret/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_names_by_voice_from_either_phone(self):
        # The MD names the PA; the PA hears it; every phone reads the new name from the server.
        d = self.interpret(self.md_client, "meri PA ka naam Suraj hai", _cmd(intent="set_setting", setting_key="paName", setting_value="Suraj"))
        self.assertEqual(d["action"], "set_setting")
        self.assertEqual(d["setting_patch"], {"paName": "Suraj"})
        r = self.md_client.patch("/api/v1/me/prefs/", d["setting_patch"], format="json")
        self.assertEqual(r.status_code, 200, r.content)
        # (the test client keeps the same user object; a real phone reads /me/ afresh and sees the new name)
        self.assertEqual(User.objects.get(username="pa").spoken_name, "Suraj")
        fresh_pa = APIClient()
        fresh_pa.force_authenticate(User.objects.get(username="pa"))
        self.assertEqual(fresh_pa.get("/api/v1/me/").json()["name"], "Suraj")
        self.assertEqual(self.md_client.get("/api/v1/today/").json()["pa_name"], "Suraj")
        told = [a for a in self.pa_client.get("/api/v1/alerts/").json()["results"] if a["ref_type"] == "profile"]
        self.assertTrue(told and "Suraj" in told[0]["spoken_text"], told)
        # The PA sets her own name; the MD is told.
        r = fresh_pa.patch("/api/v1/me/prefs/", {"myName": "Mansi"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(User.objects.get(username="pa").spoken_name, "Mansi")
        self.assertTrue(any("Mansi" in a["spoken_text"] for a in self.md_client.get("/api/v1/alerts/").json()["results"]))
        # The PA cannot rename the MD or herself through paName.
        self.assertEqual(self.pa_client.patch("/api/v1/me/prefs/", {"paName": "Someone"}, format="json").status_code, 403)
        # The model is told the names as settings it may change.
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="small_talk", spoken_reply="Hi")) as fake:
            self.md_client.post("/api/v1/assistant/interpret/", {"text": "hello"}, format="json")
        settings_ctx = fake.call_args.args[1]["settings"]
        self.assertEqual((settings_ctx["myName"]["now"], settings_ctx["paName"]["now"]), ("Sir", "Mansi"))
        self.assertNotIn("paName", json.dumps(self.pa_client.get("/api/v1/me/prefs/").json()))

    def test_rules_are_settings_and_habits_are_learned_and_forgettable(self):
        from core import scheduling
        # A full day is the person's own number.
        day = tu.today_local() + timedelta(days=3)
        for i in range(4):
            services.create_private_task(self.md, {"title": f"Thing {i}", "kind": Kind.TASK, "priority": Priority.NORMAL, "due_date": day})
        self.assertFalse(scheduling.day_load(self.md, day, services.get_prefs(self.md))["heavy"])
        self.md_client.patch("/api/v1/me/prefs/", {"heavyDayItems": 4}, format="json")
        self.assertTrue(scheduling.day_load(self.md, day, services.get_prefs(self.md))["heavy"])
        # The check-in delay too.
        self.md_client.patch("/api/v1/me/prefs/", {"checkinAfterMinutes": 20}, format="json")
        when = tu.now_local() + timedelta(hours=1)
        services.create_private_task(self.md, {"title": "Call the auditor", "kind": Kind.CALL, "priority": Priority.NORMAL,
                                               "due_date": when.date(), "due_time": when.time().replace(second=0, microsecond=0)})
        rems = self.md_client.get("/api/v1/reminders/?hours=48").json()["reminders"]
        due = next(r for r in rems if r["title"] == "Call the auditor" and r["label"] == "due")
        check = [r for r in rems if r["title"] == "Call the auditor" and r["label"] == "checkin"]
        if check:
            self.assertEqual(datetime.fromisoformat(check[0]["fire_at"]) - datetime.fromisoformat(due["fire_at"]), timedelta(minutes=20))
        # Meeting lengths he mentions become his usual length; a meeting with none said takes it.
        for minutes in (30, 30, 45):
            services.create_private_task(self.md, {"title": "Review", "kind": Kind.MEETING, "priority": Priority.NORMAL,
                                                   "due_date": day, "due_time": time(11, 0), "duration_minutes": minutes})
        self.assertEqual(services.get_prefs(self.md)["meetingMinutes"], 30)
        plain = services.create_private_task(self.md, {"title": "Plain meeting", "kind": Kind.MEETING, "priority": Priority.NORMAL,
                                                       "due_date": day, "due_time": time(15, 0)})
        self.assertEqual(plain.scheduled_end - plain.scheduled_start, timedelta(minutes=30))
        # What he asks for is remembered; the habits are listed in plain words and any one can be forgotten.
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="query", view_kind="late")):
            for _ in range(2):
                self.md_client.post("/api/v1/assistant/interpret/", {"text": "late wale dikhao"}, format="json")
        habits = {h["key"]: h for h in self.md_client.get("/api/v1/me/habits/").json()["results"]}
        self.assertEqual(habits["meetingMinutes"]["value"], "30 minutes")
        self.assertIn("late", habits["viewsRecent"]["value"])
        self.assertEqual(habits["heavyDayItems"]["value"], "4 items")
        r = self.md_client.delete("/api/v1/me/habits/meetingMinutes/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(services.get_prefs(self.md)["meetingMinutes"], 60)
        self.assertEqual(self.md_client.delete("/api/v1/me/habits/nonsense/").status_code, 422)

    def test_history_and_cancelled_lists(self):
        from core import datacards
        last_week = tu.today_local() - timedelta(days=7)
        done = services.create_private_task(self.md, {"title": "Meeting with Sharma ji – Pune", "kind": Kind.MEETING, "priority": Priority.NORMAL,
                                                      "due_date": last_week, "due_time": time(11, 0)})
        services.update_private_task(self.md, done.id, {"status": "COMPLETED"}, 1)
        services.create_private_task(self.md, {"title": "Call Sharma ji again", "kind": Kind.CALL, "priority": Priority.NORMAL,
                                               "due_date": tu.today_local() + timedelta(days=2), "due_time": time(16, 0)})
        gone = services.create_private_task(self.md, {"title": "Old Sharma dinner", "kind": Kind.TASK, "priority": Priority.NORMAL})
        services.update_private_task(self.md, gone.id, {"status": "CANCELLED"}, 1)
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="query", view_kind="history", view_words="Sharma")):
            d = self.md_client.post("/api/v1/assistant/interpret/", {"text": "Sharma ji se pichhli baar kab mila tha"}, format="json").json()
        view = d["view"]
        self.assertEqual(view["kind"], "history")
        self.assertEqual([r["status"] for r in view["rows"]], ["Open", "Cancelled", "Done"])
        self.assertIn("Meeting with Sharma ji – Pune", d["spoken_reply"])
        self.assertIn("(done)", d["spoken_reply"])
        # The words the server guesses from the question when the model files it elsewhere.
        self.assertEqual(datacards.guess_kind("pichhli baar Sharma ji se kab mila"), "history")
        # Cancelled list, and a restore through the ordinary edit.
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="query", view_kind="cancelled")):
            d = self.md_client.post("/api/v1/assistant/interpret/", {"text": "cancel kiye hue dikhao"}, format="json").json()
        self.assertEqual([r["title"] for r in d["view"]["rows"]], ["Old Sharma dinner"])
        self.assertTrue(d["view"]["rows"][0]["restorable"])
        r = self.md_client.patch(f"/api/v1/private-tasks/{gone.id}/", {"expected_version": 2, "status": "OPEN"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], "OPEN")
        # The PA never sees the MD's private history.
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="query", view_kind="history", view_words="Sharma")):
            d = self.pa_client.post("/api/v1/assistant/interpret/", {"text": "Sharma"}, format="json").json()
        self.assertNotIn("Sharma", json.dumps(d["view"]["rows"]))


class TwoThingsInOneBreathTests(BaseCase):
    """"4 baje Aman ke saath meeting aur 8 baje Sharma ji ke saath": nothing is dropped, every item is checked by the
    same rules, one card reads them all back, one yes saves them all."""

    def interpret(self, client, text, cmd, **extra):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = client.post("/api/v1/assistant/interpret/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def turn(self, client, text, cmd, **extra):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = client.post("/api/v1/assistant/turn/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def two_meetings(self, **extra):
        day = (tu.today_local() + timedelta(days=1)).isoformat()  # tomorrow, so the times are ahead whenever this runs
        return _cmd(intent="create_task", title="Meeting with Aman", kind="MEETING", due_date=day, due_time="16:00", engine="rules",
                    more_items=[{"intent": "create_task", "title": "Meeting with Sharma ji", "notes": "", "kind": "MEETING", "priority": "NORMAL",
                                 "due_date": day, "due_time": "20:00", "due_in_minutes": 0, "duration_minutes": 0, "remind_before_minutes": 0,
                                 "staff_name": "", "team_name": "", "people": [], "reminder_line": ""}], **extra)

    def test_both_meetings_are_read_back_and_saved_on_one_yes(self):
        d = self.interpret(self.md_client, "4 baje Aman ke saath meeting aur 8 baje Sharma ji ke saath", self.two_meetings())
        self.assertEqual(d["action"], "private_task")
        self.assertEqual([b["title"] for b in d["batch"]], ["Meeting with Sharma ji"])
        self.assertEqual(d["batch"][0]["due_time"], "20:00")
        self.assertIn("2 things", d["spoken_reply"])
        self.assertIn("Meeting with Aman", d["spoken_reply"])
        self.assertIn("Meeting with Sharma ji", d["spoken_reply"])
        self.assertTrue(d["spoken_reply"].endswith("Shall I save all 2?"), d["spoken_reply"])
        self.assertEqual(d["speak_lang"], "hi")
        self.assertIn("सभी 2 सेव कर दूँ?", d["speak_text"])
        self.assertFalse(d["needs_clarification"])
        # With the app closed: the same two in one breath, "haan", both saved.
        before = PrivateTask.objects.filter(owner=self.md).count()
        r = self.turn(self.md_client, "4 baje Aman ke saath meeting aur 8 baje Sharma ji ke saath", self.two_meetings())
        self.assertEqual(r["action"], "private_task")
        self.assertIn("2 things", r["show"])
        r = self.turn(self.md_client, "haan", _cmd(intent="confirm_pending"))
        self.assertEqual((r["action"], r["show"]), ("saved", "Saved all 2, Sir."), r)
        self.assertEqual(r["say"], "सभी 2 सेव हो गए, सर।")
        self.assertEqual(PrivateTask.objects.filter(owner=self.md).count(), before + 2)
        titles = set(PrivateTask.objects.filter(owner=self.md).values_list("title", flat=True))
        self.assertTrue({"Meeting with Aman", "Meeting with Sharma ji"} <= titles)

    def test_a_question_about_one_item_comes_first_and_the_rest_wait(self):
        cmd = self.two_meetings()
        cmd["more_items"][0]["people"] = ["Aman"]  # nobody JARVIS knows
        d = self.interpret(self.md_client, "4 baje meeting aur 8 baje Aman ke saath", cmd)
        self.assertTrue(d["needs_clarification"])
        self.assertIn("I don't know any Aman", d["question"])
        self.assertEqual(len(d["batch"]), 1)  # kept, waiting behind the question
        self.assertEqual(d["spoken_reply"], d["question"])

    def test_a_mixed_list_goes_to_its_own_places(self):
        day = (tu.today_local() + timedelta(days=1)).isoformat()
        cmd = _cmd(intent="create_task", title="Call Mehta ji", kind="CALL", due_date=day, due_time="15:00", engine="rules",
                   more_items=[{"intent": "assign_to_pa", "title": "Book the Pune hotel", "notes": "", "kind": "TASK", "priority": "NORMAL",
                                "due_date": day, "due_time": "", "due_in_minutes": 0, "duration_minutes": 0, "remind_before_minutes": 0,
                                "staff_name": "", "team_name": "", "people": [], "reminder_line": ""}])
        d = self.interpret(self.md_client, "3 baje Mehta ji ko call aur Priya ko bolo Pune hotel book kare", cmd)
        self.assertEqual((d["action"], d["batch"][0]["action"]), ("private_task", "pa_task"))
        self.assertIn("personal call: Call Mehta ji", d["spoken_reply"])
        self.assertIn("task for Priya: Book the Pune hotel", d["spoken_reply"])
        r = self.turn(self.md_client, "3 baje Mehta ji ko call aur Priya ko bolo Pune hotel book kare", cmd)
        self.assertEqual(r["action"], "private_task", r)
        r = self.turn(self.md_client, "haan", _cmd(intent="confirm_pending"))
        self.assertEqual((r["action"], r["show"]), ("saved", "Saved all 2, Sir."), r)
        self.assertIn("Call Mehta ji", list(PrivateTask.objects.filter(owner=self.md).values_list("title", flat=True)))  # (encrypted title: read, not filtered)
        self.assertTrue(SharedTask.objects.filter(title="Book the Pune hotel", assignee_type="PA").exists())


class ReliabilityTests(BaseCase):
    """Reminders pushed at their moment, the phone's diagnostics, honesty about the staff door, and late wording."""

    def test_reminders_are_pushed_once_at_their_moment(self):
        from core import push, reminderpush
        push.register_device(self.md, "tok-md-1")
        task = services.create_private_task(self.md, {"title": "Call the bank", "kind": Kind.CALL, "priority": Priority.NORMAL,
                                                      "due_at": timezone.now() - timedelta(seconds=5)})
        self.assertEqual(task.due_kind, "TIMED")
        sent = []
        with mock.patch("core.push.configured", return_value=True), \
             mock.patch("core.push._deliver", side_effect=lambda tokens, data, urgent: (sent.append(data), [(True, "")] * len(tokens))[1]):
            reminderpush.STATE.update(checked_at=None, pushed=[])
            self.assertEqual(reminderpush.run(), 1)
            self.assertEqual(reminderpush.run(), 0)  # once, not on every cycle
        self.assertEqual(sent[0]["type"], "reminder")
        self.assertIn("Call the bank", sent[0]["spoken"])
        self.assertTrue(sent[0]["key"].startswith("private:"))
        self.assertEqual(json.loads(sent[0]["question"])["kind"], "done")
        # Nothing is marked told by the push itself: the phone does that when it speaks.
        due = next(r for r in self.md_client.get("/api/v1/reminders/").json()["reminders"] if r["key"] == sent[0]["key"])
        self.assertIsNone(due["told_at"])

    def test_phone_diagnostics_are_kept_per_person(self):
        now = timezone.now().isoformat()
        r = self.md_client.post("/api/v1/diagnostics/", {"app_version": "2.0.0", "events": [
            {"level": "error", "where": "save", "message": "Network request failed", "at": now},
            {"level": "warn", "where": "voice", "message": "no-speech", "at": now},
        ]}, format="json")
        self.assertEqual((r.status_code, r.json()["stored"]), (200, 2), r.content)
        mine = self.md_client.get("/api/v1/diagnostics/").json()["results"]
        self.assertEqual([e["message"] for e in mine], ["Network request failed", "no-speech"])
        self.assertEqual(self.pa_client.get("/api/v1/diagnostics/").json()["results"], [])

    def test_the_staff_door_is_described_honestly(self):
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="assign_to_staff", title="Send the GST file",
                                                                       staff_name="QTC Developer", engine="rules")):
            d = self.md_client.post("/api/v1/assistant/interpret/", {"text": "QTC Developer ko bolo GST file bheje"}, format="json").json()
        self.assertEqual(d["action"], "delegation")
        self.assertTrue(any("test mode" in w for w in d["warnings"]), d["warnings"])
        self.assertIn("test mode", d["spoken_reply"])              # said in the confirmation, not hidden
        self.assertTrue(d["spoken_reply"].endswith("Shall I save it?"))

    def test_late_items_say_how_late(self):
        services.create_private_task(self.md, {"title": "Pay the vendor", "kind": Kind.PAYMENT, "priority": Priority.NORMAL,
                                               "due_date": tu.today_local() - timedelta(days=3)})
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="query", view_kind="late")):
            d = self.md_client.post("/api/v1/assistant/interpret/", {"text": "late wale"}, format="json").json()
        self.assertIn("Pay the vendor (3 days late)", d["spoken_reply"])
        self.assertEqual(d["view"]["rows"][0]["title"], "Pay the vendor")  # oldest first
        self.assertIn("3 days late", d["view"]["rows"][0]["when"])


class LanguageTests(BaseCase):
    """One language rule for everything JARVIS says: the sentence just spoken decides, a pinned setting overrides,
    and what is said with nothing just spoken (alerts, reminders) follows the language used lately."""

    def interpret(self, client, text, cmd, **extra):
        with mock.patch("core.ai_brain.interpret", return_value=cmd) as fake:
            r = client.post("/api/v1/assistant/interpret/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json(), fake

    def turn(self, client, text, cmd, **extra):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = client.post("/api/v1/assistant/turn/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_the_sentence_itself_tells_the_language(self):
        from core import lang
        self.assertEqual(lang.detect("kal 4 baje Sharma ji ke saath meeting"), "hi")
        self.assertEqual(lang.detect("कल शर्मा जी के साथ मीटिंग"), "hi")
        self.assertEqual(lang.detect("what do I have today"), "en")
        self.assertEqual(lang.detect("remind me tomorrow at 4"), "en")
        self.assertEqual(lang.detect("haan"), "hi")
        self.assertEqual(lang.detect("yes"), "en")
        self.assertEqual(lang.detect("Ramesh Kumar"), "")   # a bare name says nothing
        self.assertEqual(lang.detect("5"), "")
        self.assertEqual(lang.pick("hi", "English", "हिंदी"), "हिंदी")
        self.assertEqual(lang.pick("hi", "English", ""), "English")  # no twin: the English line
        self.assertEqual(lang.pick("en", "English", "हिंदी"), "English")

    def test_server_decides_the_language_and_remembers_it(self):
        tomorrow = (tu.today_local() + timedelta(days=1)).isoformat()
        # Hinglish, while the model wrongly votes English: the server-built line is still spoken in Hindi.
        d, fake = self.interpret(self.md_client, "kal 4 baje Sharma ji ke saath meeting", _cmd(
            intent="create_task", title="Meeting with Sharma ji", kind="MEETING", due_date=tomorrow, due_time="16:00",
            reply_language="en", engine="rules"))
        self.assertEqual(fake.call_args.args[1]["reply_language"], "hi")  # the model is told, not asked
        self.assertEqual((d["reply_language"], d["speak_lang"]), ("hi", "hi"))
        self.assertIn("कल 4 बजे", d["speak_text"])
        self.assertTrue(d["speak_text"].endswith("सेव कर दूँ?"), d["speak_text"])
        self.assertIn("tomorrow at 4 PM", d["spoken_reply"])  # the screen keeps the English line
        self.assertEqual(services.get_prefs(self.md)["langRecent"], "hi")
        # English next: English.
        d, _ = self.interpret(self.md_client, "meeting with Sharma ji tomorrow at 4", _cmd(
            intent="create_task", title="Meeting with Sharma ji", kind="MEETING", due_date=tomorrow, due_time="16:00", engine="rules"))
        self.assertEqual((d["reply_language"], d["speak_lang"]), ("en", "en"))
        self.assertEqual(services.get_prefs(self.md)["langRecent"], "hi,en")
        # A bare answer with no language of its own: the model is told what he has mostly used lately and follows it.
        services.set_pref_quiet(self.md, "langRecent", "hi,hi,hi,en")
        d, fake = self.interpret(self.md_client, "Ramesh", _cmd(intent="assign_to_staff", title="Send the GST file",
                                                                   staff_name="Ramesh", reply_language="hi", engine="rules"))
        self.assertEqual(fake.call_args.args[1]["reply_language"], "hi")
        self.assertEqual(d["reply_language"], "hi")
        self.assertEqual(services.get_prefs(self.md)["langRecent"], "hi,hi,hi,en,hi")
        self.assertEqual(d["speak_lang"], "hi")
        self.assertIn("Ramesh Kumar", d["speak_text"])
        # Pinned: "always English" wins over a Hindi sentence, "always Hindi" over an English one.
        self.md_client.patch("/api/v1/me/prefs/", {"replyLanguage": "en"}, format="json")
        d, _ = self.interpret(self.md_client, "kal 4 baje Sharma ji ke saath meeting", _cmd(
            intent="create_task", title="Meeting with Sharma ji", kind="MEETING", due_date=tomorrow, due_time="16:00", engine="rules"))
        self.assertEqual((d["reply_language"], d["speak_lang"]), ("en", "en"))
        self.md_client.patch("/api/v1/me/prefs/", {"replyLanguage": "Hindi"}, format="json")
        d, _ = self.interpret(self.md_client, "meeting with Sharma ji tomorrow at 4", _cmd(
            intent="create_task", title="Meeting with Sharma ji", kind="MEETING", due_date=tomorrow, due_time="16:00", engine="rules"))
        self.assertEqual((d["reply_language"], d["speak_lang"]), ("hi", "hi"))
        self.assertEqual(services.get_prefs(self.md)["replyLanguage"], "hi")

    def test_questions_and_answers_the_server_builds_have_hindi_twins(self):
        # Which one did you finish? / nothing pending / delete asks first — each spoken in Hindi when asked in Hindi.
        d, _ = self.interpret(self.md_client, "wo kaam ho gaya", _cmd(intent="complete_task", title="xyz", engine="rules"))
        self.assertTrue(d["needs_clarification"])
        self.assertEqual(d["speak_lang"], "hi")
        self.assertIn("कौन सा टास्क पूरा हुआ", d["speak_text"])
        d, _ = self.interpret(self.md_client, "request accept karo", _cmd(intent="accept_request", engine="rules"))
        self.assertEqual((d["action"], d["speak_lang"]), ("answer", "hi"))
        self.assertIn("कोई रिक्वेस्ट पेंडिंग नहीं", d["speak_text"])
        d, _ = self.interpret(self.md_client, "bas, thank you", _cmd(intent="end_conversation", engine="rules"))
        self.assertEqual(d["speak_lang"], "hi")
        self.assertIn("हेलो जार्विस", d["speak_text"])
        d, _ = self.interpret(self.md_client, "okay thanks, that's all", _cmd(intent="end_conversation", engine="rules"))
        self.assertEqual(d["speak_lang"], "en")
        self.assertIn("Hello JARVIS", d["speak_text"])

    def test_the_yes_is_answered_in_the_language_of_the_yes(self):
        tomorrow = (tu.today_local() + timedelta(days=1)).isoformat()
        r = self.turn(self.md_client, "kal 4 baje Sharma ji ke saath meeting", _cmd(
            intent="create_task", title="Meeting with Sharma ji", kind="MEETING", due_date=tomorrow, due_time="16:00", engine="rules"))
        self.assertEqual((r["action"], r["say_lang"]), ("private_task", "hi"))
        self.assertIn("सेव कर दूँ", r["say"])
        r = self.turn(self.md_client, "haan", _cmd(intent="confirm_pending"))
        self.assertEqual((r["action"], r["say_lang"]), ("saved", "hi"))
        self.assertIn("सेव हो गया", r["say"])
        self.assertIn("कल 4 बजे याद दिला दूँगा", r["say"])
        self.assertTrue(r["show"].startswith("Saved, Sir."))
        # Nothing waiting, asked in English: English.
        r = self.turn(self.md_client, "yes please", _cmd(intent="confirm_pending"))
        self.assertEqual(r["say_lang"], "en")

    def test_alerts_reminders_and_nudges_follow_the_recipients_recent_language(self):
        from core import followups, push
        from core.reminders import reminders_for
        services.set_pref_quiet(self.pa, "langRecent", "hi,hi,hi")
        soon = tu.now_local() + timedelta(hours=3)
        self.md_client.post("/api/v1/pa-tasks/", {"title": "Courier the contract", "priority": "HIGH",
                                                   "due_date": soon.date().isoformat(), "due_time": soon.strftime("%H:%M")},
                            format="json", HTTP_IDEMPOTENCY_KEY="lang-1")
        alert = self.pa_client.get("/api/v1/alerts/").json()["results"][0]
        self.assertIn("ने आपको एक नया ज़रूरी टास्क दिया है: Courier the contract", alert["spoken_text"])
        self.assertIn("Sir has given you a new high-priority task", alert["spoken_text_en"])
        self.assertEqual(push.payload_for(Alert.objects.get(id=alert["id"]))["spoken"], alert["spoken_text"])
        # Her reminders for it are Hindi too; Sir's about it stay English.
        hers = [r for r in reminders_for(self.pa)["reminders"] if r["ref"]["id"] == alert["ref_id"]]
        self.assertTrue(hers and all(r["lang"] == "hi" for r in hers), hers)
        self.assertTrue(any("सर का टास्क अभी ड्यू है: Courier the contract" in r["spoken"] for r in hers), hers)
        self.assertTrue(all("Priya, " in r["spoken_en"] for r in hers), hers)
        his = [r for r in reminders_for(self.md)["reminders"] if r["ref"]["id"] == alert["ref_id"]]
        self.assertTrue(his and all(r["lang"] == "en" and "Priya's task" in r["spoken"] for r in his), his)
        # Her status change is told to Sir in English (his language), with the Hindi twin kept on the row.
        task_id = alert["ref_id"]
        r = self.pa_client.post(f"/api/v1/pa-tasks/{task_id}/status/", {"status": "COMPLETED", "expected_version": 1}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        done = [a for a in self.md_client.get("/api/v1/alerts/").json()["results"] if a["kind"] == "STAFF_UPDATE"][0]
        self.assertEqual(done["spoken_text"], "Sir, Priya has completed: Courier the contract.")
        self.assertEqual(done["spoken_text_hi"], "सर, Priya ने पूरा कर दिया: Courier the contract।")
        # A follow-up nudge for her carries a Hindi twin and is served in Hindi.
        r = self.md_client.post("/api/v1/pa-tasks/", {"title": "Chase the vendor"}, format="json", HTTP_IDEMPOTENCY_KEY="lang-3")
        SharedTask.objects.filter(id=r.json()["id"]).update(created_at=tu.now_local() - timedelta(days=2))
        followups.run(now=tu.combine_local(tu.today_local(), time(11, 0)))
        nudge = [a for a in self.pa_client.get("/api/v1/alerts/").json()["results"] if a["ref_id"].startswith("notstarted:")]
        self.assertTrue(nudge, "no nudge raised")
        self.assertIn("अभी शुरू नहीं हुआ", nudge[0]["spoken_text"])
        self.assertIn("is not started yet", nudge[0]["spoken_text_en"])


class PlainReminderTests(BaseCase):
    """A reminder is not a task card and not a meeting: it is saved at once, rings at its moment, and nobody is
    asked whose it is. And when the AI cannot be reached, nothing is guessed."""

    def interpret(self, client, text, cmd):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = client.post("/api/v1/assistant/interpret/", {"text": text}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_md_reminder_is_saved_at_once_and_rings_at_its_moment(self):
        had = set(PrivateTask.objects.values_list("id", flat=True))
        d = self.interpret(self.md_client, "remind me in 2 minutes to call Aman",
                           _cmd(intent="create_task", title="Call Aman", kind="REMINDER", due_in_minutes=2, engine="ai"))
        self.assertEqual(d["action"], "answer")
        self.assertFalse(d["needs_clarification"])
        self.assertIn("remind you in 2 minutes", d["spoken_reply"])
        task = PrivateTask.objects.exclude(id__in=had).get()
        self.assertEqual(task.remind_before_minutes, 0)
        self.assertAlmostEqual((task.due_at - timezone.now()).total_seconds(), 120, delta=10)

    def test_pa_reminder_is_her_own_without_asking_whose(self):
        d = self.interpret(self.pa_client, "10 minute baad yaad dilana courier bhejna hai",
                           _cmd(intent="needs_destination", title="Send courier", kind="REMINDER", due_in_minutes=10, engine="ai"))
        self.assertEqual(d["action"], "answer")
        self.assertTrue(SharedTask.objects.filter(title="Send courier").exists())

    def test_reminder_without_a_moment_asks_only_when(self):
        had = PrivateTask.objects.count()
        d = self.interpret(self.md_client, "ek reminder laga do", _cmd(intent="create_task", title="", kind="REMINDER", engine="ai"))
        self.assertTrue(d["needs_clarification"])
        self.assertIn("when should I remind you", d["spoken_reply"])
        self.assertEqual(PrivateTask.objects.count(), had)

    def test_ai_unreachable_guesses_nothing(self):
        d = self.interpret(self.md_client, "kya tum mujhe apne baare mein bata doge",
                           _cmd(intent="create_task", title="Kya tum mujhe apne baare mein bata doge", engine="rules", ai_failed=True))
        self.assertEqual(d["action"], "answer")
        self.assertIn("didn't catch that", d["spoken_reply"])
        self.assertNotIn("slow", d["spoken_reply"])

    def test_meeting_with_someone_else_is_never_with_the_pa(self):
        day = (tu.today_local() + timedelta(days=1)).isoformat()
        d = self.interpret(self.md_client, "kal 4 baje Aman ke saath meeting",
                           _cmd(intent="meet_with_pa", title="Meeting with Aman", kind="MEETING", due_date=day, due_time="16:00",
                                people=["Aman"], engine="ai"))
        self.assertIn(d["action"], {"private_task", "choose_destination"})


class MeetingJugglingTests(BaseCase):
    """A new meeting over an existing one: fit it in the gap, push the others, or move it; nothing moves without
    his word. Lengths change by voice, two meetings swap, and what piled up during a meeting is reported once."""

    def turn(self, client, text, cmd, **extra):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = client.post("/api/v1/assistant/turn/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def interpret(self, client, text, cmd, **extra):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = client.post("/api/v1/assistant/interpret/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def _meeting(self, title, day, at, minutes=30, priority=Priority.NORMAL):
        return services.create_private_task(self.md, {"title": title, "kind": Kind.MEETING, "priority": priority,
                                                      "due_date": day, "due_time": at, "duration_minutes": minutes})

    def _new(self, day, at="13:10", minutes=15):
        return _cmd(intent="create_task", title="Priority meeting", kind="MEETING", priority="URGENT",
                    due_date=day.isoformat(), due_time=at, duration_minutes=minutes, engine="ai")

    def test_three_choices_are_offered_and_nothing_is_saved(self):
        day = _workday(2)
        self._meeting("Aman review", day, time(13, 15))
        before = PrivateTask.objects.count()
        d = self.interpret(self.md_client, "1:10 pe priority meeting", self._new(day))
        opts = d["schedule"]["options"]
        self.assertEqual(opts["fit"], {"duration_minutes": 5})
        self.assertEqual([(m["title"], m["due_time"]) for m in opts["push"]], [("Aman review", "13:25")])
        self.assertTrue(opts["move"]["due_time"])
        self.assertIn("keep this to 5 minutes", d["spoken_reply"])
        self.assertIn("move Aman review to 1:25 PM", d["spoken_reply"])
        self.assertEqual(PrivateTask.objects.count(), before)

    def test_fit_keeps_it_to_the_gap(self):
        day = _workday(2)
        self._meeting("Aman review", day, time(13, 15))
        self.turn(self.md_client, "1:10 pe priority meeting", self._new(day))
        r = self.turn(self.md_client, "5 minute hi rakho", _cmd(intent="confirm_pending", setting_value="fit"))
        self.assertEqual(r["action"], "saved")
        new = PrivateTask.objects.get(estimated_minutes=5)
        self.assertEqual(tu.to_local(new.due_at).strftime("%H:%M"), "13:10")
        self.assertEqual(tu.to_local(new.scheduled_end).strftime("%H:%M"), "13:15")

    def test_push_moves_the_chain_and_stops_at_the_first_gap(self):
        day = _workday(2)
        aman = self._meeting("Aman review", day, time(13, 15))
        sharma = self._meeting("Sharma call", day, time(13, 45))     # straight after Aman: pushed in turn
        late = self._meeting("Evening meeting", day, time(17, 0))    # a gap before it: stays
        d = self.interpret(self.md_client, "1:10 pe priority meeting", self._new(day))
        self.assertEqual([(m["title"], m["due_time"]) for m in d["schedule"]["options"]["push"]],
                         [("Aman review", "13:25"), ("Sharma call", "13:55")])
        self.turn(self.md_client, "1:10 pe priority meeting", self._new(day))
        r = self.turn(self.md_client, "Aman wali aage kar do", _cmd(intent="confirm_pending", setting_value="push"))
        self.assertEqual(r["action"], "saved", r)
        for task, at in ((aman, "13:25"), (sharma, "13:55"), (late, "17:00")):
            task.refresh_from_db()
            self.assertEqual(tu.to_local(task.due_at).strftime("%H:%M"), at)
        new = PrivateTask.objects.get(priority=Priority.URGENT, estimated_minutes=15)
        self.assertEqual(tu.to_local(new.due_at).strftime("%H:%M"), "13:10")

    def test_keep_this_and_move_the_other_pushes_the_other(self):
        day = _workday(2)
        aman = self._meeting("Aman review", day, time(11, 0))
        self.turn(self.md_client, "11 baje priority meeting", self._new(day, at="11:00", minutes=30))
        r = self.turn(self.md_client, "isko yahin rakho aur doosri ko aage kar do",
                      _cmd(intent="confirm_pending", setting_value="push", keep_original=True))
        self.assertEqual(r["action"], "saved", r)
        aman.refresh_from_db()
        self.assertEqual(tu.to_local(aman.due_at).strftime("%H:%M"), "11:30")

    def test_one_time_is_never_given_to_two_meetings(self):
        day = _workday(2)
        aman = self._meeting("Aman review", day, time(11, 0))
        sharma = self._meeting("Sharma call", day, time(15, 0))
        d = self.interpret(self.md_client, "dono 12 baje kar do",
                           _cmd(intent="change_task", target_refs=[str(aman.id), str(sharma.id)], changed_fields=["due_time"],
                                due_time="12:00"))
        self.assertTrue(d["needs_clarification"])
        self.assertEqual(d["targets"], [])
        self.assertIn("cannot be at the same time", d["spoken_reply"])

    def test_the_saved_length_is_the_length_the_diary_was_checked_for(self):
        day = _workday(2)
        services.set_prefs(self.md, {"meetingMinutes": 30})
        self._meeting("Aman review", day, time(11, 0))
        self.turn(self.md_client, "11 baje meeting", _cmd(intent="create_task", title="Board meeting", kind="MEETING",
                                                         due_date=day.isoformat(), due_time="11:00", engine="ai"))
        r = self.turn(self.md_client, "Aman wali aage kar do", _cmd(intent="confirm_pending", setting_value="push"))
        self.assertEqual(r["action"], "saved", r)
        new = PrivateTask.objects.get(estimated_minutes=30, kind=Kind.MEETING, due_at__isnull=False, version=1,
                                      id__in=[t.id for t in PrivateTask.objects.all() if t.title == "Board meeting"])
        self.assertEqual(tu.to_local(new.scheduled_end).strftime("%H:%M"), "11:30")

    def test_a_bare_yes_asks_which_one_and_moves_nothing(self):
        day = _workday(2)
        aman = self._meeting("Aman review", day, time(13, 15))
        before = PrivateTask.objects.count()
        self.turn(self.md_client, "1:10 pe priority meeting", self._new(day))
        r = self.turn(self.md_client, "haan", _cmd(intent="confirm_pending"))
        self.assertNotEqual(r["action"], "saved")
        self.assertIn("keep this to 5 minutes", r["show"])
        self.assertEqual(PrivateTask.objects.count(), before)
        aman.refresh_from_db()
        self.assertEqual(tu.to_local(aman.due_at).strftime("%H:%M"), "13:15")

    def test_length_changes_by_voice(self):
        day = _workday(2)
        aman = self._meeting("Aman review", day, time(13, 15))
        self.turn(self.md_client, "Aman review 15 minute ki kar do",
                  _cmd(intent="change_task", target_refs=[str(aman.id)], changed_fields=["duration_minutes"], duration_minutes=15))
        r = self.turn(self.md_client, "haan", _cmd(intent="confirm_pending"))
        self.assertEqual(r["action"], "saved", r)
        aman.refresh_from_db()
        self.assertEqual(aman.estimated_minutes, 15)
        self.assertEqual(tu.to_local(aman.scheduled_end).strftime("%H:%M"), "13:30")

    def test_two_meetings_swap_on_one_yes(self):
        day = _workday(2)
        aman = self._meeting("Aman review", day, time(11, 0))
        sharma = self._meeting("Sharma call", day, time(15, 0))
        d = self.interpret(self.md_client, "in dono ka time badal do",
                           _cmd(intent="change_task", target_refs=[str(aman.id), str(sharma.id)], setting_value="swap"))
        self.assertEqual(d["action"], "change_task")
        self.assertIn("swap them", d["spoken_reply"])
        self.assertEqual(d["changes"]["due_time"], "15:00")
        self.assertEqual(d["batch"][0]["changes"]["due_time"], "11:00")
        self.turn(self.md_client, "in dono ka time badal do",
                  _cmd(intent="change_task", target_refs=[str(aman.id), str(sharma.id)], setting_value="swap"))
        r = self.turn(self.md_client, "haan", _cmd(intent="confirm_pending"))
        self.assertEqual(r["action"], "saved", r)
        aman.refresh_from_db()
        sharma.refresh_from_db()
        self.assertEqual(tu.to_local(aman.due_at).strftime("%H:%M"), "15:00")
        self.assertEqual(tu.to_local(sharma.due_at).strftime("%H:%M"), "11:00")

    def test_what_piled_up_during_a_meeting_is_reported_once_from_three(self):
        from core import followups
        now = timezone.now()
        start = now - timedelta(minutes=40)
        local = tu.to_local(start)
        self._meeting("Board meeting", local.date(), local.time().replace(second=0, microsecond=0), minutes=35)
        for i, title in enumerate(["Sign cheque", "Approve leave"]):
            req = services.create_md_request(self.pa, {"title": title, "kind": Kind.TASK, "priority": Priority.NORMAL})
            TaskRequest.objects.filter(id=req.id).update(created_at=start + timedelta(minutes=5 + i))
        self.assertEqual(followups.piled_up(now), 0)     # two are not worth the question
        req = services.create_md_request(self.pa, {"title": "Call the bank", "kind": Kind.CALL, "priority": Priority.URGENT})
        TaskRequest.objects.filter(id=req.id).update(created_at=start + timedelta(minutes=20))
        self.assertEqual(followups.piled_up(now), 1)
        alert = Alert.objects.filter(ref_type="followup", ref_id__startswith="pile:").get()
        self.assertTrue(alert.spoken_text.index("Call the bank") < alert.spoken_text.index("Sign cheque"))  # urgent first
        self.assertIn("this order", alert.spoken_text)
        self.assertEqual(followups.piled_up(now), 0)     # told once


class TasksAreNotMeetingsTests(BaseCase):
    """Asked for tasks, JARVIS lists tasks; asked for meetings, meetings; asked for the day, everything."""

    def interpret(self, client, text, cmd):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = client.post("/api/v1/assistant/interpret/", {"text": text}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def setUp(self):
        super().setUp()
        today = tu.today_local()
        services.create_pa_task(self.md, {"title": "Send the urgent list", "kind": Kind.TASK, "priority": Priority.URGENT})
        services.create_pa_task(self.md, {"title": "Weekly sync", "kind": Kind.MEETING, "priority": Priority.NORMAL,
                                          "due_date": today, "due_time": time(23, 50)}, meeting_with_pa=True)

    def _titles(self, d):
        view = d["view"]
        return {r.get("title") for r in (view.get("rows") or view.get("items") or [])}

    def test_tasks_means_tasks_whoever_gave_them(self):
        day = tu.today_local().isoformat()
        d = self.interpret(self.pa_client, "aaj mere kya tasks hain",
                           _cmd(intent="query", view_kind="tasks", view_who="me", view_from=day, view_to=day, view_from_whom="me"))
        self.assertIn("Send the urgent list", d["spoken_reply"])
        self.assertNotIn("Weekly sync", d["spoken_reply"])

    def test_meetings_means_meetings(self):
        day = tu.today_local().isoformat()
        d = self.interpret(self.pa_client, "aaj meri meetings kya hain",
                           _cmd(intent="query", view_kind="tasks", view_who="me", view_from=day, view_to=day, view_types=["MEETING"]))
        self.assertIn("Weekly sync", d["spoken_reply"])
        self.assertNotIn("Send the urgent list", d["spoken_reply"])

    def test_task_or_meeting_asked_together_gives_both(self):
        day = tu.today_local().isoformat()
        d = self.interpret(self.pa_client, "aaj mere koi task ya meeting hai",
                           _cmd(intent="query", view_kind="tasks", view_who="me", view_from=day, view_to=day))
        self.assertIn("Send the urgent list", d["spoken_reply"])
        self.assertIn("Weekly sync", d["spoken_reply"])



class WhoseMeetingTests(BaseCase):
    """The PA books a meeting: it goes to the MD only when she says it is the MD's."""

    def interpret(self, client, text, cmd):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = client.post("/api/v1/assistant/interpret/", {"text": text}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def _cmd(self, intent, hour=15):
        day = (tu.today_local() + timedelta(days=1)).isoformat()
        return _cmd(intent=intent, title="Meeting with Neha", kind="MEETING", due_date=day, due_time=f"{hour}:00", engine="ai")

    def test_no_owner_said_is_her_own_and_never_goes_to_the_md(self):
        for hour, intent in enumerate(("meet_with_pa", "needs_destination", "request_for_md"), start=10):
            d = self.interpret(self.pa_client, "kal Neha ke saath meeting book kar do", self._cmd(intent, hour))
            self.assertTrue(d.get("booked"), (intent, d["spoken_reply"]))
        self.assertEqual(SharedTask.objects.filter(title="Meeting with Neha", origin=SharedTask.Origin.PA_SELF).count(), 3)
        self.assertFalse(TaskRequest.objects.filter(requested_by=self.pa).exists())

    def test_two_meetings_in_one_breath_are_one_card_for_the_pa(self):
        day = (tu.today_local() + timedelta(days=1)).isoformat()
        cmd = _cmd(intent="needs_destination", title="Meeting with Neha", kind="MEETING", due_date=day, due_time="16:00", engine="ai",
                   people=["Neha"], more_items=[{"intent": "needs_destination", "title": "Meeting with Kavita", "kind": "MEETING",
                                                 "due_date": day, "due_time": "18:00", "due_in_minutes": 0, "staff_name": "",
                                                 "people": ["Kavita"]}])
        d = self.interpret(self.pa_client, "kal 4 baje Neha ke saath meeting aur 6 baje Kavita ke saath", cmd)
        self.assertEqual(d["action"], "pa_self_task")
        self.assertFalse(d["needs_clarification"], d["spoken_reply"])
        self.assertEqual([b["title"] for b in d["batch"]], ["Meeting with Kavita"])
        self.assertIn("2 things", d["spoken_reply"])

    def test_my_meeting_is_her_own(self):
        for hour, intent in enumerate(("meet_with_pa", "needs_destination", "create_task"), start=10):
            d = self.interpret(self.pa_client, "kal meri Neha ke saath meeting book kar do", self._cmd(intent, hour))
            self.assertTrue(d.get("booked"), (intent, d["spoken_reply"]))
        self.assertEqual(SharedTask.objects.filter(title="Meeting with Neha", origin=SharedTask.Origin.PA_SELF).count(), 3)

    def test_the_mds_meeting_is_a_request(self):
        for text in ("kal 3 baje Ma'am ki Neha ke saath meeting book kar do", "कल 3 बजे मैम की नेहा के साथ मीटिंग"):
            d = self.interpret(self.pa_client, text, self._cmd("needs_destination"))
            self.assertEqual(d["action"], "md_request", text)


class DirectBookingTests(BaseCase):
    """A clear meeting of his own is booked and told, not asked; anything unclear is still asked first."""

    def interpret(self, client, text, cmd, **extra):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = client.post("/api/v1/assistant/interpret/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def _meeting(self, hour=16, **extra):
        day = _workday(2)
        return _cmd(**{"intent": "create_task", "title": "Meeting with Neha", "kind": "MEETING", "due_date": day.isoformat(),
                       "due_time": f"{hour}:00", "engine": "ai", **extra})

    def test_clear_and_free_is_booked_and_told(self):
        before = PrivateTask.objects.count()
        d = self.interpret(self.md_client, "parson 4 baje Neha ke saath meeting kar do", self._meeting())
        self.assertEqual(d["action"], "answer")
        self.assertTrue(d["booked"])
        self.assertIn("Booked", d["spoken_reply"])
        self.assertNotIn("Shall I save", d["spoken_reply"])
        self.assertEqual(PrivateTask.objects.count(), before + 1)

    def test_a_clash_is_asked_first_and_nothing_is_booked(self):
        day = _workday(2)
        services.create_private_task(self.md, {"title": "Board review", "kind": Kind.MEETING, "priority": Priority.NORMAL,
                                               "due_date": day, "due_time": time(16, 0), "duration_minutes": 30})
        before = PrivateTask.objects.count()
        d = self.interpret(self.md_client, "parson 4 baje Neha ke saath meeting kar do", self._meeting())
        self.assertEqual(d["action"], "private_task")
        self.assertIn("Board review", d["spoken_reply"])
        self.assertEqual(PrivateTask.objects.count(), before)

    def test_a_stranger_is_asked_about_first(self):
        before = PrivateTask.objects.count()
        d = self.interpret(self.md_client, "parson 4 baje Zubin ke saath meeting kar do",
                           self._meeting(title="Meeting with Zubin", people=["Zubin"]))
        self.assertTrue(d["needs_clarification"])
        self.assertEqual(PrivateTask.objects.count(), before)

    def test_a_rough_reading_never_books_by_itself(self):
        before = PrivateTask.objects.count()
        d = self.interpret(self.md_client, "parson 4 baje Neha ke saath meeting", self._meeting(engine="rules"))
        self.assertNotEqual(d.get("booked"), True)
        self.assertEqual(PrivateTask.objects.count(), before)

    def test_a_task_still_takes_its_yes(self):
        day = _workday(2)
        before = PrivateTask.objects.count()
        d = self.interpret(self.md_client, "parson 4 baje GST file karni hai mera personal",
                           _cmd(intent="create_task", title="File GST", kind="TASK", due_date=day.isoformat(), due_time="16:00", engine="ai"))
        self.assertEqual(d["action"], "private_task")
        self.assertEqual(PrivateTask.objects.count(), before)

