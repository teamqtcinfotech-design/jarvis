"""The answer path (24 Nov 2026): a question about anything the app knows is answered in ONE exact sentence.

How it works: the strongest model gets the question with the whole picture (the clock, the last turns, the item
JARVIS last spoke about) and a set of read-only lookups it can call on the server, as many as it needs in one go:
the diary for a day, what is next, free time, a person from everywhere, what is waiting, one item, a search by
words, a panel report, the counts. The server fetches the facts and does every count and every bit of clock maths;
the model reads the facts and the question and answers only what was asked, then names the items it spoke about,
which become the cursor for "isko", "iske baad", "isse pehle". Nothing here changes data.

When the model cannot be reached, the caller falls back to the fixed templates (core.datacards), so a question
is never left unanswered.
"""
import json
import logging
import re
from datetime import date, datetime, timedelta

import anthropic
from django.conf import settings
from django.utils import timezone

from . import ai_brain, ai_usage, datacards, scheduling, services
from . import timeutils as tu
from .models import AiUsage, Alert, Contact, Memory, StaffMember, Status

log = logging.getLogger("jarvis.ask")

MAX_ROUNDS = 6            # lookups the model may chain for one question (several per round)
TOOL_RESULT_CHARS = 12000  # one lookup's facts, cut (the model asks again, narrower, if it needs more)
# "Dikhao", "list", "table": he wants the card, which the fixed templates draw; the answer path is for questions.
LIST_WORDS = re.compile(r"\b(dikhao|dikha do|dikhaao|dikhana|list|table|chart|graph|sab batao|sabhi batao|show me (?:the |all )?(?:list|table))\b"
                        r"|दिखाओ|लिस्ट|टेबल", re.IGNORECASE)

ASK_SYSTEM_PROMPT = """You are JARVIS, the personal assistant of {reader} (addressed as "{address}", in Hindi "{address_hi}"). The speaker asks about their own work, diary, team, messages, alerts, memories and the company panel. You answer from FACTS ONLY, which you fetch with the tools: call as many as the question needs, several in one go when they do not depend on each other. Never answer from memory or guess.

THE ANSWER
- ONE sentence (two at most) that says exactly what was asked, nothing more: the name of the one item when one item is asked ("meri agli meeting kaunsi hai" -> "Sharma ji ke saath, kal 10:30 baje."), the number alone when a number is asked ("kitni meetings" -> "Teen."), yes or no FIRST when it is a yes-or-no question, followed by at most ONE nearby fact, the next thing after that time, never what came before it ("12 baje koi meeting hai?" -> "Nahi, 12 baje kuch nahi hai; agli 4 baje Mehta ji ke saath hai."). Short beats complete: the speaker asks again if they want more.
- Never a list unless the speaker asked for one ("sab batao", "kaun kaun se"); then at most five, in one breath.
- "Next" / "agla" = the first item after NOW in time order. "Iske baad" / "isse pehle" / "isko" / "ye" / "wo" = relative to last_list.items and last_list.cursor in the context (the item JARVIS last spoke about); use the item tool on that id when you need its details.
- Numbers and times come from the tools: never count items yourself, never add up minutes yourself, never work out "how long till" yourself; the tools give counts, minutes and "in N minutes". Totals are in the diary already added up (meeting_time, task_time, free_time): "kitna time meetings mein jaayega" is answered with that total in hours and minutes ("Kal 3 ghante 30 minute meetings mein jaayenge."), never item by item. Every item carries its minutes; never say an item has no time when the facts give a start or minutes.
- "Pehla" / "doosra" / "teesra" / "aakhri" = that place in last_list.items (the list of the last answer, in the order it was given). When your answer counts several things (messages, tasks, approvals), put ALL their ids in "about" in the order you would read them, so the next "pehla kya hai" or "doosra padho" points at the right one.
- When the facts do not contain the answer, say exactly what is missing, in the same specific words ("12 baje ki koi meeting nahi hai.", "Mansi naam ka koi staff ya team member nahi mila.", "Himanshu ki taraf se aaj kuch nahi aaya."). Never say "samajh nahi aaya", never ask the speaker to repeat.
- Language: say_en follows the speaker's own way (English when they wrote English, Hinglish in Roman letters when they spoke Hinglish). say_hi is the same sentence in Devanagari for the Hindi voice. Keep names and titles as they are.
- The address ("{address}") at most once, and only in a question or a greeting; never "Ji Sir", never "Okay Sir", never filler.
- A person: who they are (job, department), then only what was asked (their open work, what they sent, leave). Everything a tool returns about them is allowed; nothing is private except the speaker's own personal items, which the tools already handle.

OUTPUT
After the tools, reply with ONLY a JSON object, no other text:
{{"say_en": "...", "say_hi": "...", "about": ["id", ...], "show": "", "show_params": {{}}}}
- about: the ids of the items you spoke about, in the order spoken; the first one is what "isko" / "iske baad" will mean next. Empty when you spoke about none.
- show: a card kind ONLY when the speaker asked to see something laid out (today, upcoming, tasks, late, pending, alerts, people, person, panel, messages); otherwise "". show_params: the filters for it ({{"who": "Nikki"}}, {{"types": ["MEETING"]}}, {{"group": "partners", "words": "Pune"}}), else {{}}.
"""

TOOLS = [
    {"name": "diary", "description": "The speaker's own diary for one day or a few days from one: every item with its time, kind, status and who it is for, the free windows inside office hours, and (for today) the current time. Use for 'what is at 10', 'am I free at 4', 'kal kya hai', 'is hafte'.",
     "input_schema": {"type": "object", "properties": {
         "day": {"type": "string", "description": "today, tomorrow, day after, yesterday, a weekday name, or YYYY-MM-DD"},
         "days": {"type": "integer", "description": "how many days from that day (1-7), default 1"}}, "required": ["day"]}},
    {"name": "next_up", "description": "What is next by the clock from now: the first N timed items ahead (today and later), each with 'in_minutes'. Use for 'agli meeting', 'next kya hai', 'uske baad kya'.",
     "input_schema": {"type": "object", "properties": {
         "count": {"type": "integer", "description": "1-5, default 1"},
         "kinds": {"type": "array", "items": {"type": "string", "enum": ["MEETING", "CALL", "TASK", "PAYMENT", "FOLLOW_UP", "APPROVAL"]}, "description": "only these kinds; empty = everything timed"},
         "after_id": {"type": "string", "description": "start after this item instead of now (for 'iske baad kya')"}}}},
    {"name": "person", "description": "Everything known about one person on the team or in the staff directory: who they are, their open work (count, late count, the first few), what they sent the speaker lately, leave, and anything the speaker told JARVIS to remember about them.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "waiting", "description": "What is waiting for the speaker (alerts): messages, tasks given, meetings set, approvals, requests, updates; narrowed by kind, sender, words, state (unseen / seen / waiting / approved / declined) or a span (minutes, or a day). With nothing about when: everything still waiting from recent days.",
     "input_schema": {"type": "object", "properties": {
         "types": {"type": "array", "items": {"type": "string", "enum": ["MESSAGE", "TASK", "MEETING", "APPROVAL", "REQUEST", "UPDATE"]}},
         "who": {"type": "string", "description": "the sender's name"},
         "words": {"type": "string", "description": "words from the title or message"},
         "state": {"type": "string", "enum": ["", "unseen", "seen", "waiting", "approved", "declined"]},
         "minutes": {"type": "integer", "description": "the last N minutes"},
         "day": {"type": "string", "description": "today, yesterday or YYYY-MM-DD"}}}},
    {"name": "item", "description": "One item in full by its id: title, kind, when, who gave it, who it is for, notes, steps, status, promise.",
     "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "search", "description": "Find by words across everything: tasks and meetings (any list the speaker may see), alerts and messages of the last 30 days, contacts, memories, the team and the staff directory.",
     "input_schema": {"type": "object", "properties": {"words": {"type": "string"}}, "required": ["words"]}},
    {"name": "panel", "description": "The company panel's records (MD only, view only): partners, partner_directory, partner_pipeline, partner_ledger, franchises, franchise_requests, franchise_onboarding, franchise_payments, franchise_targets, franchise_pipeline, franchise_commissions, payments, withdrawal_payouts, commissions, agreements, tds_sheet, itr_statistics; narrowed by words (a name, an ID, a city) and a period. With filters or count_only the PANEL COUNTS for you ('kitne partners inactive hain' = report partners, filters {account_status: inactive}) and returns the total: never count rows yourself (a page holds 20 of thousands). Accepted so far: partners account_status active|inactive, payment_status active|pending|closed|none; franchises account_status active|inactive, agreement_status pending, login_status never. KYC and documents are never served by the panel: say so plainly when asked.",
     "input_schema": {"type": "object", "properties": {
         "report": {"type": "string"}, "words": {"type": "string"},
         "from": {"type": "string", "description": "YYYY-MM-DD"}, "to": {"type": "string", "description": "YYYY-MM-DD"},
         "count_only": {"type": "boolean", "description": "true = only the total under the filters, no rows"},
         "filters": {"type": "object", "description": "the panel's own filters: account_status, payment_status, agreement_status, login_status, email_status, kyc_status, record_status", "additionalProperties": {"type": "string"}}},
         "required": ["report"]}},
    {"name": "counts", "description": "How much is open on the speaker's list (total, late, due today, later or undated; official and personal for the MD), what they are waiting on from others, and (MD) how much open work each team member holds.",
     "input_schema": {"type": "object", "properties": {}}},
]

_DAY_WORDS = {"today": 0, "aaj": 0, "tomorrow": 1, "kal": 1, "day after": 2, "day after tomorrow": 2, "parso": 2,
              "yesterday": -1}
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _day_of(word: str, today: date) -> date | None:
    low = " ".join((word or "today").lower().split())
    if low in _DAY_WORDS:
        return today + timedelta(days=_DAY_WORDS[low])
    if low in _WEEKDAYS:
        ahead = (_WEEKDAYS.index(low) - today.weekday()) % 7
        return today + timedelta(days=ahead)
    try:
        return date.fromisoformat(low[:10])
    except ValueError:
        return None


def _local(iso: str | None):
    if not iso:
        return None
    try:
        return tu.to_local(datetime.fromisoformat(iso))
    except (TypeError, ValueError):
        return None


class _Facts:
    """The speaker's data, fetched once per question and read by every lookup."""

    def __init__(self, user, include_private: bool):
        self.user = user
        self.include_private = include_private
        self.now = tu.now_local()
        self.today = self.now.date()
        self._dash = None
        self.known: dict[str, dict] = {}   # id -> item, for the cursor
        self.person_named = ""             # the person a lookup was about (for "she" / "uska" next)

    @property
    def dash(self) -> dict:
        if self._dash is None:
            self._dash = services.dashboard(self.user)
        return self._dash

    def keep(self, item: dict) -> bool:
        return self.include_private or item.get("scope") != "private"

    def own(self) -> list[dict]:
        d = self.dash
        return [i for i in d["overdue"] + d["today"] + d["upcoming"] + d["anytime"] if self.keep(i)]

    def everything(self) -> list[dict]:
        d = self.dash
        seen, out = set(), []
        for i in self.own() + list(d.get("given_tasks") or []) + list(d.get("delegated") or []) + list(d.get("team_tasks") or []) \
                + list(d.get("pa_tasks") or []) + list(d.get("mg_tasks") or []) + list(d.get("md_tasks") or []):
            key = (i.get("scope"), i.get("id"))
            if key in seen or not self.keep(i):
                continue
            seen.add(key)
            out.append(i)
        return out

    def brief(self, i: dict) -> dict:
        start = _local(i.get("scheduled_start") or i.get("due_at"))
        out = {"id": i["id"], "title": i["title"], "kind": i["kind"], "priority": i["priority"], "status": i["status"],
               "when": i.get("due_label") or "", "overdue": bool(i.get("overdue"))}
        if start:
            out["start"] = start.strftime("%Y-%m-%d %H:%M")
            out["in_minutes"] = int((start - self.now).total_seconds() // 60)
        end = _local(i.get("scheduled_end"))
        if end:
            out["end"] = end.strftime("%H:%M")
        # Every item has its minutes (its own estimate, else its kind's usual length), so totals never need a guess.
        if end and start:
            out["minutes"] = max(0, int((end - start).total_seconds() // 60))
        else:
            out["minutes"] = scheduling.minutes_for(i.get("kind"), i.get("estimated_minutes"))
        a = i.get("assignee") or {}
        if a.get("staff_name"):
            out["for"] = a["staff_name"]
        if i.get("created_by"):
            out["from"] = i["created_by"]
        if i.get("amount_label"):
            out["amount"] = i["amount_label"]
        if i.get("scope") == "private":
            out["personal"] = True
        self.known[str(i["id"])] = i
        return out


# ------------------------------------------------------------------ the lookups
def _diary(f: _Facts, a: dict) -> dict:
    day = _day_of(str(a.get("day") or "today"), f.today)
    if day is None:
        return {"error": "day not understood"}
    days = max(1, min(int(a.get("days") or 1), 7))
    prefs = services.get_prefs(f.user)
    out = {"now": f.now.strftime("%Y-%m-%d %H:%M"), "days": []}
    for n in range(days):
        d = day + timedelta(days=n)
        items = []
        for i in f.own():
            start = _local(i.get("scheduled_start") or i.get("due_at"))
            on = start.date() if start else (date.fromisoformat(i["due_date"]) if i.get("due_date") else None)
            if on == d or (d == f.today and i.get("overdue")):
                items.append(f.brief(i))
        items.sort(key=lambda b: (b.get("start") or "9999", b["title"]))
        office_start, office_end = scheduling.office_hours(prefs, d)
        free = []
        if not scheduling.is_off_day(prefs, d):
            cursor = max(office_start, f.now) if d == f.today else office_start
            for span in scheduling.busy_on(f.user, d, prefs):
                if span["start"] > cursor + timedelta(minutes=15):
                    free.append({"from": cursor.strftime("%H:%M"), "to": span["start"].strftime("%H:%M"),
                                 "minutes": int((span["start"] - cursor).total_seconds() // 60)})
                cursor = max(cursor, span["end"])
            if office_end > cursor + timedelta(minutes=15):
                free.append({"from": cursor.strftime("%H:%M"), "to": office_end.strftime("%H:%M"),
                             "minutes": int((office_end - cursor).total_seconds() // 60)})
        meeting_minutes = sum(b["minutes"] for b in items if b["kind"] in ("MEETING", "CALL"))
        task_minutes = sum(b["minutes"] for b in items if b["kind"] not in ("MEETING", "CALL"))
        out["days"].append({"day": d.isoformat(), "weekday": d.strftime("%A"), "items": items, "count": len(items),
                            "meetings": sum(1 for b in items if b["kind"] in ("MEETING", "CALL")),
                            "tasks": sum(1 for b in items if b["kind"] not in ("MEETING", "CALL")),
                            "timed": sum(1 for b in items if b.get("start")), "untimed": sum(1 for b in items if not b.get("start")),
                            # The totals, already added up: "kitna time meetings mein jaayega" is read, never computed.
                            "meeting_minutes": meeting_minutes, "meeting_time": _hours(meeting_minutes),
                            "task_minutes": task_minutes, "task_time": _hours(task_minutes),
                            "office_hours": f"{office_start:%H:%M}-{office_end:%H:%M}", "free": free,
                            "free_minutes": sum(w["minutes"] for w in free), "free_time": _hours(sum(w["minutes"] for w in free)),
                            "off_day": scheduling.is_off_day(prefs, d)})
    return out


def _hours(minutes: int) -> str:
    """"3 h 30 min" / "45 min" / "0 min": the way a total is said."""
    h, m = divmod(max(0, int(minutes)), 60)
    return f"{h} h {m} min" if h and m else f"{h} h" if h else f"{m} min"


def _next_up(f: _Facts, a: dict) -> dict:
    count = max(1, min(int(a.get("count") or 1), 5))
    kinds = {k for k in (a.get("kinds") or []) if isinstance(k, str)}
    timed = []
    for i in f.own():
        start = _local(i.get("scheduled_start") or i.get("due_at"))
        if start is None or i["status"] in (Status.COMPLETED, Status.CANCELLED):
            continue
        if kinds and i["kind"] not in kinds:
            continue
        timed.append((start, i))
    timed.sort(key=lambda p: p[0])
    floor = f.now - timedelta(minutes=5)
    after = str(a.get("after_id") or "")
    if after:
        at = next((s for s, i in timed if str(i["id"]) == after), None)
        if at is not None:
            floor = at + timedelta(seconds=1)
    ahead = [f.brief(i) for s, i in timed if s >= floor][:count]
    return {"now": f.now.strftime("%Y-%m-%d %H:%M"), "items": ahead, "count": len(ahead)}


def _person(f: _Facts, a: dict) -> dict:
    name = " ".join(str(a.get("name") or "").split())
    if not name:
        return {"error": "no name"}
    out: dict = {"asked": name, "found": []}
    found = services.find_people(name, exclude=None)
    for u in found.get("people") or []:
        work = [i for i in f.everything() if (i.get("assignee") or {}).get("user_id") == u.id and i["status"] not in (Status.COMPLETED, Status.CANCELLED)]
        work.sort(key=lambda i: (not i.get("overdue"), i.get("due_at") or i.get("due_date") or "9999"))
        since = timezone.now() - timedelta(days=7)
        sent = list(Alert.objects.filter(recipient=f.user, sender=u, created_at__gte=since).order_by("-created_at")[:5])
        unseen = Alert.objects.filter(recipient=f.user, sender=u, acknowledged_at__isnull=True, seen_at__isnull=True).count()
        entry = {"name": u.spoken_name, "role": services.role_label(u), "title": u.title_label, "department": u.department or "",
                 "open_work": len(work), "late_work": sum(1 for i in work if i.get("overdue")),
                 "work": [f.brief(i) for i in work[:6]],
                 "sent_to_speaker_last_7_days": len(sent), "unseen_from_them": unseen,
                 "latest_from_them": [{"what": al.title, "when": tu.to_local(al.created_at).strftime("%Y-%m-%d %H:%M")} for al in sent[:3]]}
        if getattr(u, "on_leave", None):
            entry["on_leave"] = True
        out["found"].append(entry)
        f.person_named = u.spoken_name
    for s in StaffMember.objects.filter(active=True, name__icontains=name.split()[0])[:5]:
        given = [i for i in f.dash.get("delegated") or [] if (i.get("assignee") or {}).get("staff_id") == s.id and i["status"] not in (Status.COMPLETED, Status.CANCELLED)]
        out["found"].append({"name": s.name, "where": "staff directory (company panel)", "department": s.department,
                             "designation": s.designation or s.level, "on_leave": s.on_leave,
                             "leave_until": s.leave_until.isoformat() if s.leave_until else "",
                             "open_tasks_in_panel": s.open_tasks, "given_by_speaker_open": len(given),
                             "given": [f.brief(i) for i in given[:5]]})
        f.person_named = f.person_named or s.name
    out["contacts"] = [{"name": c.name, "who": c.note} for c in Contact.objects.filter(owner=f.user, name__icontains=name.split()[0])[:3]]
    out["memories"] = [m.text for m in Memory.objects.filter(owner=f.user, text__icontains=name.split()[0])[:5]]
    if not out["found"] and not out["contacts"] and not out["memories"]:
        out["note"] = "nobody by that name on the team, in the staff directory, in contacts or in memories"
    return out


def _waiting(f: _Facts, a: dict) -> dict:
    params: dict = {}
    types = [t for t in (a.get("types") or []) if isinstance(t, str)]
    if types:
        params["types"] = types
    if a.get("who"):
        params["who"] = str(a["who"])
    if a.get("words"):
        params["words"] = str(a["words"])
    if a.get("state"):
        params["filter"] = str(a["state"])
    if a.get("minutes"):
        params["minutes"] = int(a["minutes"])
    if a.get("day"):
        d = _day_of(str(a["day"]), f.today)
        if d:
            params["from"] = params["to"] = d.isoformat()
    view = datacards.build(f.user, "alerts", f.include_private, params=params) or {}
    rows = view.get("rows") or []
    items = []
    for n, r in enumerate(rows[:30]):
        rid = str(r.get("id") or f"alert-{n}")
        # Kept as something the cursor can point at: "pehla kya hai", "doosra padho" after a count of messages.
        f.known[rid] = {"id": rid, "title": r.get("title") or "", "scope": "alert", "kind": r.get("kind")}
        items.append({"id": rid, "what": r.get("kind"), "title": r.get("title"), "from": r.get("who"),
                      "state": r.get("status"), "when": f"{r.get('day')} {r.get('when')}".strip(), "amount": r.get("amount") or ""})
    return {"count": len(rows), "by_kind": {s["label"]: s["value"] for s in view.get("stats") or []},
            "items": items, "order": "newest first", "jarvis_summary": view.get("summary") or ""}


def _item(f: _Facts, a: dict) -> dict:
    wanted = str(a.get("id") or "")
    for i in f.everything():
        if str(i["id"]) == wanted:
            b = f.brief(i)
            b.update({"notes": (i.get("notes") or i.get("description") or "")[:600],
                      "steps": [s.get("text") for s in (i.get("steps") or []) if not s.get("done")][:10],
                      "section": i.get("section") or "", "origin": i.get("origin") or "",
                      "promise": i.get("promise_label") or "", "repeats": i.get("repeat_label") or "",
                      "due_date": i.get("due_date") or "", "scope": i.get("scope")})
            return b
    return {"error": "no item with that id among what the speaker may see"}


def _search(f: _Facts, a: dict) -> dict:
    words = [w for w in re.findall(r"\w+", str(a.get("words") or "").lower()) if len(w) > 2]
    if not words:
        return {"error": "no words"}

    def hit(text: str) -> bool:
        low = (text or "").lower()
        return any(w in low for w in words)

    items = [f.brief(i) for i in f.everything() if hit(i.get("title", "")) or hit(i.get("description") or i.get("notes") or "")][:15]
    since = timezone.now() - timedelta(days=30)
    alerts = [{"id": str(al.id), "what": al.kind, "title": al.title, "body": (al.body or "")[:160],
               "from": al.sender.spoken_name if al.sender_id else "JARVIS", "when": tu.to_local(al.created_at).strftime("%Y-%m-%d %H:%M"),
               "seen": bool(al.seen_at or al.acknowledged_at)}
              for al in Alert.objects.filter(recipient=f.user, created_at__gte=since).select_related("sender").order_by("-created_at")[:400]
              if hit(al.title) or hit(al.body)][:10]
    people = [{"name": u.spoken_name, "title": u.title_label, "department": u.department or ""} for u in services.team() if hit(u.spoken_name) or hit(u.title_label or "")]
    staff = [{"name": s.name, "department": s.department, "designation": s.designation} for s in StaffMember.objects.filter(active=True) if hit(s.name) or hit(s.department) or hit(s.designation)][:10]
    contacts = [{"name": c.name, "who": c.note} for c in Contact.objects.filter(owner=f.user) if hit(c.name) or hit(c.note)][:5]
    memories = [m.text for m in Memory.objects.filter(owner=f.user) if hit(m.text)][:5]
    return {"items": items, "alerts_and_messages": alerts, "team": people, "staff": staff, "contacts": contacts, "memories": memories}


def _panel(f: _Facts, a: dict) -> dict:
    if not f.user.is_md:
        return {"error": "the company panel is only for the MD"}
    filters = {k: str(v) for k, v in (a.get("filters") or {}).items() if v} if isinstance(a.get("filters"), dict) else {}
    if a.get("count_only") or filters:
        # The panel counts under its own filters (result_mode=count): the exact total, no rows, no guessing.
        return datacards.panel_count(f.user, str(a.get("report") or ""), filters, {k: a.get(k) for k in ("from", "to") if a.get(k)})
    params: dict = {"group": str(a.get("report") or "")}
    if a.get("words"):
        params["words"] = str(a["words"])
    for k in ("from", "to"):
        if a.get(k):
            params[k] = str(a[k])
    try:
        view = datacards.build(f.user, "panel", f.include_private, params=params) or {}
    except Exception as exc:  # noqa: BLE001 — the panel is another server; its failure is a fact for the model
        return {"error": f"the panel could not be read: {type(exc).__name__}"}
    if view.get("ask"):
        return {"error": view["ask"]}
    rows = view.get("rows") or []
    return {"title": view.get("title") or "", "subtitle": view.get("subtitle") or "", "count": len(rows),
            "rows": rows[:25], "stats": view.get("stats") or [], "jarvis_summary": view.get("summary") or ""}


def _counts(f: _Facts, a: dict) -> dict:
    out = {"pending": dict(services.pending_split(f.user, f.dash, f.include_private)["counts"])}
    closed = (Status.COMPLETED, Status.CANCELLED)
    out["given_to_others_open"] = len([t for t in f.dash.get("given_tasks", []) if t["status"] not in closed])
    if f.user.is_md:
        out["with_staff_open"] = len([t for t in f.dash.get("delegated", []) if t["status"] not in closed])
        load: dict[str, int] = {}
        for t in services.team_work_qs().exclude(status__in=closed):  # everyone's open work, whoever gave it
            if t.assignee_user_id:
                load[t.assignee_user.spoken_name] = load.get(t.assignee_user.spoken_name, 0) + 1
        out["team_open_work"] = load
    return out


_LOOKUPS = {"diary": _diary, "next_up": _next_up, "person": _person, "waiting": _waiting, "item": _item,
            "search": _search, "panel": _panel, "counts": _counts}


def run_lookup(f: _Facts, name: str, args: dict) -> dict:
    fn = _LOOKUPS.get(name)
    if fn is None:
        return {"error": f"no lookup named {name}"}
    try:
        return fn(f, args if isinstance(args, dict) else {})
    except Exception:  # noqa: BLE001 — one failed lookup must not end the answer; the model is told
        log.exception("lookup %s failed", name)
        return {"error": f"{name} failed on the server"}


# ------------------------------------------------------------------ the model
_JSON = re.compile(r"\{.*\}", re.DOTALL)


def _parse(raw: str) -> dict | None:
    """The answer object from the model's final text (the whole text, else the first {...} in it)."""
    text = (raw or "").strip()
    candidates = [text]
    found = _JSON.search(text)
    if found:
        candidates.append(found.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict) and str(data.get("say_en") or "").strip():
            return data
    return None


def wants_list(text: str) -> bool:
    return bool(LIST_WORDS.search(text or ""))


def try_answer(user, text: str, ctx: dict, include_private: bool, on_screen: dict | None,
               address: str, address_hi: str) -> dict | None:
    """One exact answer to the question, or None (the AI is off or could not be reached: the templates answer).
    Returns {"en", "hi", "about": [{"id", "title", "scope"}], "person": name, "view": card | None}."""
    if not settings.JARVIS_AI_ASK_ENABLED or not ai_brain.ai_available() or wants_list(text):
        return None
    f = _Facts(user, include_private)
    context = {
        "now_india": f.now.isoformat(), "weekday": f.now.strftime("%A"),
        "speaker": {"name": user.spoken_name, "role": user.role, "address": address},
        "team": [{"name": u.spoken_name, "title": u.title_label} for u in services.team() if u.id != user.id],
        "conversation": ctx.get("conversation") or [],
        "last_list": ctx.get("last_list") or {},
        "on_screen": [{"id": i.get("id"), "title": i.get("title")} for i in ((on_screen or {}).get("items") or [])[:15]],
        "phone_unlocked": include_private,
    }
    system = ASK_SYSTEM_PROMPT.format(reader=services.role_label(user), address=address, address_hi=address_hi)
    messages = [{"role": "user", "content": f"<context>\n{json.dumps(context, ensure_ascii=False, default=str)}\n</context>\n<question>\n{text[:1000]}\n</question>"}]
    client = ai_brain._get_answer_client()  # noqa: SLF001 — the long-timeout client, shared with web answers
    try:
        for _round in range(MAX_ROUNDS):
            response = client.messages.create(
                model=settings.JARVIS_AI_ASK_MODEL,
                max_tokens=2500,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=messages,
                tools=TOOLS,
                output_config={"effort": settings.JARVIS_AI_ASK_EFFORT},
            )
            ai_usage.record(user, AiUsage.Kind.ANSWER, response)
            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                results = []
                for block in response.content:
                    if getattr(block, "type", "") != "tool_use":
                        continue
                    facts = run_lookup(f, block.name, block.input or {})
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": json.dumps(facts, ensure_ascii=False, default=str)[:TOOL_RESULT_CHARS]})
                messages.append({"role": "user", "content": results})
                continue
            if response.stop_reason == "refusal":
                return None
            data = _parse(ai_brain._first_text(response))  # noqa: SLF001
            if data is None:
                log.warning("ask: the model's final text was not the answer object")
                return None
            return _finish(f, data)
        log.warning("ask: more than %s rounds of lookups; the templates answer", MAX_ROUNDS)
    except anthropic.RateLimitError:
        log.warning("ask: rate limited; the templates answer")
    except anthropic.APIStatusError as exc:
        log.warning("ask: API error %s; the templates answer. %s", exc.status_code, str(getattr(exc, "message", ""))[:200])
    except anthropic.APIConnectionError:
        log.warning("ask: AI unreachable; the templates answer")
    return None


def _finish(f: _Facts, data: dict) -> dict:
    about = []
    for raw in data.get("about") or []:
        i = f.known.get(str(raw))
        if i:
            about.append({"id": str(i["id"]), "title": i["title"], "scope": i.get("scope") or "shared"})
    view = None
    show = str(data.get("show") or "").strip()
    if show in datacards.VIEW_KINDS:
        params = data.get("show_params") if isinstance(data.get("show_params"), dict) else {}
        try:
            view = datacards.build(f.user, show, f.include_private, params=params)
        except Exception:  # noqa: BLE001 — the card is extra; the sentence is the answer
            log.exception("ask: the card %s could not be built", show)
            view = None
        if isinstance(view, dict) and view.get("ask"):
            view = None
    en = " ".join(str(data.get("say_en") or "").split())[:600]
    hi = " ".join(str(data.get("say_hi") or "").split())[:600]
    return {"en": en, "hi": hi if ai_brain._DEVANAGARI.search(hi) else "", "about": about, "person": f.person_named, "view": view}  # noqa: SLF001
