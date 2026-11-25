"""The TaskPanel api.php connection (contract v1, 23 Nov 2026) against a fake api.php: the signature, the staff
directory as JARVIS stores it, a task created once however often it is sent, progress from the events feed, the
reports for the voice and the panel screen, and the staff screens' endpoints."""
import hashlib
import hmac
import json
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from django.test import override_settings
from django.utils import timezone

from core import services
from core import timeutils as tu
from core.models import Alert, IntegrationCommand, SharedTask, StaffMember, TaskPanelLink
from core.taskpanel import gateway, sync
from core.tests import BaseCase

SECRET = "test-secret-0123456789abcdef0123456789abcdef"
TOKEN = "a" * 64
GATEWAY = dict(TASKPANEL_MODE="gateway", TASKPANEL_BASE_URL="https://shivtrinetra.in/taskPanel/api.php", TASKPANEL_API_TOKEN=TOKEN,
               TASKPANEL_API_SECRET=SECRET, TASKPANEL_WRITES_ENABLED=True, TASKPANEL_HIDE_KEYS=["staff_26"],
               TASKPANEL_EXPECTED_ACTOR="bijender", TASKPANEL_DEFAULT_CATEGORY="other", JARVIS_TASKPANEL_STAFF=True,
               TASKPANEL_DIRECTORY_TTL_SECONDS=0)


class FakeResponse:
    def __init__(self, status: int, body: dict):
        self.status_code, self._body = status, body
        self.content = json.dumps(body).encode()

    def json(self):
        return self._body


class FakeApi:
    """api.php as the contract describes it: it checks the signature, keeps the staff list, the tasks, the events and
    the command ids, and masks nothing (the real one does; the test checks only what JARVIS asks for)."""

    def __init__(self):
        self.staff = [
            {"user_key": "bijender", "name": "Mr. Bijender ( Admin)", "dept": "admin", "assignment_dept": "admin", "level": 3, "level_name": "Senior",
             "role": "admin", "active": True, "main": True, "on_leave": False, "leave_until": None, "open_tasks": 0},
            {"user_key": "nikki", "name": "NIKKI", "dept": "acc", "assignment_dept": "acc", "level": 3, "level_name": "Senior", "role": "exec",
             "active": True, "main": False, "on_leave": False, "leave_until": None, "open_tasks": 2},
            {"user_key": "kajal", "name": "Kajal", "dept": "cc", "assignment_dept": "cc", "level": 1, "level_name": "Junior", "role": "exec",
             "active": True, "main": False, "on_leave": True, "leave_until": (tu.today_local() + timedelta(days=2)).isoformat(), "open_tasks": 0},
            {"user_key": "staff_24", "name": "QTC Developer2", "dept": "admin", "assignment_dept": "admin", "level": 2, "level_name": "Executive",
             "role": "admin", "active": True, "main": False, "on_leave": False, "leave_until": None, "open_tasks": 1},
            {"user_key": "staff_25", "name": "QTC fed", "dept": "admin", "assignment_dept": "admin", "level": 2, "level_name": "Executive",
             "role": "viewer", "active": False, "main": False, "on_leave": False, "leave_until": None, "open_tasks": 0},
            {"user_key": "staff_26", "name": "Shivtrinetra Trading", "dept": "admin", "assignment_dept": "admin", "level": 2, "level_name": "Executive",
             "role": "admin", "active": True, "main": False, "on_leave": False, "leave_until": None, "open_tasks": 0},
        ]
        self.categories = [
            {"key": "other", "name": "Other", "dept": "", "default_priority": "normal", "assignable_to": ["nikki", "kajal", "staff_24", "staff_26"]},
            {"key": "tds", "name": "TDS / Report", "dept": "acc", "default_priority": "normal", "assignable_to": ["nikki"]},
        ]
        self.tasks: dict[str, dict] = {}
        self.events: list[dict] = []
        self.commands: dict[str, tuple[str, str]] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.next_id = 100
        self.bad = {}  # route -> (status, code) to force an error once

    def _signed(self, method, url, data, headers) -> bool:
        parts = urlsplit(url)
        query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        text = gateway.canonical(method, parts.path, query, headers["X-Jarvis-Timestamp"], headers["X-Jarvis-Nonce"], data or b"")
        return hmac.compare_digest(hmac.new(SECRET.encode(), text.encode(), hashlib.sha256).hexdigest(), headers["X-Jarvis-Signature"])

    def add_task(self, title: str, to: str, status: str = "todo", sender: str = "bijender", **extra) -> dict:
        self.next_id += 1
        tid = f"T-{self.next_id}"
        self.tasks[tid] = {"id": tid, "title": title, "status": status, "priority": extra.get("pri", "normal"), "category": extra.get("cat", "other"),
                           "assignee_key": to, "assignee_name": next((s["name"] for s in self.staff if s["user_key"] == to), to),
                           "sender_key": sender, "due_at": extra.get("due_at"), "created_at": timezone.now().isoformat(),
                           "updated_at": timezone.now().isoformat(), "completed_at": None, "hold_reason": "", "version": 1,
                           "detail": extra.get("detail", "")}
        self.events.append({"id": len(self.events) + 1, "task_id": tid, "kind": "created", "created_at": timezone.now().isoformat()})
        return self.tasks[tid]

    def progress(self, tid: str, kind: str, **changes):
        t = self.tasks[tid]
        t.update(changes)
        t["version"] += 1
        t["updated_at"] = timezone.now().isoformat()
        self.events.append({"id": len(self.events) + 1, "task_id": tid, "kind": kind, "created_at": timezone.now().isoformat()})

    def __call__(self, method, url, data=None, headers=None, **_kw):
        headers = headers or {}
        parts = urlsplit(url)
        query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        route = query.get("route", "")
        body = json.loads(data.decode()) if data else {}
        self.calls.append((method, route, {**query, **body}))
        if headers.get("X-Jarvis-Token") != TOKEN:
            return FakeResponse(401, {"ok": False, "request_id": "r", "error": {"code": "bad_token", "message": "The token is not valid."}})
        if not self._signed(method, url, data, headers):
            return FakeResponse(401, {"ok": False, "request_id": "r", "error": {"code": "bad_signature", "message": "The signature does not match."}})
        if route in self.bad:
            status, code = self.bad.pop(route)
            return FakeResponse(status, {"ok": False, "request_id": "r", "error": {"code": code, "message": code}})

        def ok(d, status=200):
            return FakeResponse(status, {"ok": True, "request_id": "r", "data": d})

        if route == "health":
            return ok({"service": "taskpanel-jarvis", "contract": 1, "actor": {"user_key": "bijender", "role": "admin"},
                       "scopes": ["staff:read", "tasks:read", "data:read", "tasks:create"], "writes_enabled": True,
                       "network": {"remote_addr": "2a02::1", "ip_allowed": True}})
        if route == "staff":
            return ok({"staff": self.staff, "categories": self.categories, "rules": {}})
        if route == "tasks":
            rows = list(self.tasks.values())
            if query.get("assignee"):
                rows = [t for t in rows if t["assignee_key"] == query["assignee"]]
            if query.get("status", "open") == "open":
                rows = [t for t in rows if t["status"] != "done"]
            elif query.get("status") not in ("all", ""):
                rows = [t for t in rows if t["status"] == query["status"]]
            return ok({"tasks": rows, "total": len(rows), "page": 1, "pages": 1})
        if route == "task":
            t = self.tasks.get(query.get("id", ""))
            if not t:
                return FakeResponse(404, {"ok": False, "request_id": "r", "error": {"code": "not_found", "message": "No such task."}})
            return ok({"task": t, "events": [e for e in self.events if e["task_id"] == t["id"]], "context": {}})
        if route == "task.events":
            since, limit = int(query.get("since_id", 0)), int(query.get("limit", 100))
            evs = [e for e in self.events if e["id"] > since][:limit]
            return ok({"events": evs, "next_since_id": evs[-1]["id"] if evs else since})
        if route == "datasets":
            return ok({"kinds": ["partners", "franchises", "franchise_requests", "payments"], "never_served": ["kyc"]})
        if route == "report":
            kind = query.get("kind")
            if kind == "partners":
                recs = [{"partner_id": "STTU001205", "name": "Sharma Traders", "city": "Pune", "status": "active", "phone": "[phone hidden]"},
                        {"partner_id": "STTU001300", "name": "Verma Agencies", "city": "Indore", "status": "pending", "phone": "[phone hidden]"}]
                if query.get("query"):
                    recs = [r for r in recs if query["query"].lower() in json.dumps(r).lower()]
                cols = [{"key": "partner_id", "label": "ID"}, {"key": "name", "label": "Name"}, {"key": "city", "label": "City"}, {"key": "status", "label": "Status"}]
                if query.get("result_mode") == "count":
                    return ok({"kind": kind, "title": "Partners", "columns": cols, "records": [], "total": len(recs), "shown": 0, "page": 1, "pages": 1})
                # A page at a time, like the real route (page_size is 50 unless a test makes the report long).
                size = getattr(self, "page_size", 50)
                page = max(1, int(query.get("page") or 1))
                pages = max(1, -(-len(recs) // size))
                shown = recs[(page - 1) * size:page * size]
                return ok({"kind": kind, "title": "Partners", "columns": cols, "records": shown, "totals": [], "total": len(recs), "shown": len(shown),
                           "page": page, "pages": pages, "hidden_columns": 3, "as_of": timezone.now().isoformat()})
            return FakeResponse(403, {"ok": False, "request_id": "r", "error": {"code": "kind_denied", "message": "Not for this account."}})
        if route == "task.create" and method == "POST":
            cid = body.get("command_id", "")
            digest = hashlib.sha256(json.dumps({k: v for k, v in body.items() if k != "command_id"}, sort_keys=True).encode()).hexdigest()
            if cid in self.commands:
                tid, seen = self.commands[cid]
                if seen != digest:
                    return FakeResponse(409, {"ok": False, "request_id": "r", "error": {"code": "command_conflict", "message": "Different content."}})
                return ok({"id": tid, "command_id": cid, "replayed": True, "task": self.tasks[tid]})
            person = next((s for s in self.staff if s["user_key"] == body.get("to")), None)
            if not person or person["role"] == "viewer" or not person["active"]:
                return FakeResponse(422, {"ok": False, "request_id": "r", "error": {"code": "unknown_staff", "message": "No such person."}})
            if person["on_leave"]:
                return FakeResponse(422, {"ok": False, "request_id": "r", "error": {"code": "not_eligible", "message": f"{person['name']} is on leave."}})
            for required in ("cat", "title", "to", "dueKind"):
                if not body.get(required):
                    return FakeResponse(422, {"ok": False, "request_id": "r", "error": {"code": "missing_field", "message": required}})
            due_at = None
            if body["dueKind"] == "custom":
                due_at = f"{body['dueDate']}T{body.get('dueTime', '18:30')}:00+05:30"
            t = self.add_task(body["title"], body["to"], pri=body.get("pri", "normal"), cat=body["cat"], due_at=due_at, detail=body.get("detail", ""))
            self.commands[cid] = (t["id"], digest)
            return ok({"id": t["id"], "command_id": cid, "replayed": False, "task": t}, 201)
        return FakeResponse(404, {"ok": False, "request_id": "r", "error": {"code": "unknown_route", "message": route}})


@override_settings(**GATEWAY)
class GatewayTests(BaseCase):
    def setUp(self):
        # The fake api.php goes in BEFORE the base fixture runs its directory sync, so no test ever reaches the network.
        gateway._shared = None
        sync._last_directory_sync["at"] = None
        self.api = FakeApi()
        self.client_obj = gateway.get_gateway()
        self.client_obj.session.request = self.api
        super().setUp()
        StaffMember.objects.all().delete()
        TaskPanelLink.objects.all().delete()
        services.sync_staff(self.md)

    def test_signature_matches_the_contracts_test_vector(self):
        text = gateway.canonical("GET", "/taskPanel/api.php", {"route": "tasks", "status": "open", "page": "1"}, "1795400000", "abcdefghijklmnop1234", b"")
        self.assertEqual(text, "JARVIS-HMAC-SHA256-V1\nGET\n/taskPanel/api.php\npage=1&route=tasks&status=open\n1795400000\nabcdefghijklmnop1234\n"
                               "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
        self.assertEqual(gateway.sign(SECRET, text), "09838672b311004d23c70483f6f63bd389ede655bb60fd9c5ca8b6cacbcfac40")
        # The fake api.php checks every call the same way: a wrong secret is refused.
        self.client_obj.secret = "wrong"
        with self.assertRaises(gateway.TaskPanelError) as ctx:
            self.client_obj.health()
        self.assertEqual(ctx.exception.code, "bad_signature")

    def test_the_directory_is_stored_as_people_with_departments_in_words(self):
        by_key = {s.external_id: s for s in StaffMember.objects.all()}
        self.assertNotIn("staff_26", by_key)                       # hidden: a company login, not a person
        self.assertEqual(by_key["nikki"].department, "Accounts")
        self.assertEqual((by_key["nikki"].level, by_key["nikki"].role, by_key["nikki"].open_tasks), ("Senior", "exec", 2))
        # A viewer is in the complete list (his rule, 25 Nov 2026) but the panel will not take work for them.
        self.assertEqual((by_key["staff_25"].active, by_key["staff_25"].role), (True, "viewer"))
        with self.assertRaises(gateway.TaskPanelError) as ctx:
            self.client_obj.validate(sync.link_for(self.md), {"data": {"title": "Test file", "assignee_key": "staff_25"}})
        self.assertEqual(ctx.exception.code, "recipient_inactive")
        self.assertIn("viewer", ctx.exception.message)
        self.assertTrue(by_key["kajal"].on_leave)
        # The MD's own link exists by itself in gateway mode, so the ledger and the feed have their actor.
        link = sync.link_for(self.md)
        self.assertEqual((link.actor_key, link.user_id), ("bijender", self.md.id))
        r = self.md_client.get("/api/v1/staff/")
        self.assertEqual(r.status_code, 200)
        rows = {x["key"]: x for x in r.json()["results"]}
        self.assertTrue(rows["bijender"]["self"])
        self.assertEqual(rows["nikki"]["open_tasks"], 2)
        self.assertTrue(r.json()["connected"])
        self.assertIn("other", {c["key"] for c in r.json()["categories"]})

    def test_a_task_is_created_once_with_the_right_body_and_retries_replay(self):
        staff = StaffMember.objects.get(external_id="staff_24")
        day = tu.today_local() + timedelta(days=1)
        body = {"title": "Test task from JARVIS", "staff_id": staff.id, "priority": "HIGH", "due_date": day.isoformat(), "due_time": "11:00",
                "description": "Shalu ko bolo kal 11 baje tak test task", "category_key": "other"}
        r = self.md_client.post("/api/v1/delegations/", body, format="json", HTTP_IDEMPOTENCY_KEY="gw-1")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["result"], "assigned")
        self.assertTrue(r.json()["remote_task_id"].startswith("T-"))
        sent = next(c for c in self.api.calls if c[1] == "task.create")[2]
        self.assertEqual((sent["cat"], sent["to"], sent["pri"], sent["dueKind"], sent["dueDate"], sent["dueTime"]),
                         ("other", "staff_24", "high", "custom", day.isoformat(), "11:00"))
        self.assertEqual(sent["detail"], "Shalu ko bolo kal 11 baje tak test task")
        self.assertEqual(len(self.api.tasks), 1)
        # The same command id sent again (a retry after a timeout) returns the same task: still one in TaskPanel.
        cmd = IntegrationCommand.objects.get()
        cmd.state, cmd.attempts, cmd.next_attempt_at = IntegrationCommand.State.OUTCOME_UNKNOWN, 1, timezone.now()
        cmd.save()
        from core.taskpanel import dispatcher
        dispatcher.dispatch(cmd.id)
        cmd.refresh_from_db()
        self.assertEqual(cmd.state, IntegrationCommand.State.SUCCEEDED)
        self.assertTrue(cmd.result["replayed"])
        self.assertEqual(len(self.api.tasks), 1)

    def test_someone_on_leave_or_a_wrong_category_is_refused_before_anything_is_sent(self):
        kajal = StaffMember.objects.get(external_id="kajal")
        r = self.md_client.post("/api/v1/delegations/", {"title": "x", "staff_id": kajal.id, "category_key": "other"}, format="json", HTTP_IDEMPOTENCY_KEY="gw-2")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["code"], "recipient_on_leave")
        dev = StaffMember.objects.get(external_id="staff_24")
        r = self.md_client.post("/api/v1/delegations/", {"title": "x", "staff_id": dev.id, "category_key": "tds"}, format="json", HTTP_IDEMPOTENCY_KEY="gw-3")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["code"], "category_recipient_conflict")
        self.assertFalse(any(c[1] == "task.create" for c in self.api.calls))

    def test_everyone_gets_one_task_each_in_category_other_and_leave_is_skipped(self):
        draft_members = [s for s in services.team_members("all")[0]]
        self.assertEqual({s.external_id for s in draft_members}, {"nikki", "staff_24"})  # kajal on leave, bijender is the MD, viewer out
        results = services.delegate_to_team(self.md, {"title": "Attendance form", "priority": "NORMAL"}, [s.id for s in draft_members],
                                            category_key="tds", description="sab ko bolo attendance form bhar do")
        self.assertEqual(sorted(results["assigned"]), ["NIKKI", "QTC Developer2"])
        self.assertEqual({c[2]["cat"] for c in self.api.calls if c[1] == "task.create"}, {"other"})
        self.assertEqual(len(self.api.tasks), 2)

    def test_progress_in_taskpanel_reaches_jarvis_as_an_alert(self):
        dev = StaffMember.objects.get(external_id="staff_24")
        r = self.md_client.post("/api/v1/delegations/", {"title": "Pilot", "staff_id": dev.id, "category_key": "other"}, format="json", HTTP_IDEMPOTENCY_KEY="gw-4")
        tid = r.json()["remote_task_id"]
        applied = sync.pull_all()                      # the creation event: nothing new
        self.api.progress(tid, "started", status="working")
        applied = sync.pull_all()
        self.assertGreaterEqual(applied, 1)
        task = SharedTask.objects.get(remote_task_uuid=tid)
        self.assertEqual((task.remote_status, task.status), ("working", "IN_PROGRESS"))
        self.api.progress(tid, "note")                  # a note: no status change, nothing rings, feed moves on
        self.api.progress(tid, "done", status="done", completed_at=timezone.now().isoformat())
        sync.pull_all()
        task.refresh_from_db()
        self.assertEqual((task.remote_status, task.status), ("done", "COMPLETED"))
        self.assertTrue(Alert.objects.filter(recipient=self.md, title="QTC Developer2 completed a task").exists())
        cursor = TaskPanelLink.objects.get(user=self.md).cursor
        self.assertEqual(cursor.cursor, str(self.api.events[-1]["id"]))

    def test_a_task_the_md_gave_in_taskpanel_itself_is_projected_too(self):
        self.api.add_task("Given in TaskPanel", "nikki", sender="bijender")
        sync.pull_all()
        self.assertTrue(SharedTask.objects.filter(assignee_type="STAFF", staff__external_id="nikki").exists())
        self.api.add_task("Given by Mansi", "nikki", sender="mansi")
        sync.pull_all()
        self.assertEqual(SharedTask.objects.filter(assignee_type="STAFF", staff__external_id="nikki").count(), 1)

    def test_the_person_screen_shows_everything_taskpanel_holds_for_them(self):
        self.api.add_task("From the web", "nikki", status="working")
        self.api.add_task("Old one", "nikki", status="done")
        nikki = StaffMember.objects.get(external_id="nikki")
        r = self.md_client.get(f"/api/v1/staff/{nikki.id}/tasks/")
        self.assertEqual(r.status_code, 200, r.content)
        card = r.json()
        self.assertEqual(card["staff"]["name"], "NIKKI")
        self.assertEqual(card["summary"], {"todo": 0, "working": 1, "waiting": 0, "done": 1, "open": 1, "overdue": 0})
        self.assertEqual([t["status_label"] for t in card["tasks"]], ["Working", "Completed"])
        self.assertTrue(card["live"])

    def test_the_panel_answers_partners_by_voice_and_only_for_the_md(self):
        from core import datacards
        view = datacards.build(self.md, "panel", params={"group": "partners"})
        self.assertEqual([r["title"] for r in view["rows"]], ["STTU001205", "STTU001300"])
        self.assertEqual([c["key"] for c in view["columns"]], ["partner_id", "name", "city", "status"])
        self.assertIn("2 partners: STTU001205, STTU001300", view["summary"])
        self.assertEqual(view["panel"]["hidden_columns"], 3)
        view = datacards.build(self.md, "panel", params={"group": "partners", "words": "Pune"})
        self.assertEqual([r["name"] for r in view["rows"]], ["Sharma Traders"])
        self.assertIn("for Pune", view["summary"])
        self.assertEqual(datacards.panel_kind("franchise requests"), "franchise_requests")
        self.assertEqual(datacards.panel_kind("partner ki list"), "partners")
        self.assertEqual(datacards.panel_kind("withdrawals"), "withdrawal_payouts")
        view = datacards.build(self.md, "panel", params={})
        self.assertIn("which report", view["ask"])
        view = datacards.build(self.pa, "panel", params={"group": "partners"})
        self.assertIn("only for the MD", view["summary"])
        r = self.md_client.get("/api/v1/views/panel/?group=franchises")
        self.assertEqual(r.status_code, 200)
        self.assertIn("could not read franchises", r.json()["summary"].lower())  # the fake serves only partners: the refusal is said, not hidden

    def test_the_panel_pages_through_a_long_report(self):
        # 6,816 partners come 20 a page: the screen's Next asks for page 2 and gets the second page, not page 1 again.
        self.api.page_size = 1
        r = self.md_client.get("/api/v1/views/panel/?group=partners&page=2")
        self.assertEqual(r.status_code, 200, r.content)
        view = r.json()
        self.assertEqual([row["title"] for row in view["rows"]], ["STTU001300"])
        self.assertEqual((view["panel"]["page"], view["panel"]["pages"]), (2, 2))
        self.assertIn("page 2 of 2", view["subtitle"])
        r = self.md_client.get("/api/v1/views/panel/?group=partners")  # no page asked: the first
        self.assertEqual([row["title"] for row in r.json()["rows"]], ["STTU001205"])

    def test_a_refusal_from_taskpanel_is_shown_and_nothing_is_saved(self):
        self.api.bad["task.create"] = (503, "writes_off")
        dev = StaffMember.objects.get(external_id="staff_24")
        r = self.md_client.post("/api/v1/delegations/", {"title": "x", "staff_id": dev.id, "category_key": "other"}, format="json", HTTP_IDEMPOTENCY_KEY="gw-5")
        self.assertEqual(r.status_code, 202)   # switched off on their side: queued, never claimed as assigned
        self.assertEqual(r.json()["result"], "pending_confirmation")
        self.assertEqual(len(self.api.tasks), 0)
