"""Lists he can ask for in any combination: the people themselves, and anybody's tasks for any day or period.

The AI never draws anything. These are the same fixed tables and charts every time (templates); the AI only
fills in a small request (whose, which period, which ones) and the server fills the template with the real
data and writes the sentence that is spoken. So every number is exact, and "Ramesh this week" looks exactly
like "Neha yesterday": the same chart, different data.

Privacy: Sir's personal tasks are read only for Sir himself, and only when include_private is true. When Mansi
asks about Sir she gets his official (shared) tasks and nothing else.
"""
from __future__ import annotations

import difflib
import re
from datetime import date, datetime, time, timedelta

from . import datacards, services
from . import timeutils as tu
from .models import SharedTask, StaffMember, Status, TaskPanelLink, User

_ME = {"", "me", "my", "mine", "myself", "i", "self", "own", "mera", "mere", "meri", "main", "apna", "apne", "apni", "khud"}
_PA = {"pa", "p.a.", "assistant", "my pa", "meri pa", "personal assistant", "secretary"}
_MD = {"sir", "md", "boss", "managing director", "the md", "sahab", "saab", "ma'am", "maam", "mam", "madam", "madame", "ma am"}
_STAFF = {"staff", "team", "all staff", "the staff", "the team", "employees", "sab staff", "poori team", "whole team",
          "my staff", "my team"}
_ALL = {"all", "everyone", "everybody", "sab", "sabka", "sab log", "all of us"}
_TEAM_WORDS = re.compile(r"\b(team|department|dept|waale|wale|walon|waalon|log|ka|ke|ki)\b")
_FILTERS = {"open", "done", "late", "on_leave", "personal", "official"}
_TYPES = {"TASK", "MEETING", "CALL", "PAYMENT", "FOLLOW_UP"}
_FROM_WHOM = {"sir", "pa", "me", "staff"}
_PRIORITIES = {"urgent", "high", "important"}
_LIMIT = 400          # most recent tasks read per source
_ROWS = 80            # rows sent to the screen
_HI_MONTHS = tu._HI_MONTHS

_TYPE_EN = {"TASK": ("task", "tasks"), "MEETING": ("meeting", "meetings"), "CALL": ("call", "calls"),
            "PAYMENT": ("payment", "payments"), "FOLLOW_UP": ("follow-up", "follow-ups")}
_TYPE_HI = {"TASK": "टास्क", "MEETING": "मीटिंग", "CALL": "कॉल", "PAYMENT": "पेमेंट", "FOLLOW_UP": "फ़ॉलो-अप"}


# --------------------------------------------------------------------------- the request
def clean_params(raw) -> dict:
    """The request form, validated: who, from, to, by (due / given / done), filter, group (department), and the
    narrowing filters: types (meeting / call / …), from_whom (sir / pa / me / staff), priority (urgent / high /
    important), words (from the title: "auditor", "Pune"), ids (the exact items he pointed at)."""
    raw = raw if isinstance(raw, dict) else {}

    def day(value):
        try:
            return date.fromisoformat(str(value)[:10]) if value else None
        except ValueError:
            return None

    def words_list(value, allowed, upper=False):
        if isinstance(value, str):
            value = value.split(",")
        out = []
        for v in value if isinstance(value, list) else []:
            if not isinstance(v, str):
                continue
            v = v.strip().upper() if upper else v.strip().lower()
            if v in allowed and v not in out:
                out.append(v)
        return out

    start, end = day(raw.get("from")), day(raw.get("to"))
    start, end = start or end, end or start
    if start and end and end < start:
        start, end = end, start
    if start and end and (end - start).days > 366:
        end = start + timedelta(days=366)
    ids_raw = raw.get("ids")
    if isinstance(ids_raw, str):
        ids_raw = ids_raw.split(",")
    return {
        "who": " ".join(str(raw.get("who") or "").split())[:80],
        "from": start, "to": end,
        "by": raw.get("by") if raw.get("by") in {"due", "given", "done"} else "",
        "filter": raw.get("filter") if raw.get("filter") in _FILTERS else "",
        "group": " ".join(str(raw.get("group") or "").split())[:80],
        "types": words_list(raw.get("types"), _TYPES, upper=True),
        "from_whom": str(raw.get("from_whom") or "").strip().lower() if str(raw.get("from_whom") or "").strip().lower() in _FROM_WHOM else "",
        "priority": str(raw.get("priority") or "").strip().lower() if str(raw.get("priority") or "").strip().lower() in _PRIORITIES else "",
        "words": " ".join(re.findall(r"[\w'’-]+", str(raw.get("words") or "")))[:80],
        "ids": [str(i).strip()[:64] for i in (ids_raw if isinstance(ids_raw, list) else []) if isinstance(i, str) and i.strip()][:10],
    }


def request_of(kind: str, p: dict) -> dict:
    """The request as it was answered, in plain values (dates as text), so the phone can send it back with the
    card and a follow-up ("sirf meetings", "aur Sir ke diye hue") can start from the same filters."""
    out = {"kind": kind}
    for key in ("who", "by", "filter", "group", "from_whom", "priority", "words"):
        if p.get(key):
            out[key] = p[key]
    if p.get("from"):
        out["from"], out["to"] = p["from"].isoformat(), (p.get("to") or p["from"]).isoformat()
    if p.get("types"):
        out["types"] = list(p["types"])
    return out


def has_request(p: dict) -> bool:
    """A particular person, period or narrowing was asked for (so the generic template answers, not a standard view)."""
    return bool(p["from"] or (p["who"] and p["who"].lower() not in _ME) or p["types"] or p["from_whom"] or p["priority"]
                or p["words"] or p["ids"])


def _departments() -> dict[str, str]:
    names = StaffMember.objects.filter(active=True).exclude(department="").values_list("department", flat=True).distinct()
    return {n.lower(): n for n in names}


def _match_department(text: str) -> str:
    wanted = " ".join(_TEAM_WORDS.sub(" ", (text or "").lower()).split())
    if not wanted:
        return ""
    depts = _departments()
    if wanted in depts:
        return depts[wanted]
    # Whole words only: "it" must never match inside "Avitha Singh".
    words = set(wanted.split())
    close = [name for low, name in depts.items() if set(low.split()) <= words or words <= set(low.split())]
    return close[0] if len(close) == 1 else ""


def resolve_subject(user, who: str) -> dict:
    """Whose tasks: {"type": me | pa | md | staff_one | department | staff_all | all | ambiguous | unknown, "label", ...}."""
    raw = " ".join((who or "").lower().split())
    pa = services.get_pa()
    pa_name = pa.spoken_name if pa else "your PA"
    if raw in _ME:
        return {"type": "me", "label": "you"}
    key = services.norm_person(raw)
    md = services.get_md()
    md_names = {services.norm_person(n) for n in (md.display_name, md.first_name, md.get_full_name()) if n} if md else set()
    md_names.add(services.norm_person(services.md_address()))
    if raw in _MD or (key and key in md_names):  # "Sir", "Ma'am", or the MD's own name
        return {"type": "me", "label": "you"} if user.is_md else {"type": "md", "label": services.md_address()}
    pa_names = {services.norm_person(n) for n in (pa.display_name, pa.first_name, pa.username, pa.get_full_name()) if n} if pa else set()
    if raw in _PA or (key and key in pa_names):
        return {"type": "me", "label": "you"} if user.is_pa else {"type": "pa", "label": pa_name}
    mg = services.get_manager()
    mg_names = {services.norm_person(n) for n in (mg.display_name, mg.first_name, mg.get_full_name()) if n} if mg else set()
    if raw in {"manager", "the manager"} or (key and key in mg_names):
        return {"type": "me", "label": "you"} if user.is_mg else {"type": "mg", "label": mg.spoken_name if mg else "the manager"}
    if raw in _STAFF:
        return {"type": "staff_all", "label": "the staff"}
    if raw in _ALL:
        return {"type": "all", "label": "everyone"}
    matches, partial = services.resolve_staff(raw)          # as said ("QTC Developer2" keeps its number)
    if not matches and key and key != raw:
        matches, more = services.resolve_staff(key)         # without ji / sir / Mr
        partial = partial or more
    if len(matches) == 1:
        return {"type": "staff_one", "label": matches[0].name, "staff": matches[0]}
    if len(matches) > 1:
        return {"type": "ambiguous", "label": who, "options": [f"{m.name} from {m.department}" for m in matches[:3]]}
    dept = _match_department(raw)
    if dept:
        return {"type": "department", "label": dept}
    near = _closest_staff(key)
    if near:
        # The microphone heard the name a little wrong ("Avitha Singh"): for looking things up, take the one
        # name it clearly was. (Giving someone work still asks "did you mean…?" first.)
        return {"type": "staff_one", "label": near.name, "staff": near}
    return {"type": "unknown", "label": who,
            "suggestions": [f"{s.name} from {s.department}" if s.department else s.name for s in partial[:2]]}


def _closest_staff(key: str):
    """The one staff member a misheard name clearly meant, or None when nothing is close or two are equally close."""
    if len(key) < 4:
        return None
    scored = []
    for s in StaffMember.objects.filter(active=True):
        full = services.norm_person(s.name)
        if not full:
            continue
        ratio = difflib.SequenceMatcher(None, key, full).ratio()
        if " " not in key:  # only a first name was said
            ratio = max(ratio, difflib.SequenceMatcher(None, key, full.split()[0]).ratio())
        scored.append((ratio, s))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if not scored or scored[0][0] < 0.8:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.05:
        return None
    return scored[0][1]


# --------------------------------------------------------------------------- reading the tasks
def _shared(qs) -> list[dict]:
    return [services.shared_item(t) for t in qs.exclude(status=Status.CANCELLED).order_by("-created_at")[:_LIMIT]]


def _collect(user, subject: dict, include_private: bool) -> list[dict]:
    """Every task (not cancelled) this user may see for this subject."""
    kind = subject["type"]
    items: list[dict] = []
    if kind in ("me", "all"):
        if user.is_md:
            if include_private:  # his personal items: for him alone, and not on a locked phone
                items += [services.private_item(t) for t in
                          services.md_private_qs(user).exclude(status=Status.CANCELLED).order_by("-created_at")[:_LIMIT]]
            items += _shared(services.shared_md_tasks_qs())
            items += _shared(services.pa_tasks_qs().filter(origin=SharedTask.Origin.MD_MEET_PA))
        else:
            items += _shared(services.assistant_tasks_qs(user))
    if kind == "pa":
        items += _shared(services.pa_tasks_qs())
    if kind == "mg":
        items += _shared(services.mg_tasks_qs())
    if kind == "all" and user.is_md:
        items += _shared(services.pa_tasks_qs().exclude(origin=SharedTask.Origin.MD_MEET_PA))
        items += _shared(services.mg_tasks_qs())
    if kind == "md" or (kind == "all" and user.is_assistant):
        items += _shared(services.shared_md_tasks_qs())  # official only: his personal items are never read here
    if kind == "staff_one":
        items += _shared(services.delegated_qs().filter(staff=subject["staff"]))
    if kind == "department":
        items += _shared(services.delegated_qs().filter(staff__department__iexact=subject["label"]))
    if kind in ("staff_all", "all"):
        items += _shared(services.delegated_qs())
    seen, unique = set(), []
    for item in items:
        mark = (item["scope"], item["id"])
        if mark not in seen:
            seen.add(mark)
            unique.append(item)
    return unique


def _iso_day(value) -> date | None:
    return tu.to_local(datetime.fromisoformat(value)).date() if value else None


def _due_day(item: dict) -> date | None:
    when = item.get("scheduled_start") or item.get("due_at")
    if when:
        return _iso_day(when)
    return date.fromisoformat(item["due_date"]) if item.get("due_date") else None


def _due_moment(item: dict) -> datetime | None:
    if item.get("due_at"):
        return datetime.fromisoformat(item["due_at"])
    if item.get("due_date"):
        return tu.combine_local(date.fromisoformat(item["due_date"]), time(23, 59))
    return None


def _is_done(item: dict) -> bool:
    return item["status"] == Status.COMPLETED


def _finished_late(item: dict) -> bool:
    due = _due_moment(item)
    return bool(_is_done(item) and due and item.get("completed_at") and datetime.fromisoformat(item["completed_at"]) > due)


def _in_period(item: dict, start: date | None, end: date | None, by: str, today: date) -> bool:
    if not start:
        # No period said: what is open, plus what was finished in the last 30 days.
        done_on = _iso_day(item.get("completed_at"))
        return not _is_done(item) or bool(done_on and done_on >= today - timedelta(days=30))

    def inside(d):
        return d is not None and start <= d <= end

    if by == "given":
        return inside(_iso_day(item.get("created_at")))
    if by == "done":
        return _is_done(item) and inside(_iso_day(item.get("completed_at")))
    if by == "due":
        return inside(_due_day(item))
    # What was planned for the period: due in it, or finished in it. Late items from earlier days are NOT pulled
    # into "today" (he asked about today); they are counted and offered separately, and "late wale" lists them.
    if inside(_due_day(item)) or (_is_done(item) and inside(_iso_day(item.get("completed_at")))):
        return True
    # Work with no date on it is the work of the day it was given, for as long as it is open: "aaj mere kya kaam
    # hain" must name the urgent thing Sir gave an hour ago even though nobody put a date on it.
    return _due_day(item) is None and not _is_done(item) and inside(_iso_day(item.get("created_at")))


def _from_whom(item: dict, whom: str, user) -> bool:
    """Who the item came from, as the speaker sees it: Sir's gifts to Mansi, her own, what the PA sent Sir, staff work."""
    origin = item.get("origin")
    assignee = item.get("assignee", {}).get("type")
    if whom == "staff":
        return assignee == "STAFF"
    if user.is_assistant:
        if whom == "sir":
            return origin in (SharedTask.Origin.MD_TO_PA, SharedTask.Origin.MD_MEET_PA, SharedTask.Origin.MD_TO_MG) or assignee == "MD"
        if whom == "pa" and user.is_mg:
            return origin == SharedTask.Origin.PA_TO_MG  # what the PA gave the manager
        return origin in services.OWN_ORIGINS  # "me": their own
    if whom == "pa":
        return origin in (SharedTask.Origin.PA_REQUEST, SharedTask.Origin.PA_SELF)
    # "me" / "sir" for Sir: his own, personal or official, not what the PA sent him
    return item.get("scope") == "private" or (assignee in ("MD", "PA") and origin not in (SharedTask.Origin.PA_REQUEST, SharedTask.Origin.PA_SELF))


_WANT_PRIORITY = {"urgent": {"URGENT"}, "high": {"HIGH"}, "important": {"URGENT", "HIGH"}}


def _keep(item: dict, p: dict, user) -> bool:
    """Every filter of the request together: open / done / late / personal / official, the type, who it came
    from, the priority and the exact items pointed at. Title words are matched afterwards (see _by_words)."""
    flt = p["filter"]
    if flt == "open" and _is_done(item):
        return False
    if flt == "done" and not _is_done(item):
        return False
    if flt == "late" and not item.get("overdue"):
        return False
    if flt == "personal" and item.get("scope") != "private":
        return False
    if flt == "official" and item.get("scope") == "private":
        return False
    if p["types"] and item.get("kind") not in p["types"]:
        return False
    if p["priority"] and item.get("priority") not in _WANT_PRIORITY[p["priority"]]:
        return False
    if p["from_whom"] and not _from_whom(item, p["from_whom"], user):
        return False
    if p["ids"] and item["id"] not in p["ids"]:
        return False
    return True


def _by_words(items: list[dict], words: str) -> list[dict]:
    """The items whose title (or description) carries the words said ("auditor", "Pune vendor"): the best matches."""
    tokens = [t for t in re.findall(r"\w+", words.lower()) if len(t) > 2]
    if not tokens:
        return items
    scored = []
    for item in items:
        text = f"{item.get('title', '')} {item.get('description') or item.get('notes') or ''}".lower()
        score = sum(1 for t in tokens if t in text)
        if score:
            scored.append((score, item))
    if not scored:
        return []
    best = max(s for s, _ in scored)
    return [i for s, i in scored if s == best]


def _ordered(items: list[dict]) -> list[dict]:
    still = sorted([i for i in items if not _is_done(i)], key=services._sort_key)
    done = sorted([i for i in items if _is_done(i)], key=lambda i: i.get("completed_at") or "", reverse=True)
    return still + done


def _owner(item: dict, user) -> str:
    who = item.get("assignee", {})
    if who.get("type") == "STAFF":
        return who.get("staff_name") or "Staff"
    if who.get("type") == "PA":
        return "You" if user.is_pa else (who.get("staff_name") or "PA")
    if who.get("type") == "MG":
        return "You" if user.is_mg else (who.get("staff_name") or "Manager")
    return "You" if user.is_md else services.md_address()


# --------------------------------------------------------------------------- words
def _period_title(start, end) -> str:
    if not start:
        return ""
    if start == end:
        word = tu.fmt_day(start)
        return word[:1].upper() + word[1:]
    return f"{start.day} {start.strftime('%b')} – {end.day} {end.strftime('%b')}"


def _period_en(start, end) -> str:
    if not start:
        return ""
    if start == end:
        word = tu.fmt_day(start)
        return word if word in ("today", "tomorrow", "yesterday") else f"on {word}"
    return f"from {start.day} {start.strftime('%b')} to {end.day} {end.strftime('%b')}"


def _period_hi(start, end) -> str:
    if not start:
        return ""
    today = tu.today_local()
    if start == end:
        if start == today:
            return "आज"
        if start == today + timedelta(days=1):
            return "कल"
        if start == today - timedelta(days=1):
            return "बीते कल"
        return f"{start.day} {_HI_MONTHS[start.month - 1]} को"
    return f"{start.day} {_HI_MONTHS[start.month - 1]} से {end.day} {_HI_MONTHS[end.month - 1]} तक"


def _join(*parts: str) -> str:
    return " ".join(p for p in parts if p)


def _names(names: list[str], limit: int = 3, hindi: bool = False) -> str:
    shown = names[:limit]
    more = len(names) - len(shown)
    joiner = " और " if hindi else " and "
    if more > 0:
        return ", ".join(shown) + (f" और {more} और लोग" if hindi else f" and {more} more")
    return ", ".join(shown[:-1]) + (joiner if len(shown) > 1 else "") + shown[-1] if shown else ""


def clean_missing(value) -> str:
    """A detail he asked for that the data does not hold ("gender"): letters only, short."""
    return " ".join(re.findall(r"[a-zA-Z]+", str(value or "")))[:30].lower()


# --------------------------------------------------------------------------- template: the people
def people_view(user, p: dict, missing: str = "", not_found: str = "", suggestions: list[str] | None = None) -> dict:
    """The staff directory as it is in the staff app: name, department, designation and leave, plus how much
    open work each person has. Grouped by department, with a head-count chart. One template for every question
    about the people ("list of my staff", "who is in Accounts", "who is on leave")."""
    # Everyone in the staff app except Sir's own entry (he is not his own staff). Mansi is staff too, so she is listed.
    links = dict(TaskPanelLink.objects.filter(active=True).values_list("actor_key", "user__role"))
    everyone = [s for s in StaffMember.objects.filter(active=True).order_by("department", "name")
                if links.get(s.external_id) != User.Role.MD]
    wanted = _match_department(p["group"]) if p["group"] else ""
    group_missed = bool(p["group"] and not wanted)
    staff = [s for s in everyone if not wanted or s.department == wanted]
    on_leave_only = p["filter"] == "on_leave"
    if on_leave_only:
        staff = [s for s in staff if s.on_leave]

    open_n: dict[int, int] = {}
    late_n: dict[int, int] = {}
    for t in services.delegated_qs().exclude(status__in=[Status.COMPLETED, Status.CANCELLED]):
        if t.staff_id:
            open_n[t.staff_id] = open_n.get(t.staff_id, 0) + 1
            if services.is_overdue(t):
                late_n[t.staff_id] = late_n.get(t.staff_id, 0) + 1
    # Mansi's work lives inside JARVIS (not the staff app): count it against her own entry.
    mansi = next((s for s in everyone if links.get(s.external_id) == User.Role.PA), None)
    if mansi:
        hers = services.pa_tasks_qs().exclude(origin=SharedTask.Origin.MD_MEET_PA).exclude(
            status__in=[Status.COMPLETED, Status.CANCELLED])
        open_n[mansi.id] = hers.count()
        late_n[mansi.id] = sum(1 for t in hers if services.is_overdue(t))

    rows = []
    for s in staff:
        leave = (f"On leave until {tu.fmt_day(s.leave_until)}" if s.leave_until else "On leave") if s.on_leave else ""
        rows.append({
            "id": str(s.id), "scope": "", "title": s.name, "who": s.department or "Other", "kind": s.designation,
            "status": leave, "when": "", "day": "", "priority": "NORMAL", "progress": 0, "private": False,
            "overdue": late_n.get(s.id, 0) > 0, "version": None, "hold_reason": "",
            "assigned": open_n.get(s.id, 0), "late": late_n.get(s.id, 0), "on_leave": s.on_leave,
        })
    depts: dict[str, int] = {}
    for s in staff:
        depts[s.department or "Other"] = depts.get(s.department or "Other", 0) + 1
    away = [s for s in staff if s.on_leave]
    busy = sum(1 for s in staff if open_n.get(s.id))
    late_total = sum(late_n.get(s.id, 0) for s in staff)

    title = "On leave" if on_leave_only else f"{wanted} team" if wanted else "Your staff" if user.is_md else "Staff"
    view = datacards._table(
        "people", title, rows,
        [("title", "Name"), ("who", "Department"), ("kind", "Designation"), ("status", "Leave"), ("assigned", "Open tasks"), ("late", "Late")],
        subtitle="Name, team, role and leave, as in the staff app", layout="people")
    if wanted or on_leave_only:
        view["stats"] = [
            {"label": "People", "value": len(staff), "tone": "accent", "icon": "people-outline"},
            {"label": "On leave", "value": len(away), "tone": "warn" if away else "muted", "icon": "airplane-outline"},
            {"label": "Open tasks", "value": sum(open_n.get(s.id, 0) for s in staff), "tone": "info", "icon": "briefcase-outline"},
            {"label": "Late", "value": late_total, "tone": "danger" if late_total else "muted", "icon": "alert-circle-outline"},
        ]
    else:
        view["stats"] = [
            {"label": "People", "value": len(staff), "tone": "accent", "icon": "people-outline"},
            {"label": "Teams", "value": len(depts), "tone": "info", "icon": "git-network-outline"},
            {"label": "On leave", "value": len(away), "tone": "warn" if away else "muted", "icon": "airplane-outline"},
            {"label": "With work", "value": busy, "tone": "success", "icon": "briefcase-outline"},
        ]
        view["series"] = [{"label": name, "date": name, "value": count, "minutes": 0, "heavy": False, "off": False, "today": False}
                          for name, count in sorted(depts.items(), key=lambda kv: (-kv[1], kv[0]))]

    # The sentence that is spoken (and its Hindi twin), from the real numbers.
    address = services.address_of(user)
    address_hi = services.address_hi_of(user)
    n, people = len(staff), "person" if len(staff) == 1 else "people"
    names = _names([s.name for s in staff])
    names_hi = _names([s.name for s in staff], hindi=True)
    if on_leave_only:
        en = "nobody is on leave right now." if not n else f"{n} {people} on leave: {names}."
        hi = "अभी कोई छुट्टी पर नहीं है।" if not n else f"{n} {'व्यक्ति' if n == 1 else 'लोग'} छुट्टी पर: {names_hi}।"
    elif wanted:
        en = f"{wanted} has {n} {people}: {names}."
        hi = f"{wanted} में {n} {'व्यक्ति है' if n == 1 else 'लोग हैं'}: {names_hi}।"
    else:
        own_word, own_hi = ("you have", "आपके") if user.is_md else ("there are", "कुल")
        en = f"{own_word} {n} staff in {len(depts)} teams" + (f"; {len(away)} on leave" if away else "") + "."
        hi = f"{own_hi} {len(depts)} टीम में {n} स्टाफ़ हैं" + (f"; {len(away)} छुट्टी पर" if away else "") + "।"
    lead_en = lead_hi = ""
    if missing:
        lead_en = f"the staff list has no {missing} detail, so here is everyone: "
        lead_hi = f"स्टाफ़ लिस्ट में {missing} की जानकारी नहीं है, इसलिए पूरी लिस्ट: "
    elif not_found:
        hint = f" Did you mean {suggestions[0]}?" if suggestions else ""
        lead_en = f"I couldn't find {not_found} on the staff list.{hint} Here is everyone: "
        lead_hi = f"स्टाफ़ लिस्ट में {not_found} नहीं मिला। पूरी लिस्ट: "
    elif group_missed:
        lead_en = f"I couldn't find a team called {p['group']}; here is everyone: "
        lead_hi = f"{p['group']} नाम की टीम नहीं मिली; पूरी लिस्ट: "
    view["summary"] = f"{address}, {lead_en}{en}"
    view["summary_hi"] = f"{address_hi}, {lead_hi}{hi}"
    return view


# --------------------------------------------------------------------------- template: anybody's tasks
def _profile(subject: dict) -> dict | None:
    """Who one person is, from the staff app: team, role, leave. None for anyone who is not a single person."""
    staff = subject.get("staff")
    if subject["type"] == "pa":
        pa_keys = TaskPanelLink.objects.filter(active=True, user__role=User.Role.PA).values_list("actor_key", flat=True)
        staff = StaffMember.objects.filter(active=True, external_id__in=list(pa_keys)).first()
        if staff is None:
            return {"name": subject["label"], "department": "", "designation": "Personal Assistant", "on_leave": False, "leave": ""}
    if staff is None:
        return None
    leave = (f"On leave until {tu.fmt_day(staff.leave_until)}" if staff.leave_until else "On leave") if staff.on_leave else ""
    return {"name": staff.name if subject["type"] != "pa" else subject["label"], "department": staff.department,
            "designation": staff.designation, "on_leave": staff.on_leave, "leave": leave}


def _phrases(p: dict, user, pa_name: str) -> dict:
    """The narrowing filters in words (English and Hindi): the type, who it came from, the priority, the title words."""
    types = p["types"]
    if len(types) == 1:
        one, many = _TYPE_EN[types[0]]
        type_hi = _TYPE_HI[types[0]]
    else:
        one, many, type_hi = "item", "items", "काम"
    from_en = from_hi = ""
    w = p["from_whom"]
    if w == "staff":
        from_en, from_hi = "with staff", "स्टाफ़ के"
    elif w and user.is_assistant:
        from_en, from_hi = (f"from {services.md_address()}", f"{services.md_address_hi()} के दिए हुए") if w == "sir" else ("of your own", "आपके अपने")
    elif w:
        from_en, from_hi = (f"from {pa_name}", f"{pa_name} के भेजे हुए") if w == "pa" else ("of your own", "आपके अपने")
    return {
        "one": one, "many": many, "type_hi": type_hi, "from_en": from_en, "from_hi": from_hi,
        "pr_en": {"urgent": "urgent ", "high": "high-priority ", "important": "important "}.get(p["priority"], ""),
        "pr_hi": {"urgent": "अर्जेंट ", "high": "ज़रूरी ", "important": "ज़रूरी "}.get(p["priority"], ""),
        "words_en": f"about {p['words']}" if p["words"] else "",
        "words_hi": f"{p['words']} वाले" if p["words"] else "",
    }


def _when_of(item: dict) -> tuple[datetime | None, date | None]:
    """(the moment, or None) and (the day, or None) an item is due."""
    when = item.get("scheduled_start") or item.get("due_at")
    if when:
        dt = datetime.fromisoformat(when)
        return dt, tu.to_local(dt).date()
    if item.get("due_date"):
        return None, date.fromisoformat(item["due_date"])
    return None, None


def _short_en(item: dict, one_day: bool) -> str:
    """'Call Mehta at 4 PM' (the day too when the list spans several days: 'Call Mehta tomorrow at 4 PM')."""
    dt, d = _when_of(item)
    day = "" if one_day or d is None else f" {tu.fmt_day(d)}"
    at = f" at {tu.fmt_time(dt)}" if dt else ""
    return f"{item['title']}{day}{at}"


def _short_hi(item: dict, one_day: bool) -> str:
    dt, d = _when_of(item)
    day = "" if one_day or d is None else f" {tu.fmt_day_hi(d)}"
    at = f" {tu.fmt_time_hi(dt)}" if dt else ""
    return f"{item['title']}{day}{at}"


def _length_minutes(item: dict) -> int:
    start, end = item.get("scheduled_start"), item.get("scheduled_end")
    if start and end:
        return max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() // 60))
    return int(item.get("estimated_minutes") or 0)


def _detail_en(item: dict, one_day: bool) -> str:
    """One item in full: 'Weekly Review at 4 PM, 60 minutes, urgent, in progress'."""
    bits = [_short_en(item, one_day)]
    mins = _length_minutes(item)
    if item.get("kind") in ("MEETING", "CALL") and mins:
        bits.append(f"{mins} minutes")
    if item.get("priority") == "URGENT":
        bits.append("urgent")
    elif item.get("priority") == "HIGH":
        bits.append("high priority")
    if _is_done(item):
        bits.append("done")
    elif item.get("overdue"):
        bits.append("late")
    elif item.get("status") == Status.IN_PROGRESS:
        bits.append("in progress")
    return ", ".join(bits)


def _detail_hi(item: dict, one_day: bool) -> str:
    bits = [_short_hi(item, one_day)]
    mins = _length_minutes(item)
    if item.get("kind") in ("MEETING", "CALL") and mins:
        bits.append(f"{mins} मिनट")
    if item.get("priority") == "URGENT":
        bits.append("अर्जेंट")
    elif item.get("priority") == "HIGH":
        bits.append("ज़रूरी")
    if _is_done(item):
        bits.append("हो गया")
    elif item.get("overdue"):
        bits.append("लेट")
    elif item.get("status") == Status.IN_PROGRESS:
        bits.append("चल रहा है")
    return ", ".join(bits)


def _next_after(everything: list[dict], p: dict, user, end: date | None) -> dict | None:
    """The next open item matching the same filters that is due after the period asked about (or None)."""
    loose = {**p, "ids": []}
    pool = [i for i in everything if not _is_done(i) and _keep(i, loose, user)]
    if p["words"]:
        pool = _by_words(pool, p["words"])
    future = []
    for item in pool:
        moment, d = _when_of(item)
        if d is not None and (end is None or d > end):
            future.append((moment or tu.combine_local(d, time(23, 59)), item))
    future.sort(key=lambda pair: pair[0])
    return future[0][1] if future else None


def tasks_view(user, p: dict, include_private: bool = True, mode: str = "", missing: str = "") -> dict:
    """Any list of tasks: whose (me / PA / Sir / one staff member / a team / all staff / everyone), for which day
    or period (due, given or finished then), and which ones (open / done / late / personal / official).

    mode "performance": the same template as one person's report for the last 30 days (or the period said).
    mode "person": the same template as one person's whole picture: who they are (team, role, leave), what is
    on their plate now and what they finished lately."""
    subject = resolve_subject(user, p["who"])
    profile = _profile(subject) if mode == "person" else None
    if mode == "person" and profile is None and subject["type"] not in ("ambiguous", "unknown"):
        mode = ""  # "details" of a team or of everyone is simply their list
    performance = mode == "performance"
    address = services.address_of(user)
    address_hi = services.address_hi_of(user)
    sir, sir_hi = services.md_address(), services.md_address_hi()
    if subject["type"] == "ambiguous":
        return {"ask": f"{address}, which {subject['label']}: {' or '.join(subject['options'])}?",
                "ask_hi": f"{address_hi}, कौन से {subject['label']}: {' या '.join(subject['options'])}?"}
    if subject["type"] == "unknown":
        return people_view(user, clean_params({}), not_found=subject["label"], suggestions=subject.get("suggestions"))

    today = tu.today_local()
    start, end, by, flt = p["from"], p["to"], p["by"], p["filter"]
    if performance and not start:
        start, end, by = today - timedelta(days=29), today, by or "given"
    kind = subject["type"]
    private_ok = include_private and user.is_md and kind in ("me", "all")
    everything = _collect(user, subject, include_private)
    items = [i for i in everything if _in_period(i, start, end, by, today) and _keep(i, p, user)]
    if p["words"]:
        items = _by_words(items, p["words"])
    items = _ordered(items)
    many = kind in ("staff_all", "all", "department")
    pa = services.get_pa()
    pa_name = pa.spoken_name if pa else "your PA"
    ph = _phrases(p, user, pa_name)

    rows = []
    for item in items[:_ROWS]:
        row = datacards._row(item, True)
        row["who"] = _owner(item, user) if many else ""
        row["day"] = row["day"] or "No date"
        if _is_done(item):
            row["status"], row["progress"] = "Done", 100
        rows.append(row)
    total = len(items)
    done = sum(1 for i in items if _is_done(i))
    late = sum(1 for i in items if i.get("overdue") or (performance and _finished_late(i)))
    still = total - done
    on_time = sum(1 for i in items if _is_done(i) and not _finished_late(i))
    on_time_pct = round(on_time / done * 100) if done else 0

    mine = f"My {ph['many']}" if p["types"] else "My tasks"
    who_title = {"me": mine, "pa": f"{subject['label']}'s tasks", "md": f"{sir}'s official tasks",
                 "staff_one": subject["label"], "department": f"{subject['label']} team",
                 "staff_all": "Work with staff", "all": "Everyone's tasks"}[kind]
    period = _period_title(start, end)
    title = _join(who_title, f"· {period}" if period else "")
    basis = {"given": "Given in this period", "done": "Finished in this period", "due": "Due in this period",
             "": "Due or finished then" if start else "Open, and finished in the last 30 days"}[by]
    which = {"open": "open only", "done": "finished only", "late": "late only", "personal": "personal only",
             "official": "official only"}.get(flt, "")
    narrow = [f"{ph['many']} only" if p["types"] else "", ph["from_en"], ph["pr_en"].strip(), ph["words_en"]]
    subtitle = _join(basis, f"· {which}" if which else "", *(f"· {n}" for n in narrow if n))
    if kind == "me" and user.is_md and not private_ok and flt != "official":
        subtitle = _join(subtitle, "· personal items hidden while locked")
    view = datacards._table(
        "tasks", title, rows,
        [("day", "Day"), ("when", "When"), ("title", "What"), ("who", "Who"), ("status", "Status")],
        subtitle=subtitle, layout="table" if kind in ("me", "md") else "progress")
    if performance:
        view["stats"] = [
            {"label": "Given", "value": total, "tone": "accent", "icon": "briefcase-outline"},
            {"label": "Done", "value": done, "tone": "success", "icon": "checkmark-done-outline"},
            {"label": "Late", "value": late, "tone": "danger" if late else "muted", "icon": "alert-circle-outline"},
            {"label": "On time", "value": f"{on_time_pct}%", "tone": "info", "icon": "time-outline"},
        ]
    else:
        view["stats"] = [
            {"label": "Total", "value": total, "tone": "accent", "icon": "list-outline"},
            {"label": "Open", "value": still, "tone": "info", "icon": "ellipse-outline"},
            {"label": "Done", "value": done, "tone": "success", "icon": "checkmark-done-outline"},
            {"label": "Late", "value": late, "tone": "danger" if late else "muted", "icon": "alert-circle-outline"},
        ]
    view["series"] = _series(items, start, end, by, today, weekly=performance)
    if profile:
        view["kind"] = "person"
        view["profile"] = profile
        view["title"] = _join(profile["name"], f"· {period}" if period else "")
        role_line = " · ".join(x for x in (profile["designation"], profile["department"]) if x)
        view["subtitle"] = _join(role_line, f"· {profile['leave']}" if profile["leave"] and role_line else profile["leave"])
        view["stats"] = [
            {"label": "Open", "value": still, "tone": "accent", "icon": "ellipse-outline"},
            {"label": "Done", "value": done, "tone": "success", "icon": "checkmark-done-outline"},
            {"label": "Late", "value": late, "tone": "danger" if late else "muted", "icon": "alert-circle-outline"},
            {"label": "On time", "value": f"{on_time_pct}%" if done else "–", "tone": "info", "icon": "time-outline"},
        ]

    # The sentence that is spoken, from the real numbers.
    past = bool(end and end < today)
    one_day = bool(start and start == end)
    count_word = "one" if total == 1 else str(total)
    things = ph["one"] if total == 1 else ph["many"]
    things_hi = ph["type_hi"]
    # "open" is what "you have" already means: it is not said ("one urgent item", not "one urgent open item").
    adj = {"done": "finished ", "late": "late ", "personal": "personal ", "official": "official "}.get(flt, "")
    adj_hi = {"done": "पूरे हुए ", "late": "लेट ", "personal": "पर्सनल ", "official": "ऑफ़िशियल "}.get(flt, "")
    adj, adj_hi = ph["pr_en"] + adj, ph["pr_hi"] + adj_hi
    if kind == "md" and not adj and not p["types"]:
        # Mansi asking about Sir sees only his official work: say so, so "nothing" is never read as "he is free".
        adj, adj_hi = "official ", "ऑफ़िशियल "
    tail_en, tail_hi = _join(ph["from_en"], ph["words_en"]), _join(ph["from_hi"], ph["words_hi"])
    name = subject["label"]
    period_en, period_hi = _period_en(start, end), _period_hi(start, end)
    if performance:
        period_en = period_en if p["from"] else "in the last 30 days"
        period_hi = period_hi if p["from"] else "पिछले 30 दिन में"
    has = {"me": "you had" if past else "you have", "pa": f"{name} {'had' if past else 'has'}",
           "md": f"{sir} {'had' if past else 'has'}", "staff_one": f"{name} {'had' if past else 'has'}",
           "department": f"the {name} team {'had' if past else 'has'}", "staff_all": f"the staff {'had' if past else 'have'}",
           "all": "there were" if past else "there are"}[kind]
    whose_hi = {"me": "आपके", "pa": f"{name} के", "md": f"{sir_hi} के", "staff_one": f"{name} के", "department": f"{name} टीम के",
                "staff_all": "स्टाफ़ के", "all": "सबके"}[kind]
    to_hi = {"me": "आपको", "pa": f"{name} को", "md": f"{sir_hi} को", "staff_one": f"{name} को", "department": f"{name} टीम को",
             "staff_all": "स्टाफ़ को", "all": "कुल"}[kind]
    by_hi = {"me": "आपने", "pa": f"{name} ने", "md": f"{sir_hi} ने", "staff_one": f"{name} ने", "department": f"{name} टीम ने",
             "staff_all": "स्टाफ़ ने", "all": "सबने"}[kind]
    if not total:
        target = "on your list" if kind == "me" else "for everyone" if kind == "all" else f"for {name}"
        en = _join(f"I found no {adj}{ph['many']}", tail_en, target, period_en) + "."
        hi = _join(whose_hi, period_hi, tail_hi, f"कोई {adj_hi}{things_hi} नहीं मिला।")
        # Nothing in that period, but something later: say when, so "nothing today" never hides tomorrow's.
        if start and not past and not performance and flt not in ("done", "late") and by in ("", "due"):
            nxt = _next_after(everything, p, user, end)
            if nxt:
                en += f" The next one is {_short_en(nxt, False)}."
                hi += f" अगला {_short_hi(nxt, False)} है।"
    else:
        was = "was" if total == 1 else "were"
        if by == "given":
            lead = {"me": f"{count_word} {adj}{things} {was} added to your list", "all": f"{count_word} {adj}{things} {was} given out",
                    "staff_all": f"the staff were given {count_word} {adj}{things}",
                    "department": f"the {name} team was given {count_word} {adj}{things}"}.get(
                        kind, f"{name} was given {count_word} {adj}{things}")
            lead_hi = _join(period_hi, to_hi, tail_hi, f"{total} {adj_hi}{things_hi} दिए गए")
        elif by == "done":
            lead = {"me": f"you finished {count_word} {things}", "all": f"{count_word} {things} {was} finished",
                    "staff_all": f"the staff finished {count_word} {things}", "department": f"the {name} team finished {count_word} {things}"}.get(
                        kind, f"{name} finished {count_word} {things}")
            lead_hi = _join(by_hi, period_hi, tail_hi, f"{total} {things_hi} पूरे किए")
        else:
            lead = f"{has} {count_word} {adj}{things}"
            lead_hi = _join(period_hi, whose_hi, tail_hi, f"{total} {adj_hi}{things_hi} {'थे' if past else 'हैं'}")
            if total == 1:
                lead_hi = _join(period_hi, whose_hi, tail_hi, f"एक {adj_hi}{things_hi} है")
        en = _join(lead, tail_en, period_en)
        hi = lead_hi
        if total == 1 and not performance:
            # One item: say it in full, so he gets the thing itself, not a count.
            en += f": {_detail_en(items[0], one_day)}"
            hi += f": {_detail_hi(items[0], one_day)}"
        elif total <= 3 and by != "done" and not performance:
            # A few: name them all, with their times.
            en += ": " + ", ".join(_short_en(i, one_day) for i in items)
            hi += ": " + ", ".join(_short_hi(i, one_day) for i in items)
            if done:
                en += f"; {done} done"
                hi += f"; {done} पूरे"
        elif by != "done" and flt in ("", "open", "personal", "official"):
            en += f": {done} done, {still} open" + (f", {late} late" if late else "")
            hi += f": {done} पूरे, {still} बाकी" + (f", {late} लेट" if late else "")
            if not performance:
                first = next((i for i in items if not _is_done(i)), items[0])
                en += f". First: {_short_en(first, one_day)}"
                hi += f"। सबसे पहले {_short_hi(first, one_day)}"
        if performance and done:
            en += f"; {on_time_pct}% on time"
            hi += f"; {on_time_pct}% समय पर"
        en += "."
        hi += "।"
    if profile:
        # One person's whole picture: who they are first, then the work, in one breath.
        role = _join(profile["designation"], f"in {profile['department']}" if profile["department"] else "")
        intro = f"{profile['name']} is {role}" if role else profile["name"]
        role_hi = _join(f"{profile['department']} टीम में" if profile["department"] else "", profile["designation"])
        intro_hi = f"{profile['name']} {role_hi} हैं" if role_hi else profile["name"]
        if profile["leave"]:
            intro += f", {profile['leave'][:1].lower()}{profile['leave'][1:]}"
            intro_hi += ", अभी छुट्टी पर"
        work = f"{still} open" + (f", {late} late" if late else "") + f"; {done} done" + (f", {on_time_pct}% on time" if done else "")
        work_hi = f"{still} काम बाकी" + (f", {late} लेट" if late else "") + f"; {done} पूरे" + (f", {on_time_pct}% समय पर" if done else "")
        en = f"{intro}. " + _join(f"{period_en[:1].upper()}{period_en[1:]}:" if period_en else "", work) + "."
        hi = f"{intro_hi}। " + _join(f"{period_hi}:" if period_hi else "", work_hi) + "।"
    if missing:
        en += f" I don't have {missing} in my data."
        hi += f" {missing} की जानकारी मेरे पास नहीं है।"
    view["summary"] = f"{address}, {en}"
    view["summary_hi"] = f"{address_hi}, {hi}"
    return view


def _series(items: list[dict], start, end, by: str, today: date, weekly: bool = False) -> list[dict]:
    """The bar chart under the numbers: one bar a day for a short period, one a week for a report."""
    if not start or start == end:
        return []

    def day_of(item):
        if by == "given":
            return _iso_day(item.get("created_at"))
        if by == "done" or (weekly and _is_done(item)):
            return _iso_day(item.get("completed_at"))
        return _due_day(item) or (_iso_day(item.get("completed_at")) if _is_done(item) else None)

    span = (end - start).days + 1
    if weekly or span > 14:
        if span > 120:
            return []
        buckets, cursor = [], start
        while cursor <= end:
            last = min(end, cursor + timedelta(days=6))
            chosen = [i for i in items if (lambda d: d is not None and cursor <= d <= last)(day_of(i))]
            if weekly:
                chosen = [i for i in chosen if _is_done(i)]
            buckets.append({"label": f"{cursor.day} {cursor.strftime('%b')}", "date": cursor.isoformat(), "value": len(chosen),
                            "minutes": 0, "heavy": False, "off": False, "today": cursor <= today <= last})
            cursor = last + timedelta(days=1)
        return buckets
    out = []
    for n in range(span):
        d = start + timedelta(days=n)
        out.append({"label": d.strftime("%a") if span <= 7 else str(d.day), "date": d.isoformat(),
                    "value": sum(1 for i in items if day_of(i) == d), "minutes": 0, "heavy": False, "off": False,
                    "today": d == today})
    return out
