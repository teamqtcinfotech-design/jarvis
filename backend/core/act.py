"""The act path (25 Nov 2026): one brain for everything the speaker says.

The sentence, the day's thread (what was said and done since the morning), the open items with their ids and the
card on the phone go to Claude in one call. It understands, looks things up with the answer path's read-only
lookups (core/ask.py) and DOES things through tools that wrap the existing services: create, move, rename, cancel,
complete, make room, give work, send a message, set a reminder, undo. The guard rails stay in the tools on the
server: ids from the thread only, versions, clashes (returned as facts so the model asks in its own words), privacy,
undo for a minute. The model writes the reply. The rule engine in core/assistant.py is the fallback when the model
cannot be reached, and still builds the cards for work given to several people.

Sonnet 5.5 reads every turn first; when it says it is unsure (before it changed anything), the same thread goes to
Opus 5.5 for that turn. One thread per person per day, kept on the server (BrainThread) and sent with prompt
caching, so each turn costs only its new words.
"""
import json
import logging
import re
from datetime import date, datetime, time, timedelta

import anthropic
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist

from . import actions, ai_brain, ai_usage, ask, assistant, datacards, memory, scheduling, services
from . import lang as lang_mod
from . import timeutils as tu
from .models import AiUsage, BrainThread, Kind, Memory, Priority, PrivateTask, Status, User

log = logging.getLogger("jarvis.act")

MAX_ROUNDS = 8               # tool rounds for one sentence (several tools per round)
TOOL_RESULT_CHARS = 6000     # one tool's facts, cut (the model asks again, narrower, if it needs more)
THREAD_MAX_CHARS = 150_000   # a thread longer than this starts again with a short recap (never edited in place)
OPEN_ITEMS_PER_LIST = 14     # open items named in every turn (with their ids), per list
_JSON = re.compile(r"\{.*\}", re.DOTALL)
_KINDS = ("TASK", "MEETING", "CALL", "PAYMENT", "FOLLOW_UP")
_PRIORITIES = ("URGENT", "HIGH", "NORMAL", "LOW")

ACT_SYSTEM_PROMPT = """You are JARVIS, the personal assistant of {reader} (addressed as "{address}", in Hindi "{address_hi}"), inside their phone app. The speaker talks in Hinglish, Hindi or English, often through speech recognition (a name or a number may be slightly off). This conversation is today's thread: everything the speaker said today, everything you looked up and everything you did. Read it the way a person would: "isko", "usko", "wo wali", "pehle wali baat", "uske baad wali" mean what they mean in this thread.

WHAT YOU DO
- Understand, then act. Facts come only from the read tools (diary, next_up, person, waiting, item, search, panel, counts, staff); never guess a fact. "Staff" means the company's staff panel (the staff tool); "team" means only the few with JARVIS accounts. Things are done only through the write tools (create_item, move_item, edit_item, cancel_items, complete_item, make_room, free_slot, give_work, send_message, set_reminder, undo, remember, old_engine). Nothing is done without a tool, and nothing is claimed done unless its tool result says so.
- Ids come ONLY from this thread: the open items listed in the turn, and tool results. An item named by words ("Sharma ji wali", "4 baje wali", "kal wali", "the second one") is found among those first, with search or diary when it is not there.
- Specific = done at once: a clear change to one item (its time, day, length, name, priority), a delete, a complete, one piece of work for one person, a message, a reminder. No "shall I?", no reading back. The speaker can say undo for a minute.
- Ask only when it is genuinely unclear which item or which person is meant: ONE short question naming the choices by day or time ("Sharma ji ki do meetings hain: kal 10:30 wali ya Friday 4 baje wali?"). Never the same question twice; never a list of titles unasked. When the words themselves cannot be understood, say once "{address}, phir se boliye." Never say "samajh nahi aaya".
- Clashes: when create_item or move_item returns a clash, ask the one named question with the suggested time ("Aapki 4 baje Mehta ji ke saath meeting hai. Sharma ji ko 4:30 rakh dun?") and stop, unless the speaker already said what to do: "isko aage kar do" / "baaki aage kar do" / "push the other" = make_room, then create or move with force; "Mehta ji 4 pe hi" = the new one takes the suggested slot; "wahin rakho" / "4 baje hi" = force. Two meetings never share a time unless forced on purpose. The answer to your clash question comes in the next turn: act on it then.
- Several things in one breath: do all of them (several tool calls in one go) and reply with one line naming each. Work for two or more people in one breath goes through give_work in ONE call with all its items.
- give_work (his rule, 27 Nov 2026): the SAME work for several named people → send_now true: it is sent at once, and you reply with one line naming the people ("Nikki, Ankur Sir aur Gauri ko bhej diya."), never the work per person. DIFFERENT work for different people → send_now false: the list is on the phone for him to check; you reply with ONE short question naming only the people ("Nikki, Priya aur Gauri — bhej dun?"); his next yes sends all of them. "In sabko" / "sab ko" / "in teeno ko" right after you named some people (a list of who has pending work, say) means exactly those people, never the whole company.
- A card on the phone (a tool result with "card"): the details are on the screen, so say ONE line and never the items one by one: the one question the card needs (a name nobody knows, a clash), or "bhej dun?" with the people's names. Say "sent" or "done" only when the result says done.
- Questions: one exact sentence from the facts (the name for one item, the number for a number, yes or no first and at most one nearby fact); totals in hours and minutes from the diary; never count or add up yourself. "Dikhao" / "list" = also set show.
- Someone else's personal items are private; the tools already keep them out.

ASKED
- <asked> (when present) is what JARVIS asked a moment ago and is waiting on: the count of what came ("Sir, ek reminder hai. Kya main bataun?"), a reminder's own question ("Done, or remind you later?", "Shall I put them in?"), or a card on the phone waiting for a yes. The speaker's words are most likely the answer to it: a yes in any words (haan, ji, batao, sunao, padho, bolo, theek hai, "read them", "go ahead") → answer_asked yes; a no (nahi, rehne do, abhi nahi, not now, chhodo) → no; a time ("10 minute baad", "baad mein", "after lunch") → later with the minutes; part of the offered ones ("sirf meetings wale", "Nikki wala pehle", "pehla") → yes with which = those ids. Something else entirely (a question, an instruction) → do that instead with the tools; the asked thing stays as it is. Never answer the asked question in words yourself: answer_asked does it, and the phone then reads or acts. After answer_asked reply with one short word or line ("Ji.", "Theek hai, 10 minute baad.").

MEMORY
- <memory> (when present) holds what JARVIS knows about the speaker for good: who people are to them, how they like things done, their habits, and corrections they made before. Use it without being asked: a habit decides a default ("Mehta ji" on a Friday means the usual 4 PM), a preference decides how a thing is done, a correction is binding (that mistake is never made again). Facts about items, counts and times still come from the tools.
- A turn marked <correction> is the speaker correcting your last reply or action: take back what was wrong (undo, move_item, edit_item, cancel_items, or the right item this time) and do what they meant, in one go; say only what stands now. Never defend, never explain.

THE REPLY
- One sentence, two at most, specific: what was done and when ("Done: Meeting with Sharma ji, kal 7 PM."), the answer, or the one question. When a tool result carries "said", use that line as it is: it already names who got what.
- No "Ji Sir", no "Okay Sir", no filler; the address at most once, and only in a question. Never open two replies in a row the same way (look at your last reply in this thread).
- A suggestion of your own (a better time, a thing worth moving, a shorter way) is never mixed into the answer: ask first, in one short line ("{address}, ek suggestion dun?"), and give it only after a yes in the next turn.
- say_en in the speaker's own way (Roman Hinglish when they spoke Hinglish, English when English); say_hi the same sentence in Devanagari.

OUTPUT
After the tools, reply with ONLY a JSON object, no other text:
{{"say_en": "...", "say_hi": "...", "about": ["id", ...], "show": "", "show_params": {{}}, "learned": ""}}
- about: the ids of the items you changed or spoke about, first the one "isko" would mean next; empty when none.
- show: a card kind only when the speaker asked to see something laid out (today, upcoming, tasks, late, pending, alerts, people, person, panel, messages); else "". show_params: its filters ({{"who": "Nikki"}}, {{"group": "partners", "words": "Pune"}}), else {{}}.
- learned: one short plain sentence worth remembering for good, when this turn taught one: the rule a correction teaches ("'Sharma ji wali' means the GST meeting, not the Sharma Traders call"), a preference the speaker stated, who someone is. Never a one-off item, never a number or time of one item. "" otherwise (almost always).
When you are not sure what the speaker means and have not changed anything yet, reply with exactly {{"escalate": true}}: a stronger reading of the same thread follows.
"""

_ID = {"type": "string", "description": "the item's id exactly as it appears in this thread"}
_DAY = {"type": "string", "description": "YYYY-MM-DD"}
_TIME = {"type": "string", "description": "HH:MM, 24-hour"}
_KIND = {"type": "string", "enum": list(_KINDS)}
_PRIORITY = {"type": "string", "enum": list(_PRIORITIES)}

WRITE_TOOLS = [
    {"name": "create_item",
     "description": "A new item in the speaker's own diary: a meeting or call (with outside people), their own task, payment or follow-up. For a timed meeting or call the slot is checked first: a clash comes back as a fact with a suggested free time, and nothing is created until force is true.",
     "input_schema": {"type": "object", "properties": {
         "title": {"type": "string"}, "kind": _KIND, "date": _DAY, "time": _TIME,
         "minutes": {"type": "integer", "description": "its length, only when said"},
         "priority": _PRIORITY, "notes": {"type": "string"},
         "personal": {"type": "boolean", "description": "true for personal life (family, health, home, errands); false for office work"},
         "force": {"type": "boolean", "description": "true only once the speaker has settled a clash: put it at this time anyway"}},
         "required": ["title", "kind"]}},
    {"name": "move_item",
     "description": "Move one item to another day and/or time; its length is kept. For a meeting or call the new slot is checked: a clash comes back as a fact and nothing moves until force is true.",
     "input_schema": {"type": "object", "properties": {"id": _ID, "date": _DAY, "time": _TIME,
                                                       "force": {"type": "boolean", "description": "true only once the speaker has settled a clash"}},
                      "required": ["id"]}},
    {"name": "edit_item",
     "description": "Change one item's name, priority, length or kind.",
     "input_schema": {"type": "object", "properties": {"id": _ID, "title": {"type": "string"}, "priority": _PRIORITY,
                                                       "minutes": {"type": "integer"}, "kind": _KIND},
                      "required": ["id"]}},
    {"name": "cancel_items",
     "description": "Cancel these items at once (a cancel, never an erase: undo brings them back).",
     "input_schema": {"type": "object", "properties": {"ids": {"type": "array", "items": _ID}}, "required": ["ids"]}},
    {"name": "complete_item",
     "description": "Mark one item done.",
     "input_schema": {"type": "object", "properties": {"id": _ID}, "required": ["id"]}},
    {"name": "make_room",
     "description": "The speaker wants a new or moved meeting to take a slot and whatever is in the way to move on in turn ('isko 4 baje rakho, baaki aage kar do'). Moves the meetings and calls in the way (never a fixed block, never an urgent one) and returns what moved. Then create_item or move_item with force.",
     "input_schema": {"type": "object", "properties": {"date": _DAY, "time": _TIME, "minutes": {"type": "integer"},
                                                       "exclude_id": {"type": "string", "description": "the item being moved, if any"}},
                      "required": ["date", "time"]}},
    {"name": "free_slot",
     "description": "The first free stretch of this length on a day (today: after now), inside office hours. For 'jab free hoon tab', 'aage kabhi bhi', 'koi time nikaalo'.",
     "input_schema": {"type": "object", "properties": {"day": {"type": "string", "description": "today, tomorrow, a weekday or YYYY-MM-DD"},
                                                       "minutes": {"type": "integer"}, "after_time": _TIME}}},
    {"name": "give_work",
     "description": "Work for people: someone on the team (by name or job), a staff member of the company panel, a department, or everyone. One call with EVERY item of the sentence. One item for one person is sent at once. Several items: send_now true = the same work for several named people, sent at once; send_now false = different work for different people, shown as a list the speaker checks and sends with one yes.",
     "input_schema": {"type": "object", "properties": {
         "send_now": {"type": "boolean", "description": "true when the same work goes to several named people (sent at once); false for different work to different people (a list, one yes sends all)"},
         "items": {"type": "array", "items": {"type": "object", "properties": {
         "to": {"type": "string", "description": "the name as said (or a job: PA, accountant), several comma-separated, a department, or 'everyone'"},
         "title": {"type": "string", "description": "the work in a few clear words"},
         "kind": {"type": "string", "enum": ["TASK", "MEETING", "CALL", "PAYMENT", "FOLLOW_UP", "APPROVAL"]},
         "date": _DAY, "time": _TIME, "in_minutes": {"type": "integer", "description": "a span from now, when said"},
         "priority": _PRIORITY, "notes": {"type": "string"},
         "amount": {"type": "number", "description": "rupees for an approval or payment; 0 otherwise"}},
         "required": ["to", "title"]}}}, "required": ["items"]}},
    {"name": "send_message",
     "description": "A message (not work) to people on the team, sent at once (held until a date and time when one is given). Nobody named = the usual person.",
     "input_schema": {"type": "object", "properties": {"to": {"type": "array", "items": {"type": "string"}}, "text": {"type": "string"},
                                                       "date": _DAY, "time": _TIME}, "required": ["text"]}},
    {"name": "set_reminder",
     "description": "A plain reminder for the speaker at a moment: in N minutes, or on a date at a time.",
     "input_schema": {"type": "object", "properties": {"what": {"type": "string"}, "in_minutes": {"type": "integer"}, "date": _DAY, "time": _TIME},
                      "required": ["what"]}},
    {"name": "undo", "description": "Take back what JARVIS saved, sent or changed last (within a minute).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "remember", "description": "A fact the speaker wants JARVIS to remember.",
     "input_schema": {"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]}},
    {"name": "old_engine",
     "description": "Hand the sentence to the fixed engine for what has no tool here: a JARVIS setting, a screen action (PDF, share, open an item, settings, the walkie-talkie, home), the briefing, an approval decision, a checklist step, an update on an item for the other side, organising departments, or a change to the card waiting on the phone (a new time for it).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "answer_asked",
     "description": "The speaker's answer to what JARVIS just asked (<asked>): yes (read them / do it / send it), no (leave it), or later (bring it back after some minutes). For a count of several, which = the ids to read first or alone.",
     "input_schema": {"type": "object", "properties": {
         "decision": {"type": "string", "enum": ["yes", "no", "later"]},
         "minutes": {"type": "integer", "description": "later: after how many minutes (left out = the speaker's usual)"},
         "which": {"type": "array", "items": {"type": "string"}, "description": "yes to a count: only these ids (as listed in <asked>), in the order to read them"}},
         "required": ["decision"]}},
]
TOOLS = ask.TOOLS + WRITE_TOOLS
WRITE_NAMES = {t["name"] for t in WRITE_TOOLS}


def enabled() -> bool:
    return bool(getattr(settings, "JARVIS_AI_ACT_ENABLED", False) and ai_brain.ai_available())


_client_obj = None


def _client() -> anthropic.Anthropic:
    global _client_obj
    if _client_obj is None:
        _client_obj = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=settings.JARVIS_AI_ANSWER_TIMEOUT_SECONDS, max_retries=1)
    return _client_obj


# ------------------------------------------------------------------------------------------------- one turn
class _Turn:
    """What one sentence does: the facts it fetched, the writes it made (for undo), a card the fixed engine built."""

    def __init__(self, user: User, text: str, include_private: bool, screen: dict | None, history: list | None, previous: str,
                 asked: dict | None = None, pending: dict | None = None):
        self.user, self.text, self.include_private = user, text, include_private
        self.screen, self.history, self.previous = screen, history, previous
        self.asked = asked if isinstance(asked, dict) and asked.get("kind") else None  # what JARVIS just asked (an offer, a question, a card)
        self.pending = pending if isinstance(pending, dict) else None                # the card on the phone, when one waits
        self.answered: dict | None = None                                             # the speaker's answer to it (answer_asked)
        self.facts = ask._Facts(user, include_private)  # noqa: SLF001 — the answer path's facts and lookups
        self.writes = 0
        self.refs: list[dict] = []
        self.label = ""
        self.undo = False
        self.draft: dict | None = None   # a card on the phone (the model writes the line said with it), or a fixed-engine reply
        self.engine_answer = False       # the fixed engine answered (old_engine): its line stands, the model says nothing
        self._full: dict | None = None

    def full(self) -> dict:
        if self._full is None:
            self._full = assistant._full_items(self.user)  # noqa: SLF001 — every open item by id, with scope and version
        return self._full

    def item(self, raw) -> dict | None:
        it = self.full().get(str(raw or ""))
        if it is not None and not self.include_private and it.get("scope") == "private":
            return None
        return it


def _when(d: str, t: str) -> dict:
    return {"when": assistant._due_label(d, t), "when_hi": assistant._due_label_hi(d, t)}  # noqa: SLF001


def _clash_fact(review: dict) -> dict:
    out = {"clash": [{"title": c.get("title"), "from": tu.fmt_time(c["start"]), "to": tu.fmt_time(c["end"])}
                     for c in review.get("clashes") or []][:3]}
    sug = review.get("suggested")
    if sug:
        out["suggested"] = {"date": sug["due_date"], "time": sug["due_time"], **_when(sug["due_date"], sug["due_time"])}
    out["note"] = "nothing was changed: ask the speaker with the suggested time, or do what they already said (make_room / force)"
    return out


def _task_of(t: _Turn, item: dict):
    task = assistant._target_task(item)  # noqa: SLF001
    if task is None:
        return None
    if isinstance(task, PrivateTask) and task.owner_id != t.user.id:
        return None
    return task


def _create_item(t: _Turn, a: dict) -> dict:
    title = " ".join(str(a.get("title") or "").split())[:200]
    if not title:
        return {"error": "a title is needed"}
    kind = a.get("kind") if a.get("kind") in _KINDS else Kind.TASK
    d, tm = assistant._safe_date(str(a.get("date") or "")), assistant._safe_time(str(a.get("time") or ""))  # noqa: SLF001
    minutes = int(a["minutes"]) if isinstance(a.get("minutes"), int) and 0 < a["minutes"] <= 1440 else None
    prefs = services.get_prefs(t.user)
    if kind in (Kind.MEETING, Kind.CALL) and d and tm and not a.get("force"):
        review = scheduling.review(t.user, kind=kind, due_date=d, due_time=tm, minutes=minutes, prefs=prefs)
        if review["clashes"]:
            return _clash_fact(review)
    data = {"title": title, "kind": kind, "priority": a.get("priority") if a.get("priority") in _PRIORITIES else Priority.NORMAL,
            "notes": " ".join(str(a.get("notes") or "").split())[:2000]}
    if d:
        data["due_date"] = date.fromisoformat(d)
    if tm and d:
        data["due_time"] = time.fromisoformat(tm)
    if minutes:
        data["duration_minutes"] = minutes
    if t.user.is_md or a.get("personal"):
        task = services.create_private_task(t.user, data, source=PrivateTask.Source.VOICE)
        scope = "private"
    else:
        task = services.create_own_task(t.user, data)
        scope = "shared"
    t.refs.append({"type": scope, "id": str(task.id)})
    t.writes += 1
    t.label = t.label or task.title
    t.facts.known[str(task.id)] = {"id": str(task.id), "title": task.title, "scope": scope}
    return {"done": True, "id": str(task.id), "title": task.title, "kind": kind, **_when(d, tm if d else "")}


def _move_item(t: _Turn, a: dict) -> dict:
    item = t.item(a.get("id"))
    task = _task_of(t, item) if item else None
    if item is None or task is None:
        return {"error": "no open item with that id in this thread"}
    local = tu.to_local(task.due_at) if task.due_at else None
    d = assistant._safe_date(str(a.get("date") or "")) or (local.date().isoformat() if local else task.due_date.isoformat() if task.due_date else tu.today_local().isoformat())  # noqa: SLF001
    tm = assistant._safe_time(str(a.get("time") or "")) or (local.strftime("%H:%M") if local else "")  # noqa: SLF001
    if item.get("kind") in (Kind.MEETING, Kind.CALL) and tm and not a.get("force"):
        minutes = int(task.estimated_minutes or 0) or None
        review = scheduling.review(t.user, kind=item["kind"], due_date=d, due_time=tm, minutes=minutes,
                                   prefs=services.get_prefs(t.user), exclude_ids=[str(task.id)])
        if review["clashes"]:
            return _clash_fact(review)
    before = {"due_date": task.due_date.isoformat() if task.due_date else "", "due_time": local.strftime("%H:%M") if local else ""}
    changes = {"due_date": date.fromisoformat(d)}
    if tm:
        changes["due_time"] = time.fromisoformat(tm)
    scope = item.get("scope") or "shared"
    try:
        if scope == "private":
            services.update_private_task(t.user, str(task.id), changes, task.version)
        else:
            _task, req = services.change_shared_task(t.user, str(task.id), changes, task.version, "")
            if req is not None:
                return {"asked": f"that is {services.md_address()}'s own work: {services.md_address()} has been asked to move it"}
    except (services.ServiceError, ObjectDoesNotExist, ValueError) as exc:
        return {"error": f"could not move {task.title}: {getattr(exc, 'message', 'it was changed meanwhile')}"}
    t.refs.append({"type": "moved", "scope": scope, "id": str(task.id), "before": before})
    t.writes += 1
    t.label = t.label or task.title
    return {"done": True, "id": str(task.id), "title": task.title, **_when(d, tm)}


def _edit_item(t: _Turn, a: dict) -> dict:
    item = t.item(a.get("id"))
    task = _task_of(t, item) if item else None
    if item is None or task is None:
        return {"error": "no open item with that id in this thread"}
    changes, before = {}, {}
    title = " ".join(str(a.get("title") or "").split())[:200]
    if title:
        changes["title"], before["title"] = title, task.title
    if a.get("priority") in _PRIORITIES:
        changes["priority"], before["priority"] = a["priority"], task.priority
    if a.get("kind") in _KINDS:
        changes["kind"], before["kind"] = a["kind"], task.kind
    if isinstance(a.get("minutes"), int) and 0 < a["minutes"] <= 1440:
        changes["duration_minutes"], before["duration_minutes"] = a["minutes"], (task.estimated_minutes or 0)
    if not changes:
        return {"error": "nothing to change was given"}
    scope = item.get("scope") or "shared"
    try:
        if scope == "private":
            services.update_private_task(t.user, str(task.id), changes, task.version)
        else:
            _task, req = services.change_shared_task(t.user, str(task.id), changes, task.version, "")
            if req is not None:
                return {"asked": f"that is {services.md_address()}'s own work: {services.md_address()} has been asked"}
    except (services.ServiceError, ObjectDoesNotExist, ValueError) as exc:
        return {"error": f"could not change {task.title}: {getattr(exc, 'message', 'it was changed meanwhile')}"}
    t.refs.append({"type": "edited", "scope": scope, "id": str(task.id), "before": before})
    t.writes += 1
    t.label = t.label or changes.get("title") or task.title
    return {"done": True, "id": str(task.id), "title": changes.get("title") or task.title, "changed": sorted(changes)}


def _cancel_items(t: _Turn, a: dict) -> dict:
    items = [it for it in (t.item(i) for i in (a.get("ids") or [])[:20]) if it is not None]
    if not items:
        return {"error": "no open item with those ids in this thread"}
    done, failed, requests = assistant._cancel_now(t.user, items)  # noqa: SLF001 — the one cancel path (undo-able)
    if done:
        last = actions.last_done(t.user) or {}
        t.refs.extend(last.get("refs") or [])
        t.writes += len(done)
        t.label = t.label or done[0]["title"]
    return {"cancelled": [d["title"] for d in done], "failed": [f["title"] for f in failed], "asked_md": requests}


def _complete_item(t: _Turn, a: dict) -> dict:
    item = t.item(a.get("id"))
    task = _task_of(t, item) if item else None
    if item is None or task is None:
        return {"error": "no open item with that id in this thread"}
    scope = item.get("scope") or "shared"
    try:
        if scope == "private":
            services.update_private_task(t.user, str(task.id), {"status": Status.COMPLETED}, task.version)
            t.refs.append({"type": "edited", "scope": "private", "id": str(task.id), "before": {"status": item.get("status") or Status.OPEN}})
        else:
            _task, req = services.complete_shared_task(t.user, str(task.id), task.version)
            if req is not None:
                return {"asked": f"that is {services.md_address()}'s own work: {services.md_address()} has been asked to confirm"}
            t.refs.append({"type": "status", "id": str(task.id), "before": item.get("status") or Status.OPEN})
    except (services.ServiceError, ObjectDoesNotExist, ValueError) as exc:
        return {"error": f"could not complete {task.title}: {getattr(exc, 'message', 'it was changed meanwhile')}"}
    t.writes += 1
    t.label = t.label or task.title
    return {"done": True, "id": str(task.id), "title": task.title}


def _make_room(t: _Turn, a: dict) -> dict:
    d, tm = assistant._safe_date(str(a.get("date") or "")), assistant._safe_time(str(a.get("time") or ""))  # noqa: SLF001
    if not (d and tm):
        return {"error": "a date and a time are needed"}
    minutes = int(a["minutes"]) if isinstance(a.get("minutes"), int) and 0 < a["minutes"] <= 1440 else scheduling.minutes_for(Kind.MEETING, None)
    start = tu.combine_local(date.fromisoformat(d), time.fromisoformat(tm))
    exclude = [str(a["exclude_id"])] if a.get("exclude_id") else []
    plan = scheduling.push_plan(t.user, start, start + timedelta(minutes=minutes), services.get_prefs(t.user), exclude_ids=exclude)
    if plan is None:
        busy = scheduling.clashes(t.user, start, start + timedelta(minutes=minutes), services.get_prefs(t.user), exclude_ids=exclude)
        if not busy:
            return {"moved": [], "note": "the slot is free; nothing had to move"}
        return {"error": "the others cannot be moved on (an urgent meeting, or the day would run out): ask the speaker"}
    olds = {c["id"]: tu.to_local(c["start"]) for c in scheduling.commitments(t.user, start.date()) if c.get("id")}  # for undo
    failed = assistant._apply_moves(t.user, plan)  # noqa: SLF001
    moved = []
    for m in plan:
        if m["title"] in failed:
            continue
        old = olds.get(m["id"])
        t.refs.append({"type": "moved", "scope": m["scope"], "id": m["id"],
                       "before": {"due_date": old.date().isoformat() if old else "", "due_time": old.strftime("%H:%M") if old else ""}})
        moved.append({"id": m["id"], "title": m["title"], **_when(m["due_date"], m["due_time"])})
    t.writes += len(moved)
    return {"moved": moved, "failed": failed}


def _free_slot(t: _Turn, a: dict) -> dict:
    day = ask._day_of(str(a.get("day") or "today"), t.facts.today) or t.facts.today  # noqa: SLF001
    minutes = int(a["minutes"]) if isinstance(a.get("minutes"), int) and 0 < a["minutes"] <= 1440 else scheduling.minutes_for(Kind.MEETING, None)
    after = assistant._safe_time(str(a.get("after_time") or ""))  # noqa: SLF001
    not_before = tu.combine_local(day, time.fromisoformat(after)) if after else (t.facts.now if day == t.facts.today else None)
    sug = scheduling.suggest(t.user, minutes=minutes, prefs=services.get_prefs(t.user), day=day, not_before=not_before)
    if not sug:
        return {"free": None, "note": "no free stretch of that length in the coming days"}
    return {"free": {"date": sug["due_date"], "time": sug["due_time"], **_when(sug["due_date"], sug["due_time"])}}


def _engine_draft(t: _Turn, name: str, form: dict, send_now: bool = False) -> dict:
    """A form for the fixed engine (work for people, a message, a reminder, a memory): its own routing, checks and
    categories are reused as they are. A thing saved at once comes back as a fact with the engine's line. A card (a
    list to send, a question) stays on the phone, and the model writes the line said with it (his ask, 27 Nov 2026:
    the brain decides what is spoken, never a read-back of the list); with send_now a clean list is sent at once."""
    from . import livevoice  # noqa: PLC0415 — livevoice imports this module
    cmd = livevoice.cmd_for(t.user, name, {**form, "said": t.text})
    cmd.pop("_said", None)
    draft = assistant.build_draft(t.user, t.text, t.previous, t.history, "", None, include_private=t.include_private,
                                  pending=None, screen=t.screen, cmd=cmd)
    if draft.get("booked") or draft.get("reminder_saved") or draft.get("saved_now"):
        t.writes += 1
        t.undo = t.undo or bool(draft.get("undo"))
        last = actions.last_done(t.user) or {}
        t.refs.extend(r for r in (last.get("refs") or []) if r not in t.refs)
        return {"done": True, "said": draft.get("spoken_reply") or "", "said_hi": draft.get("reply_hi") or ""}
    facts = _card_facts(draft)
    if send_now and not facts.get("needs"):
        return _send_card_now(t, draft, facts)
    t.draft = draft
    if facts.get("needs"):
        facts["note"] = "the card waits on the phone: ask the one question in your own words (one line), nothing is sent yet"
    else:
        facts["note"] = ("the list is on the phone and nothing is sent yet: ask one short question naming only the people "
                         "('Nikki, Priya aur Gauri — bhej dun?'); his next yes sends all of them")
    return facts


def _card_facts(draft: dict) -> dict:
    """What a card on the phone holds, as plain facts for the model: each item's person, words and time, and what
    the card still needs (an unknown name, a clash) before it can go."""
    items = [draft] + [b for b in (draft.get("batch") or []) if isinstance(b, dict)]
    rows = []
    for it in items:
        who = [str(a.get("name") or "") for a in (it.get("assignees") or []) if isinstance(a, dict)]
        if it.get("staff"):
            who.append(str((it.get("staff") or {}).get("name") or ""))
        if it.get("team"):
            who.append(str((it.get("team") or {}).get("label") or "the team"))
        rows.append({"to": ", ".join(w for w in who if w) or str(it.get("staff_name") or ""),
                     "what": it.get("message") if it.get("action") == "message" else it.get("title") or "",
                     "when": it.get("due_label") or "", "kind": it.get("kind") or ""})
    needs = ""
    if draft.get("needs_clarification"):
        needs = draft.get("question") or draft.get("clarification_question") or "the card needs an answer"
    elif (draft.get("schedule") or {}).get("question"):
        needs = (draft.get("schedule") or {}).get("question") or ""
    out = {"card": True, "items": rows, "people": sorted({r["to"] for r in rows if r["to"]})}
    if needs:
        out["needs"] = needs
    return out


def _send_card_now(t: _Turn, draft: dict, facts: dict) -> dict:
    """The same work for several people (send_now): the list is sent at once, every item by the path its yes would
    take (the team's work and messages together, staff of the panel through the gateway)."""
    items = [draft] + [b for b in (draft.get("batch") or []) if isinstance(b, dict)]
    team = {"private_task", "md_request", "message"} | actions.PERSON_ACTIONS | set(actions.LEGACY)
    sent, failed = [], []
    together = [it for it in items if it.get("action") in team]
    if together:
        r = actions.run_card(t.user, {**together[0], "batch": together[1:]})
        t.refs.extend(x for x in (r.get("undo") or []) if x not in t.refs)
        failed += [{"what": title, "why": why} for title, why in r.get("failed") or []]
        missed = {x for x, _m in r.get("failed") or []}  # named as run_card names them
        sent += [{"to": f["to"], "what": f["what"]} for it, f in zip(items, facts["items"])
                 if it.get("action") in team and actions.item_name(it) not in missed]
    for it, f in zip(items, facts["items"]):
        if it.get("action") not in ("delegation", "team_delegation"):
            continue
        data = actions._task_data(it)  # noqa: SLF001 — the card's own reading of the item
        try:
            if it["action"] == "delegation":
                staff = services.staff_qs().get(id=(it.get("staff") or {})["id"])
                task, command = services.delegate_to_staff(t.user, data, staff, category_key=it.get("category_key") or "",
                                                           description=it.get("transcript") or t.text)
                if command.state == command.State.REJECTED:
                    task.delete()
                    failed.append({"what": f["what"], "why": command.error_message or "the staff app did not accept it"})
                    continue
                t.refs.append({"type": "shared", "id": str(task.id)})
            else:
                ids = [m["id"] for m in (it.get("team") or {}).get("members", [])]
                r = services.delegate_to_team(t.user, data, ids, category_key=it.get("category_key") or "", description=it.get("transcript") or t.text)
                failed += [{"what": f["what"], "why": f"not given to {x['name']}"} for x in r.get("not_assigned") or []]
                t.refs.extend({"type": "shared", "id": str(x["id"])} for x in r.get("assigned") or [] if x.get("id"))
            sent.append({"to": f["to"], "what": f["what"]})
        except (services.ServiceError, ObjectDoesNotExist, KeyError, ValueError) as exc:
            failed.append({"what": f["what"], "why": getattr(exc, "message", "it could not be sent")})
    if not sent and not failed:
        # Nothing here knew how to send this card (a kind the engine made that is not work for people): it stays
        # on the phone for his yes, as a list, rather than vanish.
        t.draft = draft
        return {**facts, "note": "the list is on the phone and nothing is sent yet: ask one short question naming only the people; his next yes sends all of them"}
    t.writes += len(sent)
    t.label = t.label or (sent[0]["what"] if sent else "")
    people = sorted({s["to"] for s in sent if s["to"]})
    return {"done": bool(sent), "sent": sent, "people": people, "failed": failed,
            "note": "say one line naming the people it went to" + (", and name what failed" if failed else "")}


def _give_work(t: _Turn, a: dict) -> dict:
    items = []
    for it in (a.get("items") or [])[:assistant.MAX_ITEMS]:
        if not isinstance(it, dict) or not str(it.get("title") or "").strip():
            continue
        items.append({"to": str(it.get("to") or ""), "what": str(it.get("title") or ""), "kind": it.get("kind"), "priority": it.get("priority"),
                      "notes": it.get("notes") or "", "amount": it.get("amount") or 0, "when_words": "",
                      "due_date": it.get("date") or "", "due_time": it.get("time") or "", "in_minutes": it.get("in_minutes") or 0})
    if not items:
        return {"error": "no work item was given"}
    return _engine_draft(t, "give_work", {"items": items}, send_now=bool(a.get("send_now")) and len(items) > 1)


def _send_message(t: _Turn, a: dict) -> dict:
    text = " ".join(str(a.get("text") or "").split())
    if not text:
        return {"error": "the message has no words"}
    return _engine_draft(t, "send_message", {"to": [str(x) for x in (a.get("to") or [])], "text": text, "when_words": "",
                                             "due_date": a.get("date") or "", "due_time": a.get("time") or ""})


def _set_reminder(t: _Turn, a: dict) -> dict:
    return _engine_draft(t, "remind_me", {"what": str(a.get("what") or "Reminder"), "when_words": "", "in_minutes": a.get("in_minutes") or 0,
                                          "due_date": a.get("date") or "", "due_time": a.get("time") or ""})


def _undo(t: _Turn, _a: dict) -> dict:
    try:
        out = actions.undo_last(t.user)
    except services.ServiceError as err:
        return {"error": err.message}
    return {"done": True, "said": out.get("en") or "", "said_hi": out.get("hi") or "", "undone": out.get("undone")}


def _remember(t: _Turn, a: dict) -> dict:
    return _engine_draft(t, "remember", {"fact": str(a.get("fact") or "")})


def _answer_asked(t: _Turn, a: dict) -> dict:
    """The speaker's yes / no / later to what JARVIS asked: recorded here; the phone (or the closed-app conversation)
    then reads, acts or snoozes exactly as it does for a yes it understood itself."""
    if not t.asked:
        return {"error": "nothing was asked"}
    decision = str(a.get("decision") or "").lower()
    if decision not in ("yes", "no", "later"):
        return {"error": "decision must be yes, no or later"}
    out: dict = {"decision": decision, "kind": t.asked.get("kind")}
    if decision == "later":
        prefs = services.get_prefs(t.user)
        minutes = a.get("minutes") if isinstance(a.get("minutes"), int) and 0 < a["minutes"] <= 24 * 60 else int(prefs.get("snoozeMinutes") or 15)
        until = (t.facts.now + timedelta(minutes=minutes)).replace(microsecond=0)
        out["minutes"], out["until"] = minutes, until.isoformat()
    which = [str(x) for x in (a.get("which") or []) if str(x)]
    if which and t.asked.get("kind") == "offer" and decision == "yes":
        known = {str(i.get("id")) for i in (t.asked.get("items") or []) if isinstance(i, dict)}
        picked = [x for x in which if x in known]
        if not picked:
            return {"error": "none of those ids is in the count: use the ids as listed in <asked>"}
        out["which"] = picked
    t.answered = out
    fact = {"done": True, "decision": decision}
    if "until" in out:
        fact["until"] = tu.fmt_time(datetime.fromisoformat(out["until"]))
        fact["until_hi"] = tu.fmt_time_hi(datetime.fromisoformat(out["until"]))
    if "which" in out:
        fact["reading"] = out["which"]
    fact["note"] = "say one short word or line; the phone reads or acts now"
    return fact


def _old_engine(t: _Turn, _a: dict) -> dict:
    draft = assistant.build_draft(t.user, t.text, t.previous, t.history, "", None, include_private=t.include_private,
                                  pending=None, screen=t.screen, engine_only=True)
    t.draft = draft
    t.engine_answer = True  # a setting, a screen action: the engine's own line is the answer
    return {"card": True, "said": draft.get("question") or draft.get("spoken_reply") or "", "note": "the fixed engine answered; say nothing more"}


_WRITES = {"create_item": _create_item, "move_item": _move_item, "edit_item": _edit_item, "cancel_items": _cancel_items,
           "complete_item": _complete_item, "make_room": _make_room, "free_slot": _free_slot, "give_work": _give_work,
           "send_message": _send_message, "set_reminder": _set_reminder, "undo": _undo, "remember": _remember, "old_engine": _old_engine,
           "answer_asked": _answer_asked}


def run_tool(t: _Turn, name: str, args: dict) -> dict:
    fn = _WRITES.get(name)
    if fn is None:
        return ask.run_lookup(t.facts, name, args)
    try:
        return fn(t, args if isinstance(args, dict) else {})
    except Exception:  # noqa: BLE001 — one failed tool must not end the turn; the model is told
        log.exception("act tool %s failed", name)
        return {"error": f"{name} failed on the server"}


# ------------------------------------------------------------------------------------------------- the thread
def _open_items_text(t: _Turn) -> str:
    """The open items with their ids, the way the brain has always seen them, so most sentences need no lookup."""
    dash = t.facts.dash
    parts = []
    for key, label in (("overdue", "late"), ("today", "today"), ("upcoming", "coming up"), ("anytime", "no date"),
                       ("given_tasks", "given by the speaker"), ("delegated", "with staff")):
        rows = [i for i in (dash.get(key) or []) if t.facts.keep(i) and i.get("status") not in (Status.COMPLETED, Status.CANCELLED)]
        if not rows:
            continue
        lines = []
        for i in rows[:OPEN_ITEMS_PER_LIST]:
            b = t.facts.brief(i)
            bits = [b["kind"].lower()]
            if b.get("start"):
                bits.append(b["start"] + (f" ({b['minutes']} min)" if b.get("minutes") else ""))
            elif b.get("when"):
                bits.append(b["when"])
            if b.get("for"):
                bits.append("for " + b["for"])
            if b.get("personal"):
                bits.append("personal")
            lines.append(f"[{b['id']}] {b['title']} ({', '.join(bits)})")
        more = f" (+{len(rows) - OPEN_ITEMS_PER_LIST} more: use diary / search)" if len(rows) > OPEN_ITEMS_PER_LIST else ""
        parts.append(f"{label}: " + "; ".join(lines) + more)
    return "\n".join(parts) or "nothing open"


def _asked_text(t: _Turn) -> str:
    """What JARVIS just asked and is waiting on, for the turn: the count with its items and their ids, a
    reminder's question, or the card on the phone."""
    a = t.asked or {}
    kind = str(a.get("kind") or "")
    if kind == "offer":
        rows = []
        for i in (a.get("items") or [])[:20]:
            if not isinstance(i, dict):
                continue
            q = i.get("question") if isinstance(i.get("question"), dict) else None
            bits = [str(i.get("what") or i.get("kind") or "item")]
            if i.get("from"):
                bits.append("from " + str(i["from"]))
            line = f"[{i.get('id')}] {' '.join(bits)}: \"{str(i.get('title') or '')[:80]}\""
            if q and q.get("en"):
                line += f" (its question: {q['en']})"
            rows.append(line)
        text = str(a.get("text") or "")
        return f'<asked kind="count">JARVIS asked: "{text}" — waiting: ' + "; ".join(rows) + "</asked>"
    if kind == "question":
        q = a.get("question") if isinstance(a.get("question"), dict) else {}
        return (f'<asked kind="question">JARVIS asked: "{q.get("en") or a.get("text") or ""}" about "{str(a.get("title") or "")[:80]}"'
                f' (a {q.get("kind") or "yes or no"} question)</asked>')
    if kind == "card":
        from . import conversation  # noqa: PLC0415 — conversation imports this module
        try:
            about = conversation._describe(t.pending) if t.pending else ""  # noqa: SLF001
        except Exception:  # noqa: BLE001 — a card shape it cannot describe
            about = ""
        return f'<asked kind="card">A card waits on the phone for a yes: {about or str(a.get("text") or "")}</asked>'
    return ""


def _turn_message(t: _Turn, recap: str = "", correction: bool = False) -> dict:
    now = t.facts.now
    head = [f'<turn now="{now.strftime("%A %d %B %Y, %H:%M")} (India)">']
    if recap:
        head.append(f"<earlier_today>{recap}</earlier_today>")
    if correction:
        head.append("<correction>the speaker is correcting your last reply or action</correction>")
    if t.asked:
        head.append(_asked_text(t))
    screen = [{"id": i.get("id"), "title": i.get("title")} for i in ((t.screen or {}).get("items") or [])[:12] if isinstance(i, dict)]
    if screen:
        head.append("<on_screen>" + json.dumps(screen, ensure_ascii=False) + "</on_screen>")
    head.append("<open_items>\n" + _open_items_text(t) + "\n</open_items>")
    head.append(f"<said>{t.text[:1500]}</said>\n</turn>")
    return {"role": "user", "content": [{"type": "text", "text": "\n".join(head)}]}


def _load_thread(user: User, today: date) -> tuple[BrainThread, list, str]:
    """The day's thread: its messages so far, and a recap when a long thread had to start again."""
    row, _made = BrainThread.objects.get_or_create(user=user, day=today)
    try:
        messages = json.loads(row.messages_json or "[]")
    except ValueError:
        messages = []
    if not isinstance(messages, list):
        messages = []
    recap = ""
    if len(row.messages_json or "") > THREAD_MAX_CHARS:
        recap = _recap(messages)
        messages = []
    return row, messages, recap


def _recap(messages: list) -> str:
    """The last few things said and answered, in plain words, for a thread that starts again."""
    lines = []
    for m in messages:
        content = m.get("content")
        if m.get("role") == "user" and isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    said = re.search(r"<said>(.*?)</said>", block.get("text") or "", re.DOTALL)
                    if said:
                        lines.append("Speaker: " + said.group(1).strip()[:160])
        elif m.get("role") == "assistant" and isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    data = _parse(block.get("text") or "")
                    if data and data.get("say_en"):
                        lines.append("JARVIS: " + str(data["say_en"])[:160])
    return " | ".join(lines[-10:])


def _blocks(content) -> list:
    return [b.model_dump(mode="json", exclude_none=True) if hasattr(b, "model_dump") else b for b in content]


def _with_cache(messages: list) -> list:
    """The thread as sent: the last block of the last message carries the cache mark, so the next turn pays only
    for its new words (the system prompt and the tools carry their own)."""
    if not messages:
        return messages
    out = [dict(m) for m in messages]
    last = out[-1]
    content = last.get("content")
    if isinstance(content, list) and content:
        blocks = [dict(b) for b in content]
        blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
        out[-1] = {**last, "content": blocks}
    return out


def _parse(raw: str) -> dict | None:
    text = (raw or "").strip()
    candidates = [text]
    found = _JSON.search(text)
    if found:
        candidates.append(found.group(0))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict) and (str(data.get("say_en") or "").strip() or data.get("escalate") is True):
            return data
    return None


# ------------------------------------------------------------------------------------------------- the model
def _model_turn(t: _Turn, model: str, system: list, thread: list, first: dict) -> tuple[dict | None, list]:
    """One model's reading of the turn: tool rounds until the reply. Returns (the answer object or None, the
    turn's messages as sent and received, for the thread)."""
    turn_messages = [first]
    client = _client()
    for _round in range(MAX_ROUNDS):
        response = client.messages.create(
            model=model, max_tokens=2500, system=system, messages=_with_cache(thread + turn_messages), tools=TOOLS,
            output_config={"effort": settings.JARVIS_AI_ACT_EFFORT},
        )
        ai_usage.record(t.user, AiUsage.Kind.COMMAND, response)
        turn_messages.append({"role": "assistant", "content": _blocks(response.content)})
        if response.stop_reason == "tool_use":
            results = []
            for block in response.content:
                if getattr(block, "type", "") != "tool_use":
                    continue
                facts = run_tool(t, block.name, block.input or {})
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(facts, ensure_ascii=False, default=str)[:TOOL_RESULT_CHARS]})
            turn_messages.append({"role": "user", "content": results})
            if t.engine_answer:
                return {"card": True}, turn_messages  # the fixed engine's line stands (a setting, a screen action)
            continue  # a card on the phone included: the model writes the line said with it
        if response.stop_reason == "refusal":
            return None, turn_messages
        text = ai_brain._first_text(response)  # noqa: SLF001
        data = _parse(text)
        if data is None and text.strip() and response.stop_reason == "end_turn":
            data = {"say_en": " ".join(text.split())[:600], "say_hi": "", "about": []}  # a plain sentence: taken as the reply
        return data, turn_messages
    log.warning("act: more than %s rounds for one sentence", MAX_ROUNDS)
    return None, turn_messages


def run(user: User, text: str, *, previous: str = "", history: list | None = None, include_private: bool = True,
        screen: dict | None = None, asked: dict | None = None, pending: dict | None = None) -> dict | None:
    """The sentence, understood and done: the draft the phone expects (exactly as /assistant/interpret/ returns),
    or None when the model could not be reached (the fixed engine then reads the sentence as before). With `asked`
    (what JARVIS just asked: a count, a reminder's question, a card) the words are read as its answer first."""
    if not enabled() or not (text or "").strip():
        return None
    t = _Turn(user, " ".join(text.split()), include_private, screen, history, previous, asked=asked, pending=pending)
    address, address_hi = services.address_of(user), services.address_hi_of(user)
    system = [{"type": "text", "cache_control": {"type": "ephemeral"},
               "text": ACT_SYSTEM_PROMPT.format(reader=services.role_label(user), address=address, address_hi=address_hi)}]
    # What JARVIS knows about this person for good (core/memory.py), its own cached block: it changes rarely.
    known = memory.block(user, address)
    if known:
        system.append({"type": "text", "cache_control": {"type": "ephemeral"}, "text": known})
    row, thread, recap = _load_thread(user, t.facts.today)
    correction = memory.is_correction(t.text) and bool(thread)  # "galat", "nahi, wo wali": the last turn is being corrected
    first = _turn_message(t, recap, correction)
    models = [settings.JARVIS_AI_ACT_MODEL, settings.JARVIS_AI_ACT_STRONG_MODEL]
    used = models[0]
    data, turn_messages = None, [first]
    try:
        for n, model in enumerate(models):
            used = model
            data, turn_messages = _model_turn(t, model, system, thread, first)
            if data is not None and data.get("escalate") is True and t.writes == 0 and t.draft is None and n == 0:
                log.info("act: %s unsure, handing the turn to %s", model, models[1])
                continue
            break
    except anthropic.RateLimitError:
        log.warning("act: rate limited; the fixed engine reads the sentence")
        return None
    except (anthropic.APIConnectionError, anthropic.APIStatusError) as err:
        log.warning("act: the model could not be reached (%s); the fixed engine reads the sentence", type(err).__name__)
        return None
    if data is None or data.get("escalate") is True:
        if t.writes:
            data = {"say_en": "Done: " + (t.label or "the change") + ".", "say_hi": "", "about": []}  # the writes stand; the reply is the facts
        elif t.draft is not None and not t.engine_answer:
            data = {"say_en": t.draft.get("question") or t.draft.get("spoken_reply") or "", "say_hi": t.draft.get("question_hi") or "", "about": []}
        else:
            return None
    _save_thread(row, thread, turn_messages, recap)
    _learn(t, data, correction, thread, turn_messages)
    if t.refs:
        actions.remember_done(user, t.refs, t.label)
    if t.draft is not None:
        draft = t.draft
        draft["engine"], draft["model"] = "act", used
        if not t.engine_answer:
            _voice_card(t, draft, data)
        return draft
    return _finish(t, data, used)


def _voice_card(t: _Turn, draft: dict, data: dict) -> None:
    """The card keeps everything it shows and asks on the phone; the line spoken with it is the model's (his ask,
    27 Nov 2026), in the speaker's language, with the card's own line only when the model gave none."""
    en = " ".join(str(data.get("say_en") or "").split())[:600]
    hi = " ".join(str(data.get("say_hi") or "").split())[:600]
    hi = hi if ai_brain._DEVANAGARI.search(hi) else ""  # noqa: SLF001
    if not en:
        return
    lang = lang_mod.lang_for(t.user, t.text)
    draft["spoken_reply"], draft["reply_en"], draft["reply_hi"] = en, en, hi
    draft["speak_text"], draft["speak_lang"] = (hi, "hi") if lang == "hi" and hi else (en, "en")
    draft["reply_language"] = lang


def _save_thread(row: BrainThread, thread: list, turn_messages: list, recap: str) -> None:
    messages = (thread if not recap else []) + turn_messages
    row.messages_json = json.dumps(messages, ensure_ascii=False, default=str)
    row.turns += 1
    row.save(update_fields=["messages_json", "turns", "updated_at"])


def _learn(t: _Turn, data: dict, correction: bool, thread: list, turn_messages: list) -> None:
    """What this turn taught (26 Nov 2026): the rule the model named goes into memory the same minute (a
    correction's rule as a correction, anything else as a preference), and a correction turn becomes a bank case
    with the turn it corrected, replayed against the real model by `manage.py bank run`."""
    learned = " ".join(str(data.get("learned") or "").split())[:memory.NOTE_CHARS] if isinstance(data, dict) else ""
    try:
        if learned:
            memory.note(t.user, learned, kind=Memory.Kind.CORRECTION if correction else Memory.Kind.PREFERENCE)
        if correction:
            turns = memory.split_turns(thread)
            memory.record_correction(t.user, t.facts.today, t.text, turns[-1] if turns else [], turn_messages, learned)
    except Exception:  # noqa: BLE001 — a note that could not be kept must never cost the turn
        log.exception("act: what the turn taught could not be kept")


def _finish(t: _Turn, data: dict, model: str) -> dict:
    en = " ".join(str(data.get("say_en") or "").split())[:600]
    hi = " ".join(str(data.get("say_hi") or "").split())[:600]
    hi = hi if ai_brain._DEVANAGARI.search(hi) else ""  # noqa: SLF001
    lang = lang_mod.lang_for(t.user, t.text)
    lang_mod.remember(t.user, lang_mod.detect(t.text))
    speak_hi = lang == "hi" and bool(hi)
    about = []
    for raw in data.get("about") or []:
        i = t.facts.known.get(str(raw)) or t.full().get(str(raw))
        if i:
            about.append({"id": str(i["id"]), "title": i["title"], "scope": i.get("scope") or "shared"})
    view = None
    show = str(data.get("show") or "").strip()
    if show in datacards.VIEW_KINDS:
        params = data.get("show_params") if isinstance(data.get("show_params"), dict) else {}
        try:
            view = datacards.build(t.user, show, t.include_private, params=params)
        except Exception:  # noqa: BLE001 — the card is extra; the sentence is the answer
            log.exception("act: the card %s could not be built", show)
            view = None
        if isinstance(view, dict) and view.get("ask"):
            view = None
    services.remember_last_view(t.user, {"kind": "act", "question": t.text[:120]}, en,
                                has_private=any(a.get("scope") == "private" for a in about), items=about, about=t.facts.person_named or "",
                                cursor=0 if about else None)
    out = {
        "action": "answer", "intent": "act", "engine": "act", "model": model, "transcript": t.text,
        "title": "", "kind": "TASK", "priority": "NORMAL", "due_date": "", "due_time": "", "due_label": "",
        "spoken_reply": en, "reply_en": en, "reply_hi": hi, "speak_text": hi if speak_hi else en, "speak_lang": "hi" if speak_hi else "en",
        "reply_language": lang, "needs_clarification": False, "question": "", "question_hi": "", "warnings": [],
        "booked": t.writes > 0, "saved_now": t.writes > 0, "undo": bool(t.refs) or t.undo, "card_done": True,
        "view": view, "targets": [], "target": None, "batch": [],
    }
    if t.answered:
        # The answer to what JARVIS asked, in the shape the phone and the closed-app conversation already act on:
        # a yes reads / does / sends, a no leaves it, later brings it back, "which" reads only those.
        d = t.answered["decision"]
        out["card_done"] = False  # the card waiting on the phone is settled by the phone's own yes / no handling
        if d == "later":
            out["action"], out["snooze_until"] = "snooze", t.answered["until"]
        elif d == "no":
            out["action"] = "cancel_pending"
        elif t.answered.get("which"):
            out["action"], out["read_keys"] = "read_some", t.answered["which"]
        else:
            out["action"] = "confirm_pending"
        out["asked_kind"] = t.answered.get("kind") or ""
    return out
