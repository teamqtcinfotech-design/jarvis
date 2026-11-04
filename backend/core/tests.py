"""Release-blocking tests: privacy boundary, approval flow, idempotency, parser."""
import json
from datetime import datetime, timedelta

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
        self.assertIn("meeting with Priya", md_rem[0]["spoken"])

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
        "category_key": "", "target_ref": "", "target_refs": [], "needs_clarification": False, "clarification_question": "",
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

    def test_server_built_confirmation_is_spoken_as_written(self):
        draft, _ = self.interpret(self.md_client, "Ramesh ko bolo kal GST file bheje", _cmd(
            intent="assign_to_staff", title="GST file bhejna", staff_name="Ramesh Kumar",
            spoken_reply="Sir, Ramesh ko GST file ka task de doon?", reply_language="hi",
            spoken_reply_hi="सर, रमेश को जीएसटी फाइल का टास्क दे दूँ?", engine="rules"))
        self.assertEqual(draft["action"], "delegation")
        self.assertEqual(draft["speak_lang"], "en")  # the server rewrote the reply, so no stale Hindi version
        self.assertEqual(draft["speak_text"], draft["spoken_reply"])

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
        draft, _ = self.interpret(self.md_client, "kal 4 baje Sharma ji ke saath meeting", _cmd(
            intent="create_task", title="Meeting with Sharma ji", kind="MEETING", reply_language="hi",
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
