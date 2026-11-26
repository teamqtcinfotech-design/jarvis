"""Demo work from his own task export (26 Nov 2026): real subjects from the old TMS dump, laid over the days that
matter for testing — yesterday and earlier (late), today, tomorrow, the day after, next month — a few each to named
staff in the directory (through the TaskPanel gateway, so they reach the staff's real panel) and to the MD himself
(his own list, meetings included).

    python manage.py seed_tasks /srv/jarvis/tasks.json --staff "Nikki:6,Gauri:5" --me 14 --dry-run   # the plan only
    python manage.py seed_tasks /srv/jarvis/tasks.json --staff "Nikki:6,Gauri:5" --me 14             # do it

The file is the phpMyAdmin export of the `tasks` table (a list whose "table" entry holds the rows), or a plain list
of rows / titles. Only open rows are used, demo and test rows are skipped, every title once. The same --seed gives
the same choice of titles. Nothing is deleted; run clear_data first for a clean slate.
"""
import json
import random
from datetime import time, timedelta

from django.core.management.base import BaseCommand, CommandError

from core import services
from core import timeutils as tu
from core.models import IntegrationCommand, Kind, Priority, StaffMember, User

PRIORITY = {"extreme": Priority.URGENT, "urgent": Priority.URGENT, "high": Priority.HIGH, "medium": Priority.NORMAL,
            "normal": Priority.NORMAL, "low": Priority.LOW}
SKIP_WORDS = ("demo", "test", "testing", "sample", "dummy")
# The spread of days for each person's items, in turn: yesterday (late), today, tomorrow, the day after, next month,
# three days ago (late), today again, tomorrow again … so "a few" already covers every day he wants to test.
OFFSETS = (-1, 0, 1, 2, 30, -3, 0, 1, -7, 2, 31, 0)
# The MD's meetings: a couple a day on the near days, one next month (hour, minutes, day offset); past hours today
# move to the next day so nothing is born already over.
MEETINGS = ((0, 11, 30, 30), (0, 16, 0, 45), (1, 11, 0, 30), (1, 15, 30, 45), (2, 12, 0, 30), (30, 11, 0, 60))


def load_rows(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("data") or data.get("rows") or []
    rows: list = []
    if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("type") in ("header", "database", "table"):
        for part in data:
            if isinstance(part, dict) and part.get("type") == "table" and isinstance(part.get("data"), list):
                rows.extend(part["data"])
    else:
        rows = data if isinstance(data, list) else []
    out = []
    for r in rows:
        if isinstance(r, str):
            out.append({"subject": r})
        elif isinstance(r, dict):
            out.append(r)
    return out


def pick_titles(rows: list[dict], count: int, seed: int) -> list[dict]:
    """Open rows with a real subject, every title once, demo rows out, shuffled the same way for the same seed."""
    seen = set()
    cands = []
    for r in rows:
        if str(r.get("status") or "").strip().lower() == "completed":
            continue
        title = " ".join(str(r.get("subject") or r.get("title") or "").split())
        key = title.lower()
        if len(title) < 8 or key in seen or any(w in key.split() for w in SKIP_WORDS):
            continue
        seen.add(key)
        cands.append({"title": title[:200], "priority": PRIORITY.get(str(r.get("priority") or "").strip().lower(), Priority.NORMAL),
                      "hours": r.get("estimated_hours")})
    random.Random(seed).shuffle(cands)
    if len(cands) < count:
        raise CommandError(f"only {len(cands)} usable titles in the file; {count} were asked for")
    return cands[:count]


def minutes_of(hours) -> int | None:
    try:
        h = float(hours)
    except (TypeError, ValueError):
        return None
    if h <= 0 or h >= 8:
        return None
    return max(10, min(120, int(round(h * 60 / 5)) * 5))


class Command(BaseCommand):
    help = "Lay demo work from a task export over the test days, to named staff and to the MD."

    def add_arguments(self, parser):
        parser.add_argument("file", help="the task export (JSON)")
        parser.add_argument("--staff", default="", help='who gets how many: "Nikki:6,Gauri:5" (names from the staff directory)')
        parser.add_argument("--me", type=int, default=0, help="how many tasks for the MD's own list (plus a few meetings)")
        parser.add_argument("--no-meetings", action="store_true", help="tasks only for the MD, no meetings")
        parser.add_argument("--seed", type=int, default=26, help="the same seed picks the same titles")
        parser.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")

    def handle(self, *args, **opts):
        md = User.objects.filter(role=User.Role.MD, is_active=True).first()
        if md is None:
            raise CommandError("no MD account in this database")
        rows = load_rows(opts["file"])
        if not rows:
            raise CommandError("no rows in the file")
        wants = []
        for part in [p.strip() for p in opts["staff"].split(",") if p.strip()]:
            name, _, n = part.partition(":")
            try:
                n = int(n or 3)
            except ValueError:
                raise CommandError(f"'{part}': say NAME:COUNT") from None
            wants.append((name.strip(), max(1, n)))
        people = [(self._staff(name), n) for name, n in wants]
        total = sum(n for _n, n in wants) + max(0, opts["me"])
        if not total:
            raise CommandError("nothing to do: give --staff and/or --me")
        titles = pick_titles(rows, total, opts["seed"])
        today = tu.today_local()
        plan = []  # (who, label, kind, due_date, due_time, minutes, priority, title)
        at = 0
        for staff, n in people:
            for i in range(n):
                t = titles[at]
                at += 1
                plan.append((staff, staff.name, Kind.TASK, today + timedelta(days=OFFSETS[i % len(OFFSETS)]), None, minutes_of(t["hours"]),
                             t["priority"], t["title"]))
        for i in range(max(0, opts["me"])):
            t = titles[at]
            at += 1
            day = today + timedelta(days=OFFSETS[i % len(OFFSETS)])
            plan.append((None, md.spoken_name, Kind.TASK, day, None, minutes_of(t["hours"]), t["priority"], t["title"]))
        if opts["me"] and not opts["no_meetings"]:
            now = tu.now_local()
            for k, (offset, hour, minute, length) in enumerate(MEETINGS):
                day = today + timedelta(days=offset)
                if offset == 0 and tu.combine_local(day, time(hour, minute)) <= now + timedelta(minutes=20):
                    day = day + timedelta(days=1)  # the hour has passed today: tomorrow at the same time
                subject = titles[(at + k) % len(titles)]["title"]
                plan.append((None, md.spoken_name, Kind.MEETING, day, time(hour, minute), length,
                             Priority.HIGH if k == 0 else Priority.NORMAL, f"Meeting: {subject[:70]}"))

        self.stdout.write(f"{'(plan only) ' if opts['dry_run'] else ''}{len(plan)} items over {today - timedelta(days=7)} … {today + timedelta(days=31)}:")
        for who, label, kind, day, when, minutes, priority, title in plan:
            late = " LATE" if day < today else ""
            at_s = f" {when.strftime('%H:%M')}" if when else ""
            self.stdout.write(f"  {label:<14} {day}{at_s:<6} {kind:<8} {priority:<7}{late:<5} {title[:70]}")
        if opts["dry_run"]:
            return
        made = sent = failed = 0
        for who, label, kind, day, when, minutes, priority, title in plan:
            data = {"title": title, "kind": kind, "priority": priority, "due_date": day}
            if when:
                data["due_time"] = when
            if minutes:
                data["duration_minutes"] = minutes
            try:
                if who is None:
                    services.create_private_task(md, data)
                    made += 1
                else:
                    task, cmd = services.delegate_to_staff(md, data, who, description=title)
                    made += 1
                    if cmd.state == IntegrationCommand.State.REJECTED:
                        failed += 1
                        self.stdout.write(self.style.WARNING(f"  panel refused '{title[:60]}' for {who.name}: {cmd.last_error or cmd.state}"))
                    elif cmd.state == IntegrationCommand.State.SUCCEEDED:
                        sent += 1
            except Exception as err:  # noqa: BLE001 — one bad item must not stop the rest
                failed += 1
                self.stdout.write(self.style.WARNING(f"  skipped '{title[:60]}' for {label}: {err}"))
        staff_n = sum(n for _n, n in wants)
        self.stdout.write(self.style.SUCCESS(
            f"{made} created ({staff_n} for staff, of which {sent} already in the panel; the rest are delivered by the worker), {failed} failed."))

    def _staff(self, name: str) -> StaffMember:
        own = services.own_actor_keys()
        active = [s for s in StaffMember.objects.filter(active=True) if s.external_id not in own]
        low = name.lower()
        exact = [s for s in active if s.name.lower() == low]
        hits = exact or [s for s in active if low in s.name.lower()]
        if len(hits) == 1:
            return hits[0]
        names = ", ".join(sorted(s.name for s in active))
        if not hits:
            raise CommandError(f"no staff member called '{name}'. The directory has: {names}")
        raise CommandError(f"'{name}' fits several: {', '.join(s.name for s in hits)}. Say the full name.")
