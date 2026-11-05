"""What the phone draws when JARVIS answers with data: a day timeline, a week chart, stat tiles, tables.

Every view is built from ``services.dashboard(user)``, so the privacy rules hold by construction: Mansi's views
never contain Sir's private items, and a locked phone (include_private=False) leaves them out for Sir too.
The phone only draws; nothing here changes data.
"""
from datetime import date, datetime, time, timedelta

from django.utils import timezone

from . import scheduling, services
from . import timeutils as tu
from .models import Kind, PrivateTask, SharedTask, Status

VIEW_KINDS = ["today", "week", "late", "upcoming", "nodate", "stats", "private", "pa", "staff", "requests", "busy",
              "performance", "done"]

_PROGRESS = {Status.OPEN: 0, Status.IN_PROGRESS: 50, Status.BLOCKED: 50, Status.COMPLETED: 100, Status.CANCELLED: 0}
_REMOTE_PROGRESS = {"todo": 0, "working": 50, "waiting": 50, "done": 100}
_KIND_WORD = {Kind.TASK: "Task", Kind.MEETING: "Meeting", Kind.CALL: "Call", Kind.PAYMENT: "Payment", Kind.FOLLOW_UP: "Follow-up"}


def _minutes(dt: datetime) -> int:
    local = tu.to_local(dt)
    return local.hour * 60 + local.minute


def _row(item: dict, include_private: bool) -> dict | None:
    if item.get("scope") == "private" and not include_private:
        return None
    start = item.get("scheduled_start") or item.get("due_at")
    when = tu.fmt_time(datetime.fromisoformat(start)) if start else (item.get("due_label") or "")
    status = item.get("status") or Status.OPEN
    progress = _PROGRESS.get(status, 0)
    if item.get("assignee", {}).get("type") == "STAFF" and item.get("remote_status"):
        progress = _REMOTE_PROGRESS.get(item["remote_status"], progress)
    who = item.get("assignee", {}).get("staff_name") or ""
    return {
        "id": item["id"], "scope": item.get("scope", "shared"), "title": item["title"], "kind": _KIND_WORD.get(item.get("kind"), "Task"),
        "priority": item.get("priority", "NORMAL"), "status": item.get("status_label") or status.replace("_", " ").title(),
        "when": when, "day": item.get("due_label") or "", "who": who, "progress": progress,
        "private": item.get("scope") == "private", "overdue": bool(item.get("overdue")), "version": item.get("version"),
        "hold_reason": item.get("hold_reason") or "",
    }


def _rows(items, include_private: bool) -> list[dict]:
    return [r for r in (_row(i, include_private) for i in items) if r]


def _table(kind: str, title: str, rows: list[dict], columns: list[tuple[str, str]], subtitle: str = "", layout: str = "table") -> dict:
    return {
        "kind": kind, "layout": layout, "title": title, "subtitle": subtitle,
        "columns": [{"key": k, "label": label} for k, label in columns], "rows": rows,
        "stats": [], "timeline": [], "series": [],
        "file_name": f"jarvis-{kind}-{tu.today_local().isoformat()}",
        "text": _text(title, rows, [k for k, _ in columns]),
    }


def _text(title: str, rows: list[dict], keys: list[str]) -> str:
    lines = [title]
    for r in rows:
        bits = [str(r.get(k, "")) for k in keys if r.get(k)]
        lines.append("• " + " · ".join(bits))
    return "\n".join(lines)


def _day_view(user, dash: dict, include_private: bool, prefs: dict, day: date) -> dict:
    items = dash["today"] + [i for i in dash["overdue"]]
    timeline, anytime = [], []
    o_start, o_end = scheduling.office_hours(prefs, day)
    for item in items:
        if item.get("scope") == "private" and not include_private:
            continue
        start = item.get("scheduled_start") or item.get("due_at")
        if start:
            s = datetime.fromisoformat(start)
            if tu.to_local(s).date() != day:
                continue
            end_iso = item.get("scheduled_end")
            e = datetime.fromisoformat(end_iso) if end_iso else s + timedelta(minutes=scheduling.minutes_for(item.get("kind"), item.get("estimated_minutes")))
            timeline.append({
                "id": item["id"], "scope": item.get("scope", "shared"), "title": item["title"], "kind": _KIND_WORD.get(item.get("kind"), "Task"),
                "priority": item.get("priority", "NORMAL"), "start": tu.fmt_time(s), "end": tu.fmt_time(e),
                "start_min": _minutes(s), "end_min": max(_minutes(e), _minutes(s) + 15), "private": item.get("scope") == "private",
                "routine": False, "overdue": bool(item.get("overdue")), "version": item.get("version"),
            })
        else:
            r = _row(item, include_private)
            if r:
                anytime.append(r)
    for block in scheduling.routine_blocks(prefs, day):
        timeline.append({
            "id": "", "scope": "", "title": block["title"], "kind": "Routine", "priority": "LOW", "start": tu.fmt_time(block["start"]),
            "end": tu.fmt_time(block["end"]), "start_min": _minutes(block["start"]), "end_min": _minutes(block["end"]),
            "private": False, "routine": True, "overdue": False, "version": None,
        })
    timeline.sort(key=lambda t: t["start_min"])
    meetings = sum(1 for t in timeline if t["kind"] == "Meeting")
    free = scheduling.free_slots(user, day, 30, prefs)
    free_minutes = sum(int((e - s).total_seconds() // 60) for s, e in free)
    load = scheduling.day_load(user, day, prefs)
    label = tu.fmt_day(day)
    title = f"{label[:1].upper()}{label[1:]}, {day.strftime('%a %d %b')}" if day != tu.today_local() else f"Today, {day.strftime('%a %d %b')}"
    rows = [{"id": t["id"], "scope": t["scope"], "title": t["title"], "when": f"{t['start']}–{t['end']}", "kind": t["kind"],
             "priority": t["priority"], "private": t["private"], "status": "", "who": "", "progress": 0, "overdue": t["overdue"],
             "day": label, "version": t["version"], "hold_reason": ""} for t in timeline] + anytime
    stats = [
        {"label": "Items", "value": len(rows), "tone": "accent"},
        {"label": "Meetings", "value": meetings, "tone": "info"},
        {"label": "Late", "value": len([i for i in dash["overdue"] if include_private or i.get("scope") != "private"]), "tone": "danger"},
        {"label": "Free", "value": f"{free_minutes // 60}h {free_minutes % 60:02d}m" if free_minutes else "0h", "tone": "success"},
    ]
    return {
        "kind": "today", "layout": "timeline", "title": title,
        "subtitle": ("A full day" if load["heavy"] else "Office " + tu.fmt_time(o_start) + " – " + tu.fmt_time(o_end)),
        "columns": [{"key": "when", "label": "When"}, {"key": "title", "label": "What"}, {"key": "kind", "label": "Type"}],
        "rows": rows, "stats": stats, "timeline": timeline, "series": [],
        "office": {"start_min": _minutes(o_start), "end_min": _minutes(o_end)},
        "file_name": f"jarvis-day-{day.isoformat()}",
        "text": _text(title, rows, ["when", "title", "kind"]),
    }


def _week_view(user, dash: dict, include_private: bool, prefs: dict) -> dict:
    today = tu.today_local()
    series, rows = [], []
    for i in range(7):
        d = today + timedelta(days=i)
        load = scheduling.day_load(user, d, prefs)
        timed = scheduling.commitments(user, d)
        dated = scheduling.dated_items(user, d)
        count = len(timed) + len(dated)
        visible = [c for c in timed + dated if include_private or not c.get("private")]
        series.append({"label": d.strftime("%a"), "date": d.isoformat(), "value": count, "minutes": load["minutes"],
                       "heavy": load["heavy"], "off": scheduling.is_off_day(prefs, d), "today": i == 0})
        for c in visible:
            when = f"{tu.fmt_time(c['start'])}" if c.get("start") else "any time"
            rows.append({"id": c["id"], "scope": c.get("scope", "shared"), "title": c["title"], "when": when, "day": tu.fmt_day(d),
                         "kind": _KIND_WORD.get(c.get("kind"), "Task"), "priority": c.get("priority", "NORMAL"),
                         "private": bool(c.get("private")), "status": "", "who": "", "progress": 0, "overdue": False,
                         "version": c.get("version"), "hold_reason": ""})
    heavy = [s["label"] for s in series if s["heavy"]]
    title = "This week"
    return {
        "kind": "week", "layout": "bars", "title": title,
        "subtitle": ("Heavy: " + ", ".join(heavy)) if heavy else "Nothing overloaded",
        "columns": [{"key": "day", "label": "Day"}, {"key": "when", "label": "When"}, {"key": "title", "label": "What"}],
        "rows": rows, "stats": [{"label": "Items", "value": sum(s["value"] for s in series), "tone": "accent"},
                                {"label": "Heavy days", "value": len(heavy), "tone": "danger" if heavy else "success"}],
        "timeline": [], "series": series, "file_name": f"jarvis-week-{today.isoformat()}",
        "text": _text(title, rows, ["day", "when", "title"]),
    }


def _stats_view(user, dash: dict, include_private: bool) -> dict:
    keep = lambda items: [i for i in items if include_private or i.get("scope") != "private"]  # noqa: E731
    pending = [r for r in dash["requests"] if r["state"] == "PENDING"]
    stats = [
        {"label": "Today", "value": len(keep(dash["today"])), "tone": "accent"},
        {"label": "Late", "value": len(keep(dash["overdue"])), "tone": "danger"},
        {"label": "Done today", "value": dash["completed_today"], "tone": "success"},
        {"label": "Need your OK" if user.is_md else "Waiting for Sir", "value": len(pending), "tone": "warn"},
    ]
    if user.is_md:
        open_pa = [t for t in dash.get("pa_tasks", []) if t["status"] not in (Status.COMPLETED, Status.CANCELLED)]
        stats.append({"label": "With Mansi", "value": len(open_pa), "tone": "info"})
    staff_open = [t for t in dash["delegated"] if t["status"] not in (Status.COMPLETED, Status.CANCELLED)]
    stats.append({"label": "With staff", "value": len(staff_open), "tone": "info"})
    rows = _rows(keep(dash["overdue"]) + keep(dash["today"]), include_private)
    view = _table("stats", "Where things stand", rows, [("when", "When"), ("title", "What"), ("status", "Status")],
                  subtitle=dash["date_label"], layout="stats")
    view["stats"] = stats
    return view


def _progress_table(kind: str, title: str, items, include_private: bool, empty: str) -> dict:
    rows = _rows(items, include_private)
    view = _table(kind, title, rows, [("title", "What"), ("who", "Who"), ("status", "Status"), ("when", "Due")],
                  subtitle=(f"{len(rows)} items" if rows else empty), layout="progress")
    return view


def _requests_view(user, dash: dict) -> dict:
    reqs = [r for r in dash["requests"] if r["state"] == "PENDING"] if user.is_md else dash["requests"][:20]
    rows = [{"id": r["id"], "scope": "request", "title": r["title"], "who": r["requested_by"], "when": r["due_label"],
             "status": r["state"].title(), "priority": r["priority"], "kind": "Change" if r["kind"] == "CHANGE" else "New task",
             "reason": r["reason"], "target": r.get("target_title") or "", "private": False, "progress": 0, "overdue": False,
             "day": "", "version": None, "hold_reason": ""} for r in reqs]
    title = "Need your OK" if user.is_md else "Waiting for Sir"
    return _table("requests", title, rows, [("title", "What"), ("who", "From"), ("when", "Due"), ("status", "Status")],
                  subtitle=(f"{len(rows)} waiting" if rows else "Nothing waiting"), layout="requests")


def _busy_view(dash: dict) -> dict:
    windows = dash.get("md_busy_today") or []
    rows = [{"id": w["start"], "scope": "", "title": "Busy", "when": w["label"].replace("Busy ", ""), "status": "", "who": "", "kind": "",
             "priority": "NORMAL", "private": False, "progress": 0, "overdue": False, "day": "today", "version": None, "hold_reason": ""}
            for w in windows]
    view = _table("busy", "When Sir is busy today", rows, [("when", "When")],
                  subtitle=("Sir's own plans stay private; only the time is shown." if rows else "No busy times today."), layout="busy")
    view["timeline"] = [{"id": "", "scope": "", "title": "Busy", "kind": "Busy", "priority": "NORMAL",
                         "start": tu.fmt_time(datetime.fromisoformat(w["start"])), "end": tu.fmt_time(datetime.fromisoformat(w["end"])),
                         "start_min": _minutes(datetime.fromisoformat(w["start"])), "end_min": _minutes(datetime.fromisoformat(w["end"])),
                         "private": False, "routine": False, "overdue": False, "version": None} for w in windows]
    return view


def _due_of(task) -> datetime | None:
    if task.due_at:
        return task.due_at
    if task.due_date:
        return tu.combine_local(task.due_date, time(23, 59))
    return None


def _performance_view(user) -> dict:
    """Who did what in the last 30 days: staff (and the PA, for Sir) with done, late and on-time figures."""
    now = timezone.now()
    since = now - timedelta(days=30)
    people: dict[str, dict] = {}

    def bump(name: str, dept: str, task):
        p = people.setdefault(name, {"name": name, "dept": dept, "assigned": 0, "done": 0, "late": 0, "on_time": 0, "open": 0})
        p["assigned"] += 1
        due = _due_of(task)
        if task.status == Status.COMPLETED:
            p["done"] += 1
            if due is None or (task.completed_at and task.completed_at <= due):
                p["on_time"] += 1
            else:
                p["late"] += 1
        else:
            p["open"] += 1
            if due and due < now:
                p["late"] += 1

    for t in services.delegated_qs().filter(created_at__gte=since).exclude(status=Status.CANCELLED):
        bump(t.staff.name if t.staff else "Staff", t.staff.department if t.staff else "", t)
    if user.is_md:
        pa = services.get_pa()
        for t in services.pa_tasks_qs().exclude(origin=SharedTask.Origin.MD_MEET_PA).filter(created_at__gte=since).exclude(status=Status.CANCELLED):
            bump(pa.spoken_name if pa else "PA", "PA", t)

    ordered = sorted(people.values(), key=lambda p: (-p["assigned"], p["name"]))
    rows = []
    for p in ordered:
        pct = round(p["on_time"] / p["done"] * 100) if p["done"] else 0
        rows.append({
            "id": p["name"], "scope": "", "title": p["name"], "who": p["dept"], "kind": "",
            "status": f"{p['done']}/{p['assigned']} done", "when": f"{p['late']} late" if p["late"] else "on time",
            "day": "", "priority": "URGENT" if p["late"] >= 2 else "HIGH" if p["late"] else "NORMAL",
            "progress": pct if p["done"] else 0, "private": False, "overdue": p["late"] > 0, "version": None,
            "hold_reason": "", "assigned": p["assigned"], "done": p["done"], "late": p["late"], "on_time_pct": pct,
        })
    assigned = sum(p["assigned"] for p in ordered)
    done = sum(p["done"] for p in ordered)
    late = sum(p["late"] for p in ordered)
    on_time = sum(p["on_time"] for p in ordered)
    title = "Staff performance"
    return {
        "kind": "performance", "layout": "progress", "title": title, "subtitle": "Last 30 days · on-time share of finished work",
        "columns": [{"key": "title", "label": "Who"}, {"key": "who", "label": "Team"}, {"key": "status", "label": "Done"},
                    {"key": "when", "label": "Late"}, {"key": "on_time_pct", "label": "On time %"}],
        "rows": rows,
        "stats": [{"label": "Assigned", "value": assigned, "tone": "accent", "icon": "briefcase-outline"},
                  {"label": "Done", "value": done, "tone": "success", "icon": "checkmark-done-outline"},
                  {"label": "Late", "value": late, "tone": "danger" if late else "muted", "icon": "alert-circle-outline"},
                  {"label": "On time", "value": f"{round(on_time / done * 100) if done else 0}%", "tone": "info", "icon": "time-outline"}],
        "timeline": [],
        "series": [{"label": p["name"].split()[0], "date": p["name"], "value": p["done"], "minutes": p["assigned"],
                    "heavy": p["late"] > 0, "off": False, "today": False} for p in ordered[:8]],
        "file_name": f"jarvis-performance-{tu.today_local().isoformat()}",
        "text": _text(title, rows, ["title", "status", "when"]),
    }


def _done_view(user, include_private: bool) -> dict:
    """Everything finished in the last 14 days, newest first."""
    now = timezone.now()
    since = now - timedelta(days=14)
    items = []
    if user.is_md and include_private:
        for t in PrivateTask.objects.filter(owner=user, status=Status.COMPLETED, completed_at__gte=since):
            items.append((t.completed_at, t.title, "You", "private", str(t.id), t.version))
    shared = SharedTask.objects.filter(status=Status.COMPLETED, completed_at__gte=since).select_related("staff")
    if user.is_pa:
        shared = shared.exclude(assignee_type=SharedTask.Assignee.MD) | SharedTask.objects.filter(
            status=Status.COMPLETED, completed_at__gte=since, origin=SharedTask.Origin.PA_REQUEST).select_related("staff")
    pa = services.get_pa()
    for t in shared.distinct():
        who = t.staff.name if t.staff else (pa.spoken_name if pa and t.assignee_type == SharedTask.Assignee.PA else "Sir" if user.is_pa else "You")
        items.append((t.completed_at, t.title, who, "shared", str(t.id), t.version))
    items.sort(key=lambda x: x[0], reverse=True)
    today = tu.today_local()
    week_ago = today - timedelta(days=7)
    rows = [{"id": i[4], "scope": i[3], "title": i[1], "who": i[2], "kind": "", "status": "Done",
             "when": tu.fmt_time(i[0]), "day": tu.fmt_day(tu.to_local(i[0]).date()), "priority": "NORMAL",
             "progress": 100, "private": i[3] == "private", "overdue": False, "version": i[5], "hold_reason": ""} for i in items[:60]]
    done_today = sum(1 for i in items if tu.to_local(i[0]).date() == today)
    done_week = sum(1 for i in items if tu.to_local(i[0]).date() >= week_ago)
    title = "Completed"
    view = _table("done", title, rows, [("day", "Day"), ("when", "At"), ("title", "What"), ("who", "Who")],
                  subtitle="Last 14 days" if rows else "Nothing finished in the last 14 days")
    view["stats"] = [{"label": "Today", "value": done_today, "tone": "success", "icon": "checkmark-done-outline"},
                     {"label": "This week", "value": done_week, "tone": "accent", "icon": "calendar-outline"},
                     {"label": "14 days", "value": len(items), "tone": "info", "icon": "stats-chart-outline"}]
    return view


def build(user, kind: str, include_private: bool = True, day: date | None = None) -> dict | None:
    """A data view for the phone, or None when that kind does not exist for this person."""
    if kind not in VIEW_KINDS:
        return None
    if kind == "private" and not user.is_md:
        return None
    if kind == "busy" and user.is_md:
        kind = "today"
    dash = services.dashboard(user)
    prefs = services.get_prefs(user)
    if kind == "today":
        return _day_view(user, dash, include_private, prefs, day or tu.today_local())
    if kind == "week":
        return _week_view(user, dash, include_private, prefs)
    if kind == "stats":
        return _stats_view(user, dash, include_private)
    if kind == "late":
        return _table("late", "Late", _rows(dash["overdue"], include_private), [("when", "Was due"), ("title", "What"), ("kind", "Type")],
                      subtitle="Nothing is late." if not dash["overdue"] else f"{len(dash['overdue'])} items")
    if kind == "upcoming":
        rows = _rows(dash["upcoming"], include_private)
        return _table("upcoming", "Coming up", rows, [("day", "Day"), ("when", "When"), ("title", "What")],
                      subtitle="Next 7 days")
    if kind == "nodate":
        rows = _rows(dash["anytime"], include_private)
        return _table("nodate", "No date yet", rows, [("title", "What"), ("kind", "Type"), ("priority", "Priority")],
                      subtitle=f"{len(rows)} items" if rows else "Everything has a date.")
    if kind == "private":
        items = [i for i in dash["overdue"] + dash["today"] + dash["upcoming"] + dash["anytime"] if i.get("scope") == "private"]
        return _table("private", "Private reminders", _rows(items, include_private), [("day", "Day"), ("when", "When"), ("title", "What")],
                      subtitle="Only you can see these")
    if kind == "pa":
        items = dash.get("pa_tasks", []) if user.is_md else dash["overdue"] + dash["today"] + dash["upcoming"] + dash["anytime"]
        pa = services.get_pa()
        name = pa.spoken_name if pa else "your PA"
        return _progress_table("pa", f"Given to {name}" if user.is_md else "My tasks from Sir", items, include_private,
                               "Nothing given yet" if user.is_md else "Nothing from Sir yet")
    if kind == "staff":
        return _progress_table("staff", "Given to staff", dash["delegated"], include_private, "Nothing with staff")
    if kind == "requests":
        return _requests_view(user, dash)
    if kind == "busy":
        return _busy_view(dash)
    if kind == "performance":
        return _performance_view(user)
    if kind == "done":
        return _done_view(user, include_private)
    return None


_QUERY_HINTS = [
    ("performance", ("performance", "report", "kaisa kaam", "kis ne kitna", "productivity", "kaun late", "who is late", "on time")),
    ("done", ("completed", "finished", "ho gaya", "ho gaye", "khatam", "kya kya hua", "what got done")),
    ("requests", ("request", "approve", "approval", "ok karna", "waiting for", "pending request", "manzoori")),
    ("busy", ("busy", "free kab", "khaali", "khali", "free time")),
    ("staff", ("staff", "team", "ramesh", "delegat", "taskpanel", "kaun kar raha")),
    ("pa", ("mansi", "pa ke", "pa ko", "assistant")),
    ("private", ("private", "personal", "mere personal")),
    ("late", ("late", "overdue", "pending", "baaki", "baki", "reh gay", "chhoot", "slipped")),
    ("week", ("week", "hafte", "hafta", "saptah", "7 din", "saat din", "is mahine")),
    ("upcoming", ("upcoming", "coming up", "aage", "next", "agle", "tomorrow", "kal", "parso")),
    ("nodate", ("no date", "bina date", "undated", "without date")),
    ("stats", ("kitne", "kitna", "how many", "status", "overview", "summary", "sab kuch", "load")),
    ("today", ("today", "aaj", "din", "schedule", "plan", "agenda", "meeting", "calendar")),
]


def guess_kind(text: str) -> str:
    """When the model did not say which data fits the question, pick from the words."""
    low = f" {(text or '').lower()} "
    for kind, words in _QUERY_HINTS:
        if any(w in low for w in words):
            return kind
    return "stats"
