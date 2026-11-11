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

VIEW_KINDS = ["today", "week", "pending", "official", "late", "upcoming", "nodate", "stats", "private", "pa", "staff",
              "requests", "busy", "performance", "done", "cancelled", "history", "people", "tasks", "person"]

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


def _summ(view: dict, address: str, address_hi: str, en: str, hi: str) -> dict:
    """The sentence JARVIS speaks for this card, written here from the real numbers (never by the model)."""
    view["summary"] = f"{address}, {en}"
    view["summary_hi"] = f"{address_hi}, {hi}"
    return view


def _addr(user) -> tuple[str, str]:
    return services.address_of(user), services.address_hi_of(user)


def _late_tail(n: int) -> tuple[str, str]:
    if not n:
        return "", ""
    return (f" {n} late from before." if n > 1 else " 1 late from before.", f" पहले के {n} काम लेट हैं।" if n > 1 else " पहले का 1 काम लेट है।")


def _day_view(user, dash: dict, include_private: bool, prefs: dict, day: date) -> dict:
    """The day's plan: what is due THAT day, in time order, with the free time. Late items from earlier days are
    not mixed in (he asked about the day); they are counted in the Late tile and said in one short tail."""
    visible = [i for i in dash["today"] if include_private or i.get("scope") != "private"]
    late_count = len([i for i in dash["overdue"] if include_private or i.get("scope") != "private"])
    timeline, anytime = [], []
    hi_times: dict[str, str] = {}  # item id -> "4 बजे", for the Hindi sentence
    o_start, o_end = scheduling.office_hours(prefs, day)
    for item in visible:
        start = item.get("scheduled_start") or item.get("due_at")
        if start:
            s = datetime.fromisoformat(start)
            if tu.to_local(s).date() != day:
                continue
            hi_times[item["id"]] = tu.fmt_time_hi(s)
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
    is_today = day == tu.today_local()
    # Free time still ahead today (not the morning that has passed), or the whole office day for another day.
    free = scheduling.free_slots(user, day, 30, prefs, not_before=tu.now_local() if is_today else None)
    free_minutes = sum(int((e - s).total_seconds() // 60) for s, e in free)
    load = scheduling.day_load(user, day, prefs)
    label = tu.fmt_day(day)
    title = f"{label[:1].upper()}{label[1:]}, {day.strftime('%a %d %b')}" if not is_today else f"Today, {day.strftime('%a %d %b')}"
    rows = [{"id": t["id"], "scope": t["scope"], "title": t["title"], "when": f"{t['start']}–{t['end']}", "kind": t["kind"],
             "priority": t["priority"], "private": t["private"], "status": "", "who": "", "progress": 0, "overdue": t["overdue"],
             "day": label, "version": t["version"], "hold_reason": ""} for t in timeline if not t["routine"]] + anytime
    stats = [
        {"label": "Items", "value": len(rows), "tone": "accent"},
        {"label": "Meetings", "value": meetings, "tone": "info"},
        {"label": "Late", "value": late_count, "tone": "danger" if late_count else "muted"},
        {"label": "Free", "value": f"{free_minutes // 60}h {free_minutes % 60:02d}m" if free_minutes else "0h", "tone": "success"},
    ]
    view = {
        "kind": "today", "layout": "timeline", "title": title,
        "subtitle": ("A full day" if load["heavy"] else "Office " + tu.fmt_time(o_start) + " – " + tu.fmt_time(o_end)),
        "columns": [{"key": "when", "label": "When"}, {"key": "title", "label": "What"}, {"key": "kind", "label": "Type"}],
        "rows": rows, "stats": stats, "timeline": timeline, "series": [],
        "office": {"start_min": _minutes(o_start), "end_min": _minutes(o_end)},
        "file_name": f"jarvis-day-{day.isoformat()}",
        "text": _text(title, rows, ["when", "title", "kind"]),
    }

    # The spoken answer: the day's items by name (up to three), the free time, then the late tail.
    address, address_hi = _addr(user)
    day_en, day_hi = tu.fmt_day(day), tu.fmt_day_hi(day)
    timed = [t for t in timeline if not t["routine"]]
    named_en = [f"{t['title']} at {t['start']}" for t in timed] + [r["title"] for r in anytime]
    named_hi = [f"{t['title']} {hi_times.get(t['id'], '')}".strip() for t in timed] + [r["title"] for r in anytime]
    n = len(rows)
    off = scheduling.is_off_day(prefs, day)
    if free and free_minutes >= 60:
        big = max(free, key=lambda se: se[1] - se[0])
        free_en = f" You're free {tu.fmt_time(big[0])} to {tu.fmt_time(big[1])}."
        free_hi = f" {tu.fmt_time_hi(big[0])} से {tu.fmt_time_hi(big[1])} तक आप खाली हैं।"
    elif free_minutes:
        free_en = f" {free_minutes} minutes free."
        free_hi = f" {free_minutes} मिनट खाली हैं।"
    else:
        free_en, free_hi = ("", "") if off or not n else (" No free time left.", " अब खाली समय नहीं है।")
    tail_en, tail_hi = _late_tail(late_count)
    if not n:
        en = f"nothing is scheduled {day_en}" + ("; it's your day off." if off else "; you're free all day." if is_today or not off else ".")
        hi = f"{day_hi} कुछ भी तय नहीं है" + ("; आज छुट्टी है।" if off else "; आप पूरे दिन खाली हैं।")
    else:
        more = n - 3
        en = (f"{n} {'item' if n == 1 else 'items'} {day_en}: " + ", ".join(named_en[:3])
              + (f" and {more} more" if more > 0 else "") + "." + free_en)
        hi = (f"{day_hi} {n} काम {'है' if n == 1 else 'हैं'}: " + ", ".join(named_hi[:3])
              + (f" और {more} और" if more > 0 else "") + "।" + free_hi)
    return _summ(view, address, address_hi, en + tail_en, hi + tail_hi)


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
        {"label": "Need your OK" if user.is_md else f"Waiting for {services.md_address()}", "value": len(pending), "tone": "warn"},
    ]
    if user.is_md:
        open_pa = [t for t in dash.get("pa_tasks", []) if t["status"] not in (Status.COMPLETED, Status.CANCELLED)]
        stats.append({"label": f"With {dash.get('pa_name') or 'your PA'}", "value": len(open_pa), "tone": "info"})
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
    title = "Need your OK" if user.is_md else f"Waiting for {services.md_address()}"
    return _table("requests", title, rows, [("title", "What"), ("who", "From"), ("when", "Due"), ("status", "Status")],
                  subtitle=(f"{len(rows)} waiting" if rows else "Nothing waiting"), layout="requests")


def _busy_view(dash: dict) -> dict:
    windows = dash.get("md_busy_today") or []
    rows = [{"id": w["start"], "scope": "", "title": "Busy", "when": w["label"].replace("Busy ", ""), "status": "", "who": "", "kind": "",
             "priority": "NORMAL", "private": False, "progress": 0, "overdue": False, "day": "today", "version": None, "hold_reason": ""}
            for w in windows]
    sir = services.md_address()
    view = _table("busy", f"When {sir} is busy today", rows, [("when", "When")],
                  subtitle=(f"{sir}'s own plans stay private; only the time is shown." if rows else "No busy times today."), layout="busy")
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
        who = t.staff.name if t.staff else (pa.spoken_name if pa and t.assignee_type == SharedTask.Assignee.PA else services.md_address() if user.is_pa else "You")
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


def _closed_items(user, include_private: bool, statuses: tuple, since: datetime | None):
    """(when, item dict) for everything this person may see with one of these statuses, newest first."""
    items = []
    if user.is_md and include_private:
        qs = PrivateTask.objects.filter(owner=user, status__in=statuses)
        if since:
            qs = qs.filter(updated_at__gte=since)
        items += [(t.completed_at or t.updated_at, services.private_item(t)) for t in qs]
    shared = SharedTask.objects.filter(status__in=statuses).select_related("staff")
    if since:
        shared = shared.filter(updated_at__gte=since)
    if user.is_pa:
        shared = shared.exclude(assignee_type=SharedTask.Assignee.MD) | shared.filter(origin=SharedTask.Origin.PA_REQUEST)
    items += [(t.completed_at or t.updated_at, services.shared_item(t)) for t in shared.distinct()]
    items.sort(key=lambda x: x[0] or timezone.now(), reverse=True)
    return items


def _cancelled_view(user, include_private: bool) -> dict:
    """Everything cancelled in the last 30 days, newest first: a cancel is undone from here ("restore")."""
    since = timezone.now() - timedelta(days=30)
    items = _closed_items(user, include_private, (Status.CANCELLED,), since)
    rows = []
    for when, item in items[:60]:
        row = _row(item, include_private)
        if not row:
            continue
        row.update({"status": "Cancelled", "when": tu.fmt_time(when) if when else "", "day": tu.fmt_day(tu.to_local(when).date()) if when else "",
                    "progress": 0, "overdue": False, "restorable": True})
        rows.append(row)
    view = _table("cancelled", "Cancelled", rows, [("day", "Day"), ("title", "What"), ("who", "Who")],
                  subtitle="Last 30 days. Tap one to restore it." if rows else "Nothing cancelled in the last 30 days")
    view["stats"] = [{"label": "Cancelled", "value": len(rows), "tone": "muted", "icon": "close-circle-outline"}]
    return view


def _history_view(user, include_private: bool, p: dict) -> dict:
    """The past of one thing or person: "when did I last meet Sharma", "what did I give Suraj last month". Every
    status counts (done, cancelled, still open); newest first; the outcome is said with each item."""
    words = [w.lower() for w in (p.get("words") or "").split() if len(w) > 2]
    who = (p.get("who") or "").strip().lower()
    start, end = p.get("from"), p.get("to")
    items = _closed_items(user, include_private, (Status.COMPLETED, Status.CANCELLED, Status.OPEN, Status.IN_PROGRESS, Status.BLOCKED), None)

    def moment(when, item):
        if item.get("status") == Status.COMPLETED and item.get("completed_at"):
            return datetime.fromisoformat(item["completed_at"])
        if item.get("due_at"):
            return datetime.fromisoformat(item["due_at"])
        if item.get("due_date"):
            return tu.combine_local(date.fromisoformat(item["due_date"]), time(23, 59))
        return when or timezone.now()

    def matches(item):
        title = (item.get("title") or "").lower()
        hay = title + " " + (item.get("assignee", {}).get("staff_name") or "").lower()
        if words and not any(w in hay for w in words):
            return False
        if who and who not in ("me", "all", "sir", "pa", "staff"):
            if who not in hay:
                return False
        if who == "pa" and item.get("assignee", {}).get("type") != "PA":
            return False
        if who == "staff" and item.get("assignee", {}).get("type") != "STAFF":
            return False
        if start:
            d = tu.to_local(moment(None, item)).date()
            if not (start <= d <= (end or start)):
                return False
        return True

    found = [(moment(when, item), item) for when, item in items if matches(item)]
    found.sort(key=lambda x: x[0], reverse=True)
    today = tu.today_local()
    rows = []
    for at, item in found[:60]:
        row = _row(item, include_private)
        if not row:
            continue
        status = item.get("status")
        outcome = "Done" if status == Status.COMPLETED else "Cancelled" if status == Status.CANCELLED else "Late" if item.get("overdue") else "Open"
        row.update({"status": outcome, "when": tu.fmt_time(at), "day": tu.fmt_day(tu.to_local(at).date()),
                    "past": tu.to_local(at).date() <= today})
        rows.append(row)
    what = " ".join(p.get("words", "").split()) or (p.get("who") if who not in ("me", "all") else "") or "this"
    view = _table("history", f"History: {what}", rows, [("day", "Day"), ("when", "At"), ("title", "What"), ("status", "Outcome"), ("who", "Who")],
                  subtitle="Newest first, every outcome" if rows else f"Nothing about {what} so far")
    view["stats"] = [{"label": "Done", "value": sum(1 for r in rows if r["status"] == "Done"), "tone": "success", "icon": "checkmark-done-outline"},
                     {"label": "Open", "value": sum(1 for r in rows if r["status"] in ("Open", "Late")), "tone": "accent", "icon": "time-outline"},
                     {"label": "Cancelled", "value": sum(1 for r in rows if r["status"] == "Cancelled"), "tone": "muted", "icon": "close-circle-outline"}]
    return view


def _pending_view(user, dash: dict, include_private: bool, official_only: bool = False) -> dict:
    """Everything still open, late first. Sir's rows say Official or Personal (personal rows only when the phone
    is unlocked, never in the official view); Mansi's say whether Sir gave it or it is her own. She has no
    personal category, and her dashboard never holds his private items."""
    split = services.pending_split(user, dash, include_private and not official_only)
    counts = split["counts"]
    sir = services.md_address()
    rows = []
    for item in split["items"][:60]:
        row = _row(item, True)
        if user.is_md:
            row["who"] = "Personal" if row["private"] else "Official"
        else:
            row["who"] = "Your own" if item.get("origin") == SharedTask.Origin.PA_SELF else f"From {sir}"
        row["day"] = row["day"] or "No date"
        rows.append(row)
    if user.is_md:
        stats = [{"label": "Official", "value": counts["official"], "tone": "accent", "icon": "briefcase-outline"}]
        if "personal" in counts:
            stats.append({"label": "Personal", "value": counts["personal"], "tone": "info", "icon": "lock-closed-outline"})
    else:
        stats = [{"label": f"From {sir}", "value": counts["from_sir"], "tone": "accent", "icon": "briefcase-outline"},
                 {"label": "Your own", "value": counts["own"], "tone": "info", "icon": "person-outline"}]
    stats.append({"label": "Late", "value": counts["late"], "tone": "danger" if counts["late"] else "muted",
                  "icon": "alert-circle-outline"})
    stats.append({"label": "Due today", "value": counts["due_today"], "tone": "warn" if counts["due_today"] else "muted",
                  "icon": "today-outline"})
    kind = "official" if official_only else "pending"
    title = "Official tasks" if official_only else "Pending"
    view = _table(kind, title, rows, [("day", "Day"), ("when", "When"), ("title", "What"),
                                      ("who", "Official / Personal" if user.is_md else "From")],
                  subtitle="Nothing is pending." if not rows else f"{counts['total']} still open, late first")
    view["stats"] = stats
    return view


# A standard view asked for a particular person or period becomes the generic "tasks" template with these
# settings (the same table and chart for everybody: only the data changes).
_AS_TASKS = {
    "pa": {"who": "pa"}, "staff": {"who": "staff"}, "private": {"who": "me", "filter": "personal"},
    "official": {"who": "me", "filter": "official"}, "done": {"filter": "done"}, "late": {"filter": "late"},
    "pending": {"filter": "open"}, "upcoming": {"filter": "open"}, "today": {}, "week": {}, "tasks": {},
}


def build(user, kind: str, include_private: bool = True, day: date | None = None, params: dict | None = None,
          missing: str = "") -> dict | None:
    """A data view for the phone, or None when that kind does not exist for this person.

    params (who / from / to / by / filter / group, plus the narrowing filters) come from the request form the AI
    fills in; see listviews. The result may be {"ask": ...} when the person named fits more than one staff member.
    Every view carries ``summary`` / ``summary_hi`` (the sentence JARVIS speaks, written from the real numbers)
    and ``request`` (the form as answered, so a follow-up can narrow or widen the same list).
    """
    if kind not in VIEW_KINDS:
        return None
    from . import listviews  # here, not at the top: listviews builds on this module

    p = listviews.clean_params(params)
    view = _build(user, kind, include_private, day, p, listviews.clean_missing(missing), listviews)
    if view and not view.get("ask"):
        # The request as answered, with the period the standard day / week views imply, so a follow-up
        # ("sirf meetings") can ask for the same days with one more filter.
        answered = dict(p)
        today = tu.today_local()
        if view.get("kind") == "today" and not answered["from"]:
            answered["from"] = answered["to"] = day or today
        elif view.get("kind") == "week" and not answered["from"]:
            answered["from"], answered["to"] = today, today + timedelta(days=6)
        view.setdefault("request", listviews.request_of(view.get("kind") or kind, answered))
        if not view.get("summary"):
            address, address_hi = _addr(user)
            n = len(view.get("rows") or [])
            _summ(view, address, address_hi, f"here is {view.get('title', 'the list').lower()}: {n} {'item' if n == 1 else 'items'}.",
                  f"ये रही {view.get('title', 'लिस्ट')}: {n} काम।")
    return view


def _first_two(rows: list[dict], key: str = "when") -> tuple[str, str]:
    """'A (10 AM), B (tomorrow)' in English and Hindi for the first two rows of a table."""
    def one(r, hindi):
        when = r.get(key) or ""
        return f"{r['title']} ({when})" if when and not hindi else r["title"]
    return ", ".join(one(r, False) for r in rows[:2]), ", ".join(one(r, True) for r in rows[:2])


def _build(user, kind: str, include_private: bool, day: date | None, p: dict, missing: str, listviews) -> dict | None:
    one_named = bool(p["who"]) and p["who"].lower() not in listviews._STAFF | listviews._ALL | listviews._ME
    if kind == "person" or (kind == "people" and one_named):
        # "Details of Kavita Singh" (even when the model filed it under people): that one person's own card.
        if not one_named:
            return listviews.people_view(user, p, missing=missing)
        if listviews.resolve_subject(user, p["who"])["type"] == "department":
            p["group"] = p["group"] or p["who"]
            return listviews.people_view(user, p, missing=missing)
        return listviews.tasks_view(user, p, include_private, mode="person", missing=missing)
    if kind == "people":
        return listviews.people_view(user, p, missing=missing)
    if kind == "history":
        view = _history_view(user, include_private, p)
        address, address_hi = _addr(user)
        rows = view["rows"]
        what = view["title"].removeprefix("History: ")
        if not rows:
            return _summ(view, address, address_hi, f"I have nothing about {what}.", f"{what} के बारे में मेरे पास कुछ नहीं है।")
        last = next((r for r in rows if r["past"] and r["status"] != "Cancelled"), rows[0])
        n = len(rows)
        en = f"the last one about {what} was {last['title']} {last['day']} ({last['status'].lower()})" + (f"; {n} in all." if n > 1 else ".")
        hi = f"{what} के बारे में पिछला {last['title']} {last['day']} ({last['status']})" + (f"; कुल {n}।" if n > 1 else "।")
        return _summ(view, address, address_hi, en, hi)
    if kind == "performance" and p["who"] and p["who"].lower() not in listviews._STAFF | listviews._ALL:
        return listviews.tasks_view(user, p, include_private, mode="performance", missing=missing)
    if kind == "tasks" or (kind in _AS_TASKS and listviews.has_request(p)):
        preset = _AS_TASKS[kind]
        today = tu.today_local()
        if kind == "week" and not p["from"]:
            p["from"], p["to"] = today, today + timedelta(days=6)
        if kind == "today" and not p["from"]:
            p["from"] = p["to"] = today
        if kind == "private" and not user.is_md:
            return None
        p["who"] = p["who"] or preset.get("who", "")
        p["filter"] = p["filter"] or preset.get("filter", "")
        return listviews.tasks_view(user, p, include_private, missing=missing)
    if kind == "private" and not user.is_md:
        return None
    if kind == "busy" and user.is_md:
        kind = "today"
    dash = services.dashboard(user)
    prefs = services.get_prefs(user)
    address, address_hi = _addr(user)
    sir, sir_hi = services.md_address(), services.md_address_hi()
    if kind == "today":
        return _day_view(user, dash, include_private, prefs, day or tu.today_local())
    if kind == "week":
        view = _week_view(user, dash, include_private, prefs)
        n = sum(s["value"] for s in view["series"])
        heavy = [s["label"] for s in view["series"] if s["heavy"]]
        en = f"{n} {'item' if n == 1 else 'items'} this week" + (f"; {', '.join(heavy)} {'is' if len(heavy) == 1 else 'are'} full." if heavy else "; no day is overloaded.")
        hi = f"इस हफ़्ते {n} काम हैं" + (f"; {', '.join(heavy)} भरा हुआ है।" if len(heavy) == 1 else f"; {', '.join(heavy)} भरे हुए हैं।" if heavy else "; कोई दिन ओवरलोड नहीं है।")
        return _summ(view, address, address_hi, en, hi)
    if kind == "stats":
        view = _stats_view(user, dash, include_private)
        by = {s["label"]: s["value"] for s in view["stats"]}
        waiting = by.get("Need your OK", by.get(f"Waiting for {sir}", 0))
        en = f"{by['Today']} today, {by['Late']} late, {by['Done today']} done today" + (f", {waiting} {'waiting for your OK' if user.is_md else f'waiting for {sir}'}." if waiting else ".")
        hi = f"आज {by['Today']} काम, {by['Late']} लेट, {by['Done today']} आज पूरे हुए" + (f", {waiting} {'आपकी मंज़ूरी के इंतज़ार में' if user.is_md else f'{sir_hi} के इंतज़ार में'}।" if waiting else "।")
        return _summ(view, address, address_hi, en, hi)
    if kind in ("pending", "official"):
        view = _pending_view(user, dash, include_private, official_only=kind == "official")
        counts = services.pending_split(user, dash, include_private and kind != "official")["counts"]
        total = counts["total"]
        if not total:
            return _summ(view, address, address_hi, "nothing is pending." if kind == "pending" else "no official work is pending.",
                         "कुछ भी बाकी नहीं है।" if kind == "pending" else "कोई ऑफ़िशियल काम बाकी नहीं है।")
        if user.is_md:
            if kind == "official":
                split_en, split_hi = f"{counts['official']} official pending", f"{counts['official']} ऑफ़िशियल काम बाकी हैं"
            elif "personal" in counts:
                split_en = f"{total} pending: {counts['official']} official and {counts['personal']} personal"
                split_hi = f"{total} काम बाकी हैं: {counts['official']} ऑफ़िशियल और {counts['personal']} पर्सनल"
            else:
                split_en, split_hi = f"{total} official pending", f"{total} ऑफ़िशियल काम बाकी हैं"
        else:
            split_en = f"{total} pending: {counts['from_sir']} from {sir} and {counts['own']} your own"
            split_hi = f"{total} काम बाकी हैं: {counts['from_sir']} {sir_hi} के दिए हुए और {counts['own']} आपके अपने"
        extra_en = "; ".join(x for x in (f"{counts['late']} late" if counts["late"] else "", f"{counts['due_today']} due today" if counts["due_today"] else "") if x)
        extra_hi = "; ".join(x for x in (f"{counts['late']} लेट" if counts["late"] else "", f"{counts['due_today']} आज के" if counts["due_today"] else "") if x)
        first = view["rows"][0]["title"] if view["rows"] else ""
        en = split_en + (f"; {extra_en}" if extra_en else "") + (f". First: {first}." if first else ".")
        hi = split_hi + (f"; {extra_hi}" if extra_hi else "") + (f"। सबसे पहले {first}।" if first else "।")
        return _summ(view, address, address_hi, en, hi)
    if kind == "late":
        rows = _rows(dash["overdue"], include_private)
        # How long each has been late: the oldest first, and said plainly ("3 days late").
        today = tu.today_local()
        for row, item in zip(rows, [i for i in dash["overdue"] if include_private or i.get("scope") != "private"]):
            due = item.get("due_at") or (f"{item['due_date']}T23:59:00+05:30" if item.get("due_date") else "")
            days = (today - tu.to_local(datetime.fromisoformat(due)).date()).days if due else 0
            row["late_days"] = days
            row["when"] = f"{row['day'] or row['when']} · {days} day{'s' if days != 1 else ''} late" if days > 0 else f"{row['when']} · today"
        rows.sort(key=lambda r: -r.get("late_days", 0))
        view = _table("late", "Late", rows, [("when", "Was due"), ("title", "What"), ("kind", "Type")],
                      subtitle="Nothing is late." if not rows else f"{len(rows)} items, oldest first")
        if not rows:
            return _summ(view, address, address_hi, "nothing is late.", "कुछ भी लेट नहीं है।")
        oldest = rows[0].get("late_days", 0)
        first_en, first_hi = _first_two(rows)
        if oldest > 1:
            first_en = f"{rows[0]['title']} ({oldest} days late)" + (f", {rows[1]['title']}" if len(rows) > 1 else "")
            first_hi = f"{rows[0]['title']} ({oldest} दिन लेट)" + (f", {rows[1]['title']}" if len(rows) > 1 else "")
        n = len(rows)
        return _summ(view, address, address_hi, f"{n} {'item is' if n == 1 else 'items are'} late: {first_en}" + (f" and {n - 2} more." if n > 2 else "."),
                     f"{n} काम लेट {'है' if n == 1 else 'हैं'}: {first_hi}" + (f" और {n - 2} और।" if n > 2 else "।"))
    if kind == "upcoming":
        rows = _rows(dash["upcoming"], include_private)
        view = _table("upcoming", "Coming up", rows, [("day", "Day"), ("when", "When"), ("title", "What")], subtitle="Next 7 days")
        if not rows:
            return _summ(view, address, address_hi, "nothing is coming up in the next 7 days.", "अगले 7 दिन में कुछ नहीं है।")
        n = len(rows)
        r = rows[0]
        return _summ(view, address, address_hi, f"{n} coming up in the next 7 days; first {r['title']} {r['day']}" + (f" at {r['when']}." if r["when"] and r["when"] != r["day"] else "."),
                     f"अगले 7 दिन में {n} काम हैं; सबसे पहले {r['title']} {r['day']}।")
    if kind == "nodate":
        rows = _rows(dash["anytime"], include_private)
        view = _table("nodate", "No date yet", rows, [("title", "What"), ("kind", "Type"), ("priority", "Priority")],
                      subtitle=f"{len(rows)} items" if rows else "Everything has a date.")
        if not rows:
            return _summ(view, address, address_hi, "everything has a date.", "हर काम की तारीख़ तय है।")
        first_en, first_hi = _first_two(rows)
        return _summ(view, address, address_hi, f"{len(rows)} without a date: {first_en}" + (" and more." if len(rows) > 2 else "."),
                     f"{len(rows)} काम बिना तारीख़ के: {first_hi}" + (" और बाकी।" if len(rows) > 2 else "।"))
    if kind == "private":
        items = [i for i in dash["overdue"] + dash["today"] + dash["upcoming"] + dash["anytime"] if i.get("scope") == "private"]
        rows = _rows(items, include_private)
        view = _table("private", "Private reminders", rows, [("day", "Day"), ("when", "When"), ("title", "What")],
                      subtitle="Only you can see these")
        if not include_private:
            return _summ(view, address, address_hi, "your personal items show once the phone is unlocked.", "फ़ोन अनलॉक होने पर आपके पर्सनल काम दिखेंगे।")
        if not rows:
            return _summ(view, address, address_hi, "no personal items are open.", "कोई पर्सनल काम बाकी नहीं है।")
        first_en, first_hi = _first_two(rows, "day")
        return _summ(view, address, address_hi, f"{len(rows)} personal {'item' if len(rows) == 1 else 'items'}: {first_en}" + (" and more." if len(rows) > 2 else "."),
                     f"{len(rows)} पर्सनल काम: {first_hi}" + (" और बाकी।" if len(rows) > 2 else "।"))
    if kind == "pa":
        items = dash.get("pa_tasks", []) if user.is_md else dash["overdue"] + dash["today"] + dash["upcoming"] + dash["anytime"]
        pa = services.get_pa()
        name = pa.spoken_name if pa else "your PA"
        view = _progress_table("pa", f"{name}'s tasks" if user.is_md else "My tasks", items, include_private,
                               "Nothing given yet" if user.is_md else "Nothing on your list yet")
        own_ids = {i["id"] for i in items if i.get("origin") == SharedTask.Origin.PA_SELF}
        for row in view["rows"]:  # say which ones the MD gave and which are the PA's own
            row["who"] = (f"{name}'s own" if user.is_md else "Your own") if row["id"] in own_ids else \
                ("Given by you" if user.is_md else f"From {sir}")
        open_rows = [r for r in view["rows"] if r["progress"] < 100]
        if not open_rows:
            return _summ(view, address, address_hi, f"{name} has nothing open." if user.is_md else "nothing is open on your list.",
                         f"{name} के पास कोई काम बाकी नहीं है।" if user.is_md else "आपकी लिस्ट में कुछ बाकी नहीं है।")
        first_en, first_hi = _first_two(open_rows)
        n = len(open_rows)
        en = (f"{name} has {n} open" if user.is_md else f"you have {n} open") + f": {first_en}" + (f" and {n - 2} more." if n > 2 else ".")
        hi = (f"{name} के पास {n} काम बाकी हैं" if user.is_md else f"आपके {n} काम बाकी हैं") + f": {first_hi}" + (f" और {n - 2} और।" if n > 2 else "।")
        return _summ(view, address, address_hi, en, hi)
    if kind == "staff":
        view = _progress_table("staff", "Given to staff", dash["delegated"], include_private, "Nothing with staff")
        open_rows = [r for r in view["rows"] if r["progress"] < 100]
        late_rows = [r for r in open_rows if r["overdue"]]
        if not open_rows:
            return _summ(view, address, address_hi, "nothing is open with the staff.", "स्टाफ़ के पास कोई काम बाकी नहीं है।")
        n = len(open_rows)
        en = f"{n} open with staff" + (f", {len(late_rows)} late" if late_rows else "") + f"; first {open_rows[0]['title']} with {open_rows[0]['who']}."
        hi = f"स्टाफ़ के पास {n} काम बाकी हैं" + (f", {len(late_rows)} लेट" if late_rows else "") + f"; सबसे पहले {open_rows[0]['title']}, {open_rows[0]['who']} के पास।"
        return _summ(view, address, address_hi, en, hi)
    if kind == "requests":
        view = _requests_view(user, dash)
        rows = view["rows"]
        waiting = [r for r in rows if r["status"] == "Pending"]
        if user.is_md:
            if not waiting:
                return _summ(view, address, address_hi, "no requests are waiting for you.", "कोई रिक्वेस्ट आपके इंतज़ार में नहीं है।")
            n = len(waiting)
            r = waiting[0]
            return _summ(view, address, address_hi, f"{n} {'request' if n == 1 else 'requests'} waiting: {r['title']} from {r['who']}" + (f" and {n - 1} more." if n > 1 else "."),
                         f"{n} रिक्वेस्ट इंतज़ार में: {r['who']} की {r['title']}" + (f" और {n - 1} और।" if n > 1 else "।"))
        if not waiting:
            return _summ(view, address, address_hi, f"nothing of yours is waiting for {sir}.", f"आपकी कोई रिक्वेस्ट {sir_hi} के पास बाकी नहीं है।")
        return _summ(view, address, address_hi, f"{len(waiting)} of your {'request is' if len(waiting) == 1 else 'requests are'} waiting for {sir}: {waiting[0]['title']}.",
                     f"आपकी {len(waiting)} रिक्वेस्ट {sir_hi} के इंतज़ार में: {waiting[0]['title']}।")
    if kind == "busy":
        view = _busy_view(dash)
        labels = [r["when"] for r in view["rows"]]
        if not labels:
            return _summ(view, address, address_hi, f"{sir} has no busy time today.", f"आज {sir_hi} का कोई समय व्यस्त नहीं है।")
        return _summ(view, address, address_hi, f"{sir} is busy {', '.join(labels[:3])}" + (" and later." if len(labels) > 3 else "; free otherwise."),
                     f"{sir_hi} {', '.join(labels[:3])} व्यस्त हैं" + ("।" if len(labels) > 3 else "; बाकी समय खाली हैं।"))
    if kind == "performance":
        view = _performance_view(user)
        rows = view["rows"]
        if not rows:
            return _summ(view, address, address_hi, "no staff work in the last 30 days to report on.", "पिछले 30 दिन में स्टाफ़ का कोई काम नहीं है।")
        by = {s["label"]: s["value"] for s in view["stats"]}
        late_people = [r["title"] for r in rows if r["late"]]
        en = f"last 30 days: {by['Assigned']} given, {by['Done']} done, {by['On time']} on time" + (f"; late: {', '.join(late_people[:3])}." if late_people else "; nobody is late.")
        hi = f"पिछले 30 दिन: {by['Assigned']} दिए, {by['Done']} पूरे, {by['On time']} समय पर" + (f"; लेट: {', '.join(late_people[:3])}।" if late_people else "; कोई लेट नहीं है।")
        return _summ(view, address, address_hi, en, hi)
    if kind == "done":
        view = _done_view(user, include_private)
        by = {s["label"]: s["value"] for s in view["stats"]}
        rows = view["rows"]
        if not rows:
            return _summ(view, address, address_hi, "nothing was finished in the last 14 days.", "पिछले 14 दिन में कुछ पूरा नहीं हुआ।")
        en = f"{by['Today']} finished today, {by['This week']} this week; latest {rows[0]['title']}" + (f" by {rows[0]['who']}." if rows[0]["who"] not in ("You", "") else ".")
        hi = f"आज {by['Today']} पूरे हुए, इस हफ़्ते {by['This week']}; सबसे ताज़ा {rows[0]['title']}।"
        return _summ(view, address, address_hi, en, hi)
    if kind == "cancelled":
        view = _cancelled_view(user, include_private)
        rows = view["rows"]
        if not rows:
            return _summ(view, address, address_hi, "nothing was cancelled in the last 30 days.", "पिछले 30 दिन में कुछ कैंसल नहीं हुआ।")
        n = len(rows)
        en = f"{n} cancelled in the last 30 days; latest {rows[0]['title']} {rows[0]['day']}. Say restore to bring one back."
        hi = f"पिछले 30 दिन में {n} कैंसल; सबसे ताज़ा {rows[0]['title']} {rows[0]['day']}। वापस लाना हो तो रीस्टोर बोलिए।"
        return _summ(view, address, address_hi, en, hi)
    return None


_QUERY_HINTS = [
    ("performance", ("performance", "report", "kaisa kaam", "kis ne kitna", "productivity", "kaun late", "who is late", "on time")),
    ("history", ("last time", "pichhli baar", "pichli baar", "kab mila", "last met", "last meeting with", "history", "kab diya tha", "pehle kab")),
    ("cancelled", ("cancelled", "canceled", "cancel kiye", "cancel hue", "hataye", "deleted", "restore")),
    ("done", ("completed", "finished", "ho gaya", "ho gaye", "khatam", "kya kya hua", "what got done")),
    ("requests", ("request", "approve", "approval", "ok karna", "waiting for", "pending request", "manzoori")),
    ("busy", ("busy", "free kab", "khaali", "khali", "free time")),
    ("people", ("staff ki list", "staff list", "list of my staff", "list of staff", "list of the staff", "staff ke naam",
                "staff members", "employees", "kaun kaun hai", "kitne log", "kitne staff", "chhutti par", "on leave",
                "department wise", "who is in", "mein kaun hai")),
    ("staff", ("staff", "team", "ramesh", "delegat", "taskpanel", "kaun kar raha")),
    ("pa", ("mansi", "pa ke", "pa ko", "assistant")),
    ("private", ("private", "personal", "mere personal")),
    ("official", ("official", "office ke", "kaam ke", "work items")),
    ("late", ("late", "overdue", "reh gay", "chhoot", "slipped")),
    ("pending", ("pending", "baaki", "baki", "koi task", "kya karna", "to do", "todo")),
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
