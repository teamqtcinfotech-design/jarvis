"""Demo work from his own task export (26 Nov 2026): real subjects from the old TMS dump, laid over the days that
matter for testing — yesterday and earlier (late), today, tomorrow, the day after, next month — a few each to named
staff in the directory (through the TaskPanel gateway, so they reach the staff's real panel) and to the MD himself
(his own list, meetings included).

    python manage.py seed_tasks /srv/jarvis/tasks.json --staff "Nikki:6,Gauri:5" --me 14 --dry-run   # the plan only
    python manage.py seed_tasks /srv/jarvis/tasks.json --staff "Nikki:6,Gauri:5" --me 14             # do it
    python manage.py seed_tasks --demo farmhouse --driver "Driver" --dry-run    # his farmhouse / car sheet (27 Nov 2026)
    python manage.py seed_tasks --demo farmhouse --driver "Driver"

The file is the phpMyAdmin export of the `tasks` table (a list whose "table" entry holds the rows), or a plain list
of rows / titles. Only open rows are used, demo and test rows are skipped, every title once. The same --seed gives
the same choice of titles. The demo list is built in: the car work goes to the staff member named with --driver
(through the panel), the farmhouse and office work to the MD's own list under their sections, late and ahead.
Nothing is deleted; run clear_data first for a clean slate.
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


# His sheets (27 Nov 2026): the V Class car work for the driver, the farmhouse tracker and a few office items for
# himself. (to, section, priority, day offset from today, title): the late ones are what "pending" looks like.
DEMO = {
    "farmhouse": [
        ("driver", "", Priority.HIGH, -3, "V Class car ke WiFi mein SIM lagwani hai"),
        ("driver", "", Priority.NORMAL, 0, "V Class door cleaning"),
        ("driver", "", Priority.HIGH, -1, "V Class ka mic kaam nahi kar raha"),
        ("driver", "", Priority.HIGH, 1, "V Class ka wireless kaam nahi kar raha"),
        ("driver", "", Priority.NORMAL, 5, "V Class ki servicing book karni hai"),
        ("driver", "", Priority.HIGH, 10, "Car insurance renewal ke papers lene hain"),
        ("driver", "", Priority.NORMAL, 2, "Fuel card activate karwana hai"),
        ("driver", "", Priority.NORMAL, 30, "V Class ke tyre pressure aur alignment check karwana hai"),
        ("me", "Farmhouse", Priority.NORMAL, -7, "Buddha murti theek karwani hai"),
        ("me", "Farmhouse", Priority.HIGH, -3, "Sir ke office mein extension lagwana hai"),
        ("me", "Farmhouse", Priority.NORMAL, -1, "Sir ke bedroom mein extension board lagwana hai"),
        ("me", "Farmhouse", Priority.NORMAL, 0, "Khane ki SOP banni hai"),
        ("me", "Farmhouse", Priority.NORMAL, 1, "Farm par kitne drivers chahiye, tay karna hai"),
        ("me", "Farmhouse", Priority.HIGH, -2, "Fountain mein problem aa rahi hai"),
        ("me", "Farmhouse", Priority.HIGH, 0, "Swimming pool mein light problem aur hot water"),
        ("me", "Farmhouse", Priority.URGENT, -1, "Chimney kaam nahi kar rahi"),
        ("me", "Farmhouse", Priority.NORMAL, 3, "Main gate ke liye original flowers mangwane hain"),
        ("me", "Farmhouse", Priority.NORMAL, 1, "Farmhouse ke liye electrician bulana hai"),
        ("me", "Farmhouse", Priority.LOW, 7, "Farmhouse ke liye chairs leni hain"),
        ("me", "Farmhouse", Priority.LOW, 5, "Farmhouse ke sweets order karne hain"),
        ("me", "Farmhouse", Priority.NORMAL, 14, "Garden ki pump motor service karwani hai"),
        ("me", "Farmhouse", Priority.HIGH, 2, "Farmhouse ke CCTV cameras check karwane hain"),
        ("me", "Farmhouse", Priority.NORMAL, 10, "Water tank ki safai karwani hai"),
        ("me", "Farmhouse", Priority.HIGH, 30, "Generator ki servicing book karni hai"),
        ("me", "Farmhouse", Priority.NORMAL, 4, "Staff quarters ka AC theek karwana hai"),
        ("me", "Farmhouse", Priority.LOW, 20, "Solar panel ka quotation lena hai"),
        ("me", "Farmhouse", Priority.NORMAL, 6, "Lawn ke liye mali rakhna hai"),
        ("me", "Office", Priority.NORMAL, -2, "Office ke printer ka toner mangwana hai"),
        ("me", "Office", Priority.HIGH, 1, "Conference room ka projector theek karwana hai"),
    ],
}


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
        parser.add_argument("file", nargs="?", default="", help="the task export (JSON); not needed with --demo")
        parser.add_argument("--staff", default="", help='who gets how many: "Nikki:6,Gauri:5" (names from the staff directory)')
        parser.add_argument("--me", type=int, default=0, help="how many tasks for the MD's own list (plus a few meetings)")
        parser.add_argument("--no-meetings", action="store_true", help="tasks only for the MD, no meetings")
        parser.add_argument("--seed", type=int, default=26, help="the same seed picks the same titles")
        parser.add_argument("--demo", default="", choices=["", *DEMO], help="a built-in list instead of a file (farmhouse)")
        parser.add_argument("--driver", default="Driver", help="--demo: the staff member who gets the car work (a name from the directory)")
        parser.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")

    def handle(self, *args, **opts):
        md = User.objects.filter(role=User.Role.MD, is_active=True).first()
        if md is None:
            raise CommandError("no MD account in this database")
        if opts["demo"]:
            return self._demo(md, opts)
        if not opts["file"]:
            raise CommandError("give the task export file, or --demo farmhouse")
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
        meetings_wanted = bool(opts["me"]) and not opts["no_meetings"]
        titles = pick_titles(rows, total + (len(MEETINGS) if meetings_wanted else 0), opts["seed"])  # the meetings get titles of their own
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
        if meetings_wanted:
            now = tu.now_local()
            for k, (offset, hour, minute, length) in enumerate(MEETINGS):
                day = today + timedelta(days=offset)
                if offset == 0 and tu.combine_local(day, time(hour, minute)) <= now + timedelta(minutes=20):
                    day = day + timedelta(days=1)  # the hour has passed today: tomorrow at the same time
                subject = titles[at + k]["title"]
                plan.append((None, md.spoken_name, Kind.MEETING, day, time(hour, minute), length,
                             Priority.HIGH if k == 0 else Priority.NORMAL, f"Meeting: {subject[:70]}"))

        staff_n = sum(n for _n, n in wants)
        self._run(md, [(*p, "") for p in plan], staff_n, opts["dry_run"])

    def _demo(self, md: User, opts: dict):
        """The built-in list: the car work to the driver (the panel), the rest to the MD's own list under its section."""
        rows = DEMO[opts["demo"]]
        driver = self._staff(opts["driver"]) if any(r[0] == "driver" for r in rows) else None
        today = tu.today_local()
        plan = []
        for to, section, priority, offset, title in rows:
            day = today + timedelta(days=offset)
            if to == "driver":
                plan.append((driver, driver.name, Kind.TASK, day, None, None, priority, title, ""))
            else:
                plan.append((None, md.spoken_name, Kind.TASK, day, None, None, priority, title, section))
        self._run(md, plan, sum(1 for r in rows if r[0] == "driver"), opts["dry_run"])

    def _run(self, md: User, plan: list, staff_n: int, dry_run: bool):
        today = tu.today_local()
        self.stdout.write(f"{'(plan only) ' if dry_run else ''}{len(plan)} items:")
        for who, label, kind, day, when, minutes, priority, title, section in plan:
            late = " LATE" if day < today else ""
            at_s = f" {when.strftime('%H:%M')}" if when else ""
            sec = f" [{section}]" if section else ""
            self.stdout.write(f"  {label:<14} {day}{at_s:<6} {kind:<8} {priority:<7}{late:<5} {title[:70]}{sec}")
        if dry_run:
            return
        made = sent = failed = 0
        for who, label, kind, day, when, minutes, priority, title, section in plan:
            data = {"title": title, "kind": kind, "priority": priority, "due_date": day}
            if when:
                data["due_time"] = when
            if minutes:
                data["duration_minutes"] = minutes
            if section:
                data["section"] = section
            try:
                if who is None:
                    services.create_private_task(md, data)
                    made += 1
                else:
                    task, cmd = services.delegate_to_staff(md, data, who, description=title)
                    made += 1
                    if cmd.state == IntegrationCommand.State.REJECTED:
                        failed += 1
                        self.stdout.write(self.style.WARNING(f"  panel refused '{title[:60]}' for {who.name}: {cmd.error_message or cmd.error_code or cmd.state}"))
                    elif cmd.state == IntegrationCommand.State.SUCCEEDED:
                        sent += 1
            except Exception as err:  # noqa: BLE001 — one bad item must not stop the rest
                failed += 1
                self.stdout.write(self.style.WARNING(f"  skipped '{title[:60]}' for {label}: {err}"))
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
