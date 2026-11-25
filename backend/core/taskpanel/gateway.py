"""The TaskPanel API for JARVIS (contract v1, 23 Nov 2026): one door, ``taskPanel/api.php``, reached with a token, a
timestamp, a one-time nonce and an HMAC signature over the whole request. One token, acting as the MD's own TaskPanel
account. Read: staff and categories, tasks, one task with its events, the events feed, the report kinds and the
reports (partners, franchises, payments… masked on their side). Write: task.create only, with a command id so a
retry never makes a second task.

GatewayTaskPanel keeps the shape the rest of the integration already uses (client.LiveTaskPanel / MockTaskPanel):
directory(), validate(), command(), command_status(), task(), snapshot(), changes(); plus person_tasks(), report()
and datasets() for the app's staff and panel screens.
"""
import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode, urlsplit

import requests
from django.conf import settings
from django.utils import timezone

from .. import timeutils as tu
from .client import TaskPanelError, _check_link

log = logging.getLogger("jarvis.taskpanel")

PREFIX = "JARVIS-HMAC-SHA256-V1"
# TaskPanel's department codes, as the staff and the categories carry them, in words.
DEPT_NAMES = {"acc": "Accounts", "cc": "Customer Care", "admin": "Admin", "mgmt": "Management", "ca": "CA",
              "tech": "Technical Team", "rec": "Reception"}
LEVEL_NAMES = {1: "Junior", 2: "Executive", 3: "Senior"}
# TaskPanel's event words -> the event types the projection understands (core/taskpanel/sync.py).
_EVENT_TYPES = [("start", "task.started"), ("working", "task.started"), ("done", "task.completed"), ("complet", "task.completed"),
                ("hold", "task.held"), ("wait", "task.held"), ("carry", "task.carried"), ("reassign", "task.reassigned"),
                ("note", "task.noted"), ("remind", "task.reminded"), ("creat", "task.created"), ("accept", "task.accepted")]
_RETRYABLE_CODES = {"command_in_progress", "disabled", "not_configured", "writes_off", "rate_limited"}
PAGE_SIZE = 100


def canonical(method: str, path: str, query: dict, ts: str, nonce: str, raw: bytes) -> str:
    """The signed text: 7 lines, the query sorted by name and rawurlencoded, the body as its SHA-256."""
    q = "&".join(f"{quote(str(k), safe='')}={quote(str(v), safe='')}" for k, v in sorted(query.items()))
    return "\n".join([PREFIX, method.upper(), path, q, ts, nonce, hashlib.sha256(raw).hexdigest()])


def sign(secret: str, text: str) -> str:
    return hmac.new(secret.encode(), text.encode(), hashlib.sha256).hexdigest()


def _event_type(word: str) -> str:
    low = (word or "").lower()
    for needle, etype in _EVENT_TYPES:
        if needle in low:
            return etype
    return "task.changed"


def _first(d: dict, *keys, default=None):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def _key_of(value) -> str:
    """A person as TaskPanel writes them in a task: a key, or {user_key, name}."""
    if isinstance(value, dict):
        return str(_first(value, "user_key", "key", default="") or "")
    return str(value or "")


def _name_of(value, fallback: str = "") -> str:
    if isinstance(value, dict):
        return str(_first(value, "name", default=fallback) or fallback)
    return fallback


class GatewayTaskPanel:
    mode = "gateway"

    def __init__(self):
        self.base = settings.TASKPANEL_BASE_URL
        self.path = urlsplit(self.base).path or "/taskPanel/api.php"
        self.token = settings.TASKPANEL_API_TOKEN
        self.secret = settings.TASKPANEL_API_SECRET
        self.session = requests.Session()
        self._directory = {"at": None, "data": None}

    # ------------------------------------------------------------------ the wire
    def describe(self) -> dict:
        configured = bool(self.base and self.token and self.secret)
        return {"mode": "gateway", "configured": configured, "label": "TaskPanel (api.php)" if configured else "TaskPanel not configured"}

    def _call(self, method: str, route: str, params: dict | None = None, body: dict | None = None) -> tuple[int, dict]:
        if not (self.base and self.token and self.secret):
            raise TaskPanelError(503, "not_configured", "TaskPanel connection is not configured on the server.")
        query = {"route": route, **{str(k): str(v) for k, v in (params or {}).items() if v not in (None, "")}}
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode() if body is not None else b""
        ts, nonce = str(int(time.time())), secrets.token_urlsafe(18)
        headers = {
            "X-Jarvis-Token": self.token, "X-Jarvis-Timestamp": ts, "X-Jarvis-Nonce": nonce,
            "X-Jarvis-Signature": sign(self.secret, canonical(method, self.path, query, ts, nonce, raw)),
            "Accept": "application/json", "User-Agent": "jarvis-server/1",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        url = self.base + "?" + urlencode(query, quote_via=quote, safe="")
        try:
            resp = self.session.request(method.upper(), url, data=raw or None, headers=headers, allow_redirects=False,
                                        timeout=(settings.TASKPANEL_CONNECT_TIMEOUT, settings.TASKPANEL_READ_TIMEOUT),
                                        verify=settings.TASKPANEL_VERIFY_TLS)
        except requests.ConnectTimeout as exc:
            raise TaskPanelError(0, "unreachable", "TaskPanel could not be reached.", retryable=True) from exc
        except requests.Timeout as exc:
            raise TaskPanelError(0, "timeout", "TaskPanel did not answer in time.", retryable=True, outcome_unknown=method != "GET") from exc
        except requests.ConnectionError as exc:
            raise TaskPanelError(0, "unreachable", "TaskPanel could not be reached.", retryable=True, outcome_unknown=method != "GET") from exc
        if 300 <= resp.status_code < 400:
            raise TaskPanelError(resp.status_code, "redirect", "TaskPanel redirected the request; check TASKPANEL_BASE_URL.")
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if resp.status_code >= 400 or data.get("ok") is False:
            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            code = str(err.get("code") or f"http_{resp.status_code}")
            message = str(err.get("message") or f"TaskPanel returned HTTP {resp.status_code}.")
            retryable = resp.status_code == 429 or resp.status_code >= 500 or code in _RETRYABLE_CODES
            raise TaskPanelError(resp.status_code, code, message, retryable=retryable,
                                 outcome_unknown=resp.status_code >= 500 and method != "GET", body=data)
        inner = data.get("data")
        return resp.status_code, inner if isinstance(inner, dict) else data

    # ------------------------------------------------------------------ reads
    def health(self, link=None) -> dict:
        return self._call("GET", "health")[1]

    def directory(self, link) -> dict:
        """The staff and the categories, in the shape sync.sync_directory stores: key, name, department, designation,
        active, on_leave, leave_until (plus role, level, open_tasks). Hidden keys (settings.TASKPANEL_HIDE_KEYS:
        test logins, company logins) are left out; a viewer cannot receive work, so counts as not active."""
        _check_link(link)
        cached = self._directory
        if cached["data"] is not None and cached["at"] and time.monotonic() - cached["at"] < settings.TASKPANEL_DIRECTORY_TTL_SECONDS:
            return cached["data"]
        _st, d = self._call("GET", "staff")
        hide = set(settings.TASKPANEL_HIDE_KEYS)
        staff = []
        for s in d.get("staff") or []:
            if not isinstance(s, dict):
                continue
            key = str(s.get("user_key") or "")
            if not key or key in hide or not s.get("name"):
                continue
            role = str(s.get("role") or "")
            dept_code = str(_first(s, "assignment_dept", "dept", default="") or "").lower()
            level_raw = s.get("level")
            level = str(_first(s, "level_name", default="") or LEVEL_NAMES.get(level_raw) or level_raw or "")
            staff.append({
                # Everyone the panel lists IS the staff (his rule, 25 Nov 2026: the complete list, always); whether
                # the panel will take work for them is a separate fact (panel_active, role), checked in validate().
                "key": key, "name": str(s["name"]).strip(), "department": DEPT_NAMES.get(dept_code, dept_code.title()),
                "designation": level, "level": level, "role": role,
                "active": True, "panel_active": bool(s.get("active", True)),
                "on_leave": bool(s.get("on_leave", False)), "leave_until": s.get("leave_until") or None,
                "open_tasks": int(s.get("open_tasks") or 0), "main": bool(s.get("main", False)),
            })
        categories = []
        for c in d.get("categories") or []:
            if not isinstance(c, dict) or not c.get("key"):
                continue
            dept_code = str(c.get("dept") or "").lower()
            categories.append({
                "key": str(c["key"]), "name": str(c.get("name") or c["key"]), "department": DEPT_NAMES.get(dept_code, dept_code.title()),
                "active": True, "default_priority": str(c.get("default_priority") or "normal"),
                "assignable_to": [str(k) for k in (c.get("assignable_to") or [])],
            })
        data = {"revision": str(d.get("revision") or ""), "staff": staff, "categories": categories, "rules": d.get("rules")}
        self._directory = {"at": time.monotonic(), "data": data}
        return data

    def validate(self, link, body: dict) -> dict:
        """TaskPanel's own creation rules, checked here first so a draft is questioned before anything is sent:
        the person is known, active and not on leave; the category exists and may go to that person."""
        _check_link(link)
        data = body.get("data") or {}
        directory = self.directory(link)
        staff = {s["key"]: s for s in directory["staff"]}
        cats = {c["key"]: c for c in directory["categories"]}
        person = staff.get(str(data.get("assignee_key") or ""))
        if not str(data.get("title") or "").strip():
            raise TaskPanelError(422, "title_required", "A title is required.")
        if not person or not person["active"]:
            raise TaskPanelError(422, "recipient_not_found", "That staff member is not in the TaskPanel directory.")
        # The panel's role / active flags are not trusted as a refusal here (25 Nov 2026: live reports Nikki, Mansi,
        # Kajal… as "viewer, inactive" while showing open tasks for them). The panel itself decides on task.create;
        # its own answer is what the speaker hears.
        if person["on_leave"]:
            until = person.get("leave_until")
            try:
                until_label = tu.fmt_day(datetime.fromisoformat(str(until)).date()) if until else "further notice"
            except ValueError:
                until_label = str(until)
            raise TaskPanelError(422, "recipient_on_leave", f"{person['name']} is on leave until {until_label}.")
        cat = cats.get(str(data.get("category_key") or ""))
        if not cat:
            raise TaskPanelError(422, "category_unknown", "Choose a valid task category.")
        allowed = cat.get("assignable_to") or []
        if (allowed and person["key"] not in allowed) or (not allowed and cat["department"] and cat["department"] != person["department"]):
            raise TaskPanelError(422, "category_recipient_conflict",
                                 f"{cat['name']} tasks go to {cat['department'] or 'its own department'}, but {person['name']} is in "
                                 f"{person['department'] or 'another department'}. Use Other, or choose someone from {cat['department']}.")
        if data.get("priority") not in {"normal", "high", "urgent"}:
            raise TaskPanelError(422, "priority_invalid", "Priority must be Normal, High or Urgent.")
        if data.get("due_at"):
            try:
                if datetime.fromisoformat(str(data["due_at"])) < timezone.now():
                    raise TaskPanelError(422, "due_in_past", "That time has already passed.")
            except ValueError:
                pass
        return {"valid": True, "warnings": [], "normalized": data}

    # ------------------------------------------------------------------ one task, as the projection reads it
    @staticmethod
    def _snap(t: dict, task_id: str = "") -> dict:
        tid = str(_first(t, "id", "task_id", default=task_id) or task_id)
        status = str(t.get("status") or "").lower()
        snap = {
            "uuid": tid, "legacy_id": tid, "status": status if status in ("todo", "working", "waiting", "done") else status,
            "priority": str(t.get("priority") or "normal").lower(),
            "assignee_key": _key_of(_first(t, "assignee_key", "assignee", "to", default="")),
            "assignee_name": _name_of(t.get("assignee"), str(t.get("assignee_name") or "")),
            "sender_key": _key_of(_first(t, "sender_key", "sender", "from", default="")),
            "sender_name": _name_of(t.get("sender"), str(t.get("sender_name") or "")),
            "title": str(t.get("title") or ""), "version": int(t.get("version") or 0),
            "hold_reason": str(t.get("hold_reason") or ""), "category_key": str(_first(t, "category", "category_key", "cat", default="") or ""),
            "due_at": _first(t, "due_at", "due", default=None), "created_at": t.get("created_at"), "updated_at": t.get("updated_at"),
            "completed_at": t.get("completed_at") or None, "detail": str(t.get("detail") or ""),
        }
        return snap

    def task(self, link, task_id: str) -> dict:
        _check_link(link)
        _st, d = self._call("GET", "task", {"id": task_id})
        t = d.get("task") if isinstance(d.get("task"), dict) else d
        return {"task": self._snap(t, task_id), "events": d.get("events") or [], "context": d.get("context") or {}}

    def snapshot(self, link, page: str = "") -> dict:
        _check_link(link)
        n = int(page) if str(page).isdigit() else 1
        _st, d = self._call("GET", "tasks", {"status": "all", "page": n, "page_size": PAGE_SIZE})
        tasks = [self._snap(t) for t in d.get("tasks") or [] if isinstance(t, dict)]
        pages = int(d.get("pages") or 1)
        return {"tasks": tasks, "next_page": str(n + 1) if n < pages else None, "high_water_cursor": ""}

    def changes(self, link, cursor: str, limit: int = 100) -> dict:
        """TaskPanel's task-events feed as the projection's events: one per event, newest state of the task fetched
        once per task in the batch (a note or a reminder does not move the task's updated time, the feed does)."""
        _check_link(link)
        since = int(cursor) if str(cursor or "").isdigit() else 0
        _st, d = self._call("GET", "task.events", {"since_id": since, "limit": max(1, min(int(limit), 200))})
        raw_events = [e for e in (d.get("events") or []) if isinstance(e, dict)]
        snaps: dict[str, dict | None] = {}
        events = []
        for raw in raw_events:
            eid = _first(raw, "id", "event_id", default=None)
            tid = _key_of(_first(raw, "task_id", "task", default=""))
            if isinstance(raw.get("task"), dict):
                tid = str(raw["task"].get("id") or tid)
            if eid is None or not tid:
                continue
            if tid not in snaps:
                try:
                    snaps[tid] = self.task(link, tid)["task"]
                except TaskPanelError as err:
                    if err.status == 404:
                        snaps[tid] = None
                    else:
                        raise
            snap = snaps[tid]
            if snap is None:
                continue
            events.append({
                "schema_version": 1, "event_id": f"tp-{eid}",
                "type": _event_type(str(_first(raw, "kind", "type", "action", "event", default=""))),
                "task_uuid": tid, "task_version": snap.get("version"), "sync_revision": int(eid), "origin": "taskpanel",
                "occurred_at": _first(raw, "created_at", "at", "occurred_at", default=None),
                "note": str(_first(raw, "note", "text", "detail", default="") or "")[:500],
                "task": {**snap, "sync_revision": int(eid)},
            })
        next_cursor = str(d.get("next_since_id") or (int(raw_events[-1].get("id") or since) if raw_events and str(raw_events[-1].get("id") or "").isdigit() else since))
        return {"events": events, "next_cursor": next_cursor, "has_more": len(raw_events) >= limit, "server_time": timezone.now().isoformat()}

    # ------------------------------------------------------------------ writes
    def command(self, link, command: dict) -> tuple[int, dict]:
        """task.create with the frozen command: the same command id on a retry gives the same task back."""
        _check_link(link, write=True)
        if command.get("type") != "task.create":
            raise TaskPanelError(422, "unsupported_command", "Only task.create is enabled in this phase.")
        data = command.get("data") or {}
        self.validate(link, {"data": data})  # the known refusals (leave, department) in JARVIS's words, before a call
        body = {
            "command_id": str(command["command_id"]),
            "cat": str(data.get("category_key") or settings.TASKPANEL_DEFAULT_CATEGORY),
            "title": str(data.get("title") or "")[:500],
            "to": str(data.get("assignee_key") or ""),
        }
        if data.get("priority") in ("normal", "high", "urgent"):
            body["pri"] = data["priority"]
        if data.get("description"):
            body["detail"] = str(data["description"])[:10000]
        if data.get("remarks"):
            body["remark"] = str(data["remarks"])[:10000]
        due_at, due_date = data.get("due_at"), data.get("due_date")
        if due_at:
            try:
                local = datetime.fromisoformat(str(due_at)).astimezone(tu.IST)
                body.update({"dueKind": "custom", "dueDate": local.date().isoformat(), "dueTime": local.strftime("%H:%M")})
            except ValueError:
                body["dueKind"] = "eod"
        elif due_date:
            body.update({"dueKind": "custom", "dueDate": str(due_date)[:10]})
        else:
            body["dueKind"] = "eod"
        # A deadline already behind us (a late item given on): TaskPanel refuses the past, so it is due today.
        if body.get("dueKind") == "custom" and body.get("dueDate", "") < tu.today_local().isoformat():
            body["dueDate"] = tu.today_local().isoformat()
            body.pop("dueTime", None)
        status, d = self._call("POST", "task.create", body=body)
        task_json = d.get("task") if isinstance(d.get("task"), dict) else {}
        tid = str(d.get("id") or task_json.get("id") or "")
        if not tid:
            raise TaskPanelError(502, "unexpected_state", "TaskPanel did not return the task id.", retryable=True, outcome_unknown=True)
        snap = self._snap({**task_json, "id": tid, "assignee_key": task_json.get("assignee_key") or body["to"],
                           "title": task_json.get("title") or body["title"]}, tid)
        return status, {"command_id": str(d.get("command_id") or command["command_id"]), "state": "succeeded",
                        "replayed": bool(d.get("replayed")), "task": snap}

    def command_status(self, link, command_id: str) -> dict:
        """There is no receipt route: a retry simply sends the same command id again and gets the same task."""
        _check_link(link)
        raise TaskPanelError(404, "command_not_found", "TaskPanel keeps no separate receipt; resend the same command id.")

    # ------------------------------------------------------------------ for the app's screens and the voice
    def person_tasks(self, user_key: str, status: str = "all") -> list[dict]:
        """One person's tasks as TaskPanel holds them now, newest first, for the staff screen."""
        _st, d = self._call("GET", "tasks", {"status": status, "assignee": user_key, "page": 1, "page_size": PAGE_SIZE})
        return [self._snap(t) for t in d.get("tasks") or [] if isinstance(t, dict)]

    def tasks_since(self, since: datetime, status: str = "all") -> list[dict]:
        _st, d = self._call("GET", "tasks", {"status": status, "since": since.astimezone(tu.IST).isoformat(), "page": 1, "page_size": PAGE_SIZE})
        return [self._snap(t) for t in d.get("tasks") or [] if isinstance(t, dict)]

    def datasets(self) -> dict:
        _st, d = self._call("GET", "datasets")
        return d

    def report(self, kind: str, params: dict | None = None) -> dict:
        """One report kind with the chatbot's filters (query, from, to, page, result_mode…), masked on their side."""
        _st, d = self._call("GET", "report", {"kind": kind, "result_mode": "list", **(params or {})})
        return d


_shared: GatewayTaskPanel | None = None


def get_gateway() -> GatewayTaskPanel:
    """One client per process: its directory cache and its HTTP session are reused."""
    global _shared
    if _shared is None:
        _shared = GatewayTaskPanel()
    return _shared


__all__ = ["GatewayTaskPanel", "get_gateway", "canonical", "sign", "DEPT_NAMES", "timedelta"]
