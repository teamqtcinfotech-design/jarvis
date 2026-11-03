"""TaskPanel integration client.

LiveTaskPanel talks to /taskPanel/integration/v1 with signed requests.
MockTaskPanel implements the same contract in-process ("contract mode", guide §11.1)
so the app can be demonstrated and tested before the PHP side is deployed.
"""
import hashlib
import logging
import uuid
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

import requests
from django.conf import settings
from django.utils import timezone

from .. import timeutils as tu
from .signing import sign

log = logging.getLogger("jarvis.taskpanel")


class TaskPanelError(Exception):
    """A safe-to-show error. outcome_unknown=True means the request may have been committed."""

    def __init__(self, status: int, code: str, message: str, *, retryable: bool = False,
                 outcome_unknown: bool = False, body: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown
        self.body = body or {}


def _check_link(link, write: bool = False):
    if link is None or not link.active:
        raise TaskPanelError(403, "not_linked", "This account is not linked to TaskPanel yet.")
    if link.expires_at and link.expires_at <= timezone.now():
        raise TaskPanelError(401, "grant_expired", "The TaskPanel link has expired. Please re-link this account.")
    if write and not settings.TASKPANEL_WRITES_ENABLED:
        raise TaskPanelError(503, "writes_disabled", "TaskPanel writes are switched off on this server.")


class LiveTaskPanel:
    mode = "live"

    def __init__(self):
        self.base = settings.TASKPANEL_BASE_URL
        self.key_id = settings.TASKPANEL_KEY_ID
        self.secret = settings.TASKPANEL_SIGNING_SECRET
        self.session = requests.Session()

    def describe(self) -> dict:
        configured = bool(self.base and self.key_id and self.secret)
        return {"mode": "live", "configured": configured, "label": "TaskPanel live" if configured else "TaskPanel not configured"}

    def _request(self, method: str, path: str, link, payload=None, params: dict | None = None):
        if not (self.base and self.key_id and self.secret):
            raise TaskPanelError(503, "not_configured", "TaskPanel connection is not configured on the server.")
        url = f"{self.base}{path}"
        if params:
            url += "?" + urlencode({k: v for k, v in params.items() if v not in (None, "")})
        parts = urlsplit(url)
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        body, headers = sign(method, target, payload, self.key_id, self.secret, link.grant)
        try:
            resp = self.session.request(
                method, url, data=body if body else None, headers=headers, allow_redirects=False,
                timeout=(settings.TASKPANEL_CONNECT_TIMEOUT, settings.TASKPANEL_READ_TIMEOUT),
                verify=settings.TASKPANEL_VERIFY_TLS,
            )
        except requests.ConnectTimeout as exc:
            raise TaskPanelError(0, "unreachable", "TaskPanel could not be reached.", retryable=True) from exc
        except requests.Timeout as exc:
            raise TaskPanelError(0, "timeout", "TaskPanel did not answer in time.", retryable=True,
                                 outcome_unknown=method != "GET") from exc
        except requests.ConnectionError as exc:
            raise TaskPanelError(0, "unreachable", "TaskPanel could not be reached.", retryable=True,
                                 outcome_unknown=method != "GET") from exc
        if 300 <= resp.status_code < 400:
            raise TaskPanelError(resp.status_code, "redirect", "TaskPanel redirected the request; check TASKPANEL_BASE_URL.")
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {}
        if resp.status_code >= 400:
            code = str(data.get("code") or f"http_{resp.status_code}")
            message = str(data.get("message") or f"TaskPanel returned HTTP {resp.status_code}.")
            retryable = resp.status_code == 429 or resp.status_code >= 500
            raise TaskPanelError(resp.status_code, code, message, retryable=retryable,
                                 outcome_unknown=retryable and method != "GET", body=data)
        return resp.status_code, data

    def health(self, link):
        return self._request("GET", "/health", link)[1]

    def directory(self, link):
        _check_link(link)
        return self._request("GET", "/directory", link)[1]

    def validate(self, link, body: dict):
        _check_link(link)
        return self._request("POST", "/drafts/validate", link, payload=body)[1]

    def command(self, link, command: dict):
        _check_link(link, write=True)
        return self._request("POST", "/commands", link, payload=command)

    def command_status(self, link, command_id: str):
        _check_link(link)
        return self._request("GET", f"/commands/{command_id}", link)[1]

    def task(self, link, task_uuid: str):
        _check_link(link)
        return self._request("GET", f"/tasks/{task_uuid}", link)[1]

    def snapshot(self, link, page: str = ""):
        _check_link(link)
        return self._request("GET", "/tasks/snapshot", link, params={"page": page})[1]

    def changes(self, link, cursor: str, limit: int = 100):
        _check_link(link)
        return self._request("GET", "/changes", link, params={"cursor": cursor, "limit": limit})[1]


# --------------------------------------------------------------------------- contract double
MOCK_NS = uuid.UUID("6f1d8f4e-2b7a-4c0e-9a51-3c2d7e9b1a00")


def _mock_directory():
    today = tu.today_local()
    staff = [
        ("staff_101", "Ramesh Kumar", "Accounts", "Senior Accountant", None),
        ("staff_102", "Ramesh Yadav", "Logistics", "Dispatch Lead", None),
        ("staff_103", "Suresh Patel", "Operations", "Operations Manager", None),
        ("staff_104", "Anil Verma", "Sales", "Sales Head", None),
        ("staff_105", "Neha Gupta", "HR", "HR Executive", None),
        ("staff_106", "Kavita Singh", "Franchise", "Franchise Coordinator", None),
        ("staff_107", "QTC Developer", "IT", "Developer", None),
        ("staff_108", "QTC Developer2", "IT", "Developer", None),
        ("staff_109", "Nikki", "Accounts", "Accounts Executive", today + timedelta(days=3)),
        ("staff_110", "Pooja Mehra", "Customer Care", "Customer Care Executive", None),
        ("staff_002", "Priya", "Admin", "Personal Assistant", None),
    ]
    categories = [
        ("general", "General", ""),
        ("login", "Login issue", "IT"),
        ("accounts", "Accounts & GST", "Accounts"),
        ("profile_change", "Profile Change", "Customer Care"),
        ("franchise", "Franchise", "Franchise"),
        ("hr", "HR & hiring", "HR"),
        ("sales", "Sales follow-up", "Sales"),
    ]
    return {
        "revision": "mock-1",
        "staff": [
            {"key": k, "name": n, "department": d, "designation": g, "active": True,
             "on_leave": bool(leave and leave >= today), "leave_until": leave.isoformat() if leave else None}
            for k, n, d, g, leave in staff
        ],
        "categories": [{"key": k, "name": n, "department": d, "active": True} for k, n, d in categories],
    }


class MockTaskPanel:
    """Same contract as LiveTaskPanel. Applies the TaskPanel creation rules described in the guide."""

    mode = "mock"

    def describe(self) -> dict:
        return {"mode": "mock", "configured": True, "label": "TaskPanel test mode"}

    def health(self, link=None):
        return {"status": "ok", "environment": "mock", "instance": "taskpanel-mock-01"}

    def directory(self, link):
        _check_link(link)
        return _mock_directory()

    def _problems(self, data: dict) -> tuple[str, str] | None:
        directory = _mock_directory()
        staff = {s["key"]: s for s in directory["staff"]}
        cats = {c["key"]: c for c in directory["categories"]}
        person = staff.get(data.get("assignee_key"))
        if not data.get("title", "").strip():
            return "title_required", "A title is required."
        if not person or not person["active"]:
            return "recipient_not_found", "That staff member is not in the TaskPanel directory."
        if person["on_leave"]:
            until = tu.fmt_day(tu.date.fromisoformat(person["leave_until"])) if person["leave_until"] else "further notice"
            return "recipient_on_leave", f"{person['name']} is on leave until {until}."
        cat = cats.get(data.get("category_key") or "")
        if not cat:
            return "category_unknown", "Choose a valid task category."
        if cat["department"] and cat["department"] != person["department"]:
            return ("category_recipient_conflict",
                    f"{cat['name']} tasks go to {cat['department']}, but {person['name']} is in {person['department']}. "
                    f"Use General, or choose someone from {cat['department']}.")
        if data.get("priority") not in {"normal", "high", "urgent"}:
            return "priority_invalid", "Priority must be Normal, High or Urgent."
        if data.get("due_at"):
            from datetime import datetime
            if datetime.fromisoformat(data["due_at"]) < timezone.now():
                return "due_in_past", "That time has already passed."
        return None

    def validate(self, link, body: dict):
        _check_link(link)
        problem = self._problems(body.get("data", {}))
        if problem:
            raise TaskPanelError(422, problem[0], problem[1])
        return {"valid": True, "warnings": [], "normalized": body.get("data", {})}

    def _snapshot_for(self, command_id: str, data: dict, revision: int = 1, status: str = "todo") -> dict:
        task_uuid = str(uuid.uuid5(MOCK_NS, command_id))
        legacy = 1000 + int(hashlib.sha256(task_uuid.encode()).hexdigest()[:6], 16) % 9000
        return {
            "uuid": task_uuid, "legacy_id": f"T-{legacy}", "status": status,
            "assignee_key": data.get("assignee_key"), "priority": data.get("priority", "normal"),
            "version": revision, "sync_revision": revision,
        }

    def command(self, link, command: dict):
        _check_link(link, write=True)
        if command.get("type") != "task.create":
            raise TaskPanelError(422, "unsupported_command", "Only task.create is enabled in this phase.")
        problem = self._problems(command.get("data", {}))
        if problem:
            raise TaskPanelError(422, problem[0], problem[1])
        snap = self._snapshot_for(command["command_id"], command["data"])
        return 201, {"command_id": command["command_id"], "state": "succeeded", "replayed": False, "task": snap}

    def command_status(self, link, command_id: str):
        _check_link(link)
        from ..models import IntegrationCommand

        cmd = IntegrationCommand.objects.filter(id=command_id, state=IntegrationCommand.State.SUCCEEDED).first()
        if not cmd or not cmd.result:
            raise TaskPanelError(404, "command_not_found", "TaskPanel has no record of this command.")
        return {**cmd.result, "replayed": True}

    def _progress(self, task) -> tuple[int, str, str]:
        """Simulated staff progress for demos: todo -> working (2 min) -> done (5 min)."""
        if not settings.TASKPANEL_MOCK_PROGRESS:
            return max(task.sync_revision, 1), task.remote_status or "todo", ""
        age = timezone.now() - task.created_at
        if age > timedelta(minutes=5):
            return 3, "done", "Task completed"
        if age > timedelta(minutes=2):
            return 2, "working", ""
        return 1, "todo", ""

    def _actor_tasks(self, link):
        from ..models import SharedTask

        return SharedTask.objects.filter(
            created_by=link.user, assignee_type=SharedTask.Assignee.STAFF, remote_task_uuid__isnull=False
        ).select_related("staff")

    def _task_snap(self, task, revision: int, status: str, note: str) -> dict:
        snap = {
            "uuid": task.remote_task_uuid, "legacy_id": task.remote_legacy_id, "status": status,
            "assignee_key": task.staff.external_id if task.staff else None, "sender_key": None,
            "priority": {"URGENT": "urgent", "HIGH": "high"}.get(task.priority, "normal"),
            "title": task.title, "version": revision, "sync_revision": revision,
        }
        if status == "done":
            snap["completed_at"] = (task.created_at + timedelta(minutes=5)).isoformat()
            snap["completion_note"] = note
        return snap

    def task(self, link, task_uuid: str):
        _check_link(link)
        task = self._actor_tasks(link).filter(remote_task_uuid=task_uuid).first()
        if not task:
            raise TaskPanelError(404, "task_not_found", "Task not found.")
        rev, status, note = self._progress(task)
        return {"task": self._task_snap(task, rev, status, note)}

    def snapshot(self, link, page: str = ""):
        _check_link(link)
        tasks = []
        for task in self._actor_tasks(link):
            rev, status, note = self._progress(task)
            tasks.append(self._task_snap(task, rev, status, note))
        return {"tasks": tasks, "next_page": None, "high_water_cursor": f"mock:{timezone.now().isoformat()}"}

    def changes(self, link, cursor: str, limit: int = 100):
        _check_link(link)
        events = []
        for task in self._actor_tasks(link):
            target_rev, status, note = self._progress(task)
            for rev in range(task.sync_revision + 1, target_rev + 1):
                rev_status = {1: "todo", 2: "working", 3: "done"}[rev]
                events.append({
                    "schema_version": 1,
                    "event_id": str(uuid.uuid5(MOCK_NS, f"{task.remote_task_uuid}:{rev}")),
                    "source_instance": "taskpanel-mock-01",
                    "environment": "mock",
                    "type": {"todo": "task.created", "working": "task.started", "done": "task.completed"}[rev_status],
                    "task_uuid": task.remote_task_uuid,
                    "task_version": rev,
                    "sync_revision": rev,
                    "origin": "taskpanel_web",
                    "causation_command_id": None,
                    "occurred_at": timezone.now().isoformat(),
                    "task": self._task_snap(task, rev, rev_status, note),
                })
        return {"events": events[:limit], "next_cursor": f"mock:{timezone.now().isoformat()}",
                "has_more": len(events) > limit, "server_time": timezone.now().isoformat()}


def get_client():
    return LiveTaskPanel() if settings.TASKPANEL_MODE == "live" else MockTaskPanel()
