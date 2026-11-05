"""Turns one instruction into a draft the user confirms on screen.

Nothing here writes business data. The draft says what WOULD happen; the phone
then calls the normal, permission-checked endpoint with an Idempotency-Key.
"""
import re
from datetime import date, time

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist

from . import ai_brain, datacards, hinglish_parser, scheduling, services
from . import timeutils as tu
from .models import Kind, Priority, PrivateTask, SharedTask, TaskCategory, User
from .taskpanel.client import TaskPanelError, get_client as tp_client
from .taskpanel.contracts import ALL_STAFF_WORDS, build_validate_body

TASK_ACTIONS = {"private_task", "md_request", "delegation", "pa_task", "pa_meeting", "team_delegation"}
EDIT_ACTIONS = {"change_task", "delete_task"}
_CHANGE_FIELDS = {"title", "due_date", "due_time", "priority", "kind"}
# Drafts that put something in somebody's diary, and whose diary it is: the speaker's own, the PA's
# (Sir giving Mansi work) or the MD's (Mansi asking for Sir's time; she only ever hears "busy", never why).
_SLOT_ACTIONS = {"private_task": "self", "pa_meeting": "self", "pa_task": "pa", "md_request": "md", "change_task": "self"}
MAX_SCHEDULE_QUESTIONS = 2  # ask about a clash at most twice, then take his word

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_KINDS = {k for k, _ in Kind.choices}
_PRIORITIES = {p for p, _ in Priority.choices}


def _safe_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat() if value else ""
    except ValueError:
        return ""


def _safe_time(value: str) -> str:
    return value if value and _HHMM.match(value) else ""


def _due_label(due_date: str, due_time: str) -> str:
    if not due_date:
        return ""
    d = date.fromisoformat(due_date)
    if due_time:
        return tu.due_phrase("TIMED", d, tu.combine_local(d, time.fromisoformat(due_time)))
    return tu.fmt_day(d)


def _match_items(words: str, items: list[dict]) -> list[dict]:
    tokens = [t for t in re.findall(r"\w+", words.lower()) if len(t) > 2]
    if not tokens:
        return []
    scored = []
    for item in items:
        title = item["title"].lower()
        score = sum(1 for t in tokens if t in title)
        if score:
            scored.append((score, item))
    if not scored:
        return []
    best = max(s for s, _ in scored)
    return [i for s, i in scored if s == best]


def _changes_from(cmd: dict) -> dict:
    """The validated new values for change_task (only the fields the model listed)."""
    out = {}
    for field in cmd.get("changed_fields") or []:
        if field not in _CHANGE_FIELDS:
            continue
        value = cmd.get(field)
        if field == "title" and (value or "").strip():
            out["title"] = value.strip()[:200]
        elif field == "due_date" and _safe_date(value or ""):
            out["due_date"] = _safe_date(value)
        elif field == "due_time" and _safe_time(value or ""):
            out["due_time"] = value
        elif field == "priority" and value in _PRIORITIES:
            out["priority"] = value
        elif field == "kind" and value in _KINDS:
            out["kind"] = value
    return out


def describe_changes(changes: dict) -> str:
    bits = []
    if "title" in changes:
        bits.append(f"rename to '{changes['title']}'")
    if "due_date" in changes or "due_time" in changes:
        d = changes.get("due_date")
        t = changes.get("due_time")
        when = _due_label(d, t) if d else (tu.fmt_time(tu.combine_local(tu.today_local(), time.fromisoformat(t))) if t else "")
        bits.append(f"move to {when}" if d else f"time to {when}")
    if "priority" in changes:
        bits.append(f"priority to {changes['priority'].lower()}")
    if "kind" in changes:
        bits.append(f"type to {changes['kind'].lower().replace('_', '-')}")
    return ", ".join(bits)


def _chosen(cmd: dict, by_id: dict) -> list[dict]:
    """The items the model picked (target_refs, or the single target_ref), kept only if they really exist."""
    refs = [r for r in (cmd.get("target_refs") or []) if isinstance(r, str)]
    if not refs and cmd.get("target_ref"):
        refs = [cmd["target_ref"]]
    seen, out = set(), []
    for r in refs:
        if r in by_id and r not in seen:
            seen.add(r)
            out.append(by_id[r])
    return out


_PA_WORDS = {"pa", "p.a.", "p a", "meri pa", "my pa", "mera pa", "assistant", "my assistant", "personal assistant"}


def _is_pa_name(name: str) -> bool:
    norm = " ".join((name or "").lower().split())
    if not norm:
        return False
    if norm in _PA_WORDS:
        return True
    pa = services.get_pa()
    if not pa:
        return False
    names = {n.lower() for n in (pa.display_name, pa.first_name, pa.username, pa.get_full_name()) if n}
    return norm in names


_PERSONAL = re.compile(
    r"\b(mera|meri|mere|mujhe|mujhko|personal|yaad|remind me|reminder|my own|khud|apna|apni)\b", re.IGNORECASE)
_WITH = re.compile(r"\b(ke saath|ke sath|ke sang|with)\b", re.IGNORECASE)
_TEAM_SUFFIX = re.compile(r"\b(team|department|dept|waalon|walon|waale|wale|log)\s*$", re.IGNORECASE)


def _mentions_pa(text: str) -> bool:
    low = f" {text.lower()} "
    if any(f" {w} " in low for w in ("pa", "p.a.", "meri pa", "my pa", "assistant")):
        return True
    pa = services.get_pa()
    return bool(pa and any(n and f" {n.lower()} " in low for n in (pa.display_name, pa.first_name)))


def _route_intent(user: User, cmd: dict, text: str) -> str:
    """Final say on where a task goes. The AI proposes; these rules keep the routing safe and predictable."""
    intent = cmd.get("intent", "unknown")
    staff_name = (cmd.get("staff_name") or "").strip()
    # "All staff / Accounts team" said as a person's name -> a team assignment.
    if intent == "assign_to_staff" and (staff_name.lower() in ALL_STAFF_WORDS or _TEAM_SUFFIX.search(staff_name)):
        cmd["team_name"] = "all" if staff_name.lower() in ALL_STAFF_WORDS else _TEAM_SUFFIX.sub("", staff_name).strip()
        intent = "assign_to_team"
    # A meeting WITH the PA is the MD's meeting (PA is told), never a task for the PA to do.
    if user.is_md and cmd.get("kind") in {Kind.MEETING, Kind.CALL} and _WITH.search(text) and _mentions_pa(text) \
            and intent in {"assign_to_pa", "assign_to_staff", "create_task", "needs_destination"}:
        intent = "meet_with_pa"
    # "Mansi ko bolo…" / "PA ko bolo…" from the MD = a direct PA task (inside JARVIS), not TaskPanel.
    if user.is_md and intent == "assign_to_staff" and _is_pa_name(staff_name):
        intent = "assign_to_pa"
    # Offline parser cannot judge ownership: unless it is plainly the MD's own, ask.
    if cmd.get("engine") == "rules" and intent == "create_task" and cmd.get("kind") == Kind.TASK \
            and not _PERSONAL.search(text):
        intent = "needs_destination"
    if user.is_pa:
        if intent in {"create_task", "meet_with_pa"}:
            intent = "request_for_md"
        if intent in {"accept_request", "decline_request", "complete_task", "assign_to_pa"}:
            intent = "unknown"
    elif intent == "request_for_md":
        intent = "create_task"
    return intent


def _prepare_team(draft: dict, cmd: dict, address: str):
    members, skipped, label = services.team_members(cmd.get("team_name") or "all")
    draft["team"] = {
        "label": label,
        "members": [_staff_option(s) for s in members],
        "skipped": [_staff_option(s) for s in skipped],
    }
    draft["category_key"] = settings.TASKPANEL_DEFAULT_CATEGORY  # mixed departments: use the general category
    if draft["priority"] == Priority.LOW:
        draft["priority"] = Priority.NORMAL
    if not members:
        draft["needs_clarification"] = True
        draft["question"] = (f"{address}, I couldn't find anyone in that team. Which team or person should do this?"
                             if not label else f"{address}, everyone in {label} is on leave. Who else should do this?")
        return
    skipped_txt = f" {len(skipped)} on leave will be skipped." if skipped else ""
    draft["spoken_reply"] = (f"{address}, this will go to {len(members)} "
                             f"{'person' if len(members) == 1 else 'people'} in {label}: {draft['title']}"
                             + (f", {draft['due_label']}" if draft["due_label"] else "") + f".{skipped_txt} Shall I assign it?")


def _staff_option(s) -> dict:
    return {"id": s.id, "name": s.name, "department": s.department, "on_leave": s.on_leave,
            "leave_until": s.leave_until.isoformat() if s.leave_until else None}


def _prepare_delegation(draft: dict, cmd: dict, user: User, address: str):
    """Resolve the person against the live TaskPanel directory, pick a category, pre-check with TaskPanel."""
    name = (cmd.get("staff_name") or "").strip()
    draft["categories"] = [{"key": c.key, "name": c.name, "department": c.department}
                           for c in TaskCategory.objects.filter(active=True)]
    valid_keys = {c["key"] for c in draft["categories"]}
    suggested = (cmd.get("category_key") or "").strip()
    default = settings.TASKPANEL_DEFAULT_CATEGORY
    draft["category_key"] = suggested if suggested in valid_keys else (default if default in valid_keys or not valid_keys else "")
    if draft["priority"] == Priority.LOW:
        draft["priority"] = Priority.NORMAL  # TaskPanel has no Low priority
        draft["warnings"].append("TaskPanel has no Low priority, so this will be Normal.")

    matches, suggestions = services.resolve_staff(name)
    if len(matches) == 1:
        person = matches[0]
        draft["staff"] = _staff_option(person)
        draft["staff_candidates"] = [draft["staff"]]
    elif len(matches) > 1:
        draft["staff_candidates"] = [_staff_option(s) for s in matches]
        draft["needs_clarification"] = True
        options = " or ".join(f"{s.name} from {s.department}" for s in matches[:3])
        draft["question"] = f"{address}, which {name}: {options}?"
        return
    elif suggestions:
        draft["staff_candidates"] = [_staff_option(s) for s in suggestions]
        draft["needs_clarification"] = True
        if len(suggestions) == 1:
            draft["question"] = f"{address}, did you mean {suggestions[0].name} from {suggestions[0].department}?"
        else:
            draft["question"] = f"{address}, I couldn't find {name}. Did you mean " + \
                " or ".join(s.name for s in suggestions[:3]) + "?"
        return
    else:
        draft["needs_clarification"] = True
        draft["question"] = (f"{address}, I couldn't find {name} in the TaskPanel staff list. Who should do this?"
                             if name else f"{address}, who should I assign this to?")
        return

    if person.on_leave:
        until = tu.fmt_day(person.leave_until) if person.leave_until else "further notice"
        draft["warnings"].append(f"{person.name} is on leave until {until}.")
        draft["needs_clarification"] = True
        draft["question"] = f"{address}, {person.name} is on leave until {until}. Should someone else do this?"
        return

    # Ask TaskPanel to check its own rules now (category/department, dates) — nothing is saved by this.
    link = services.user_link(user)
    if link is None:
        draft["warnings"].append("Your account is not linked to TaskPanel yet, so this cannot be assigned.")
        return
    due_at = None
    if draft["due_date"] and draft["due_time"]:
        due_at = tu.combine_local(date.fromisoformat(draft["due_date"]), time.fromisoformat(draft["due_time"]))
    body = build_validate_body(
        category_key=draft["category_key"], title=draft["title"] or draft["transcript"][:120],
        description=draft["transcript"], assignee_key=person.external_id, priority=draft["priority"],
        due_at=due_at, due_date=draft["due_date"] or None,
    )
    try:
        tp_client().validate(link, body)
    except TaskPanelError as err:
        if err.status == 422 and err.code != "due_in_past":
            draft["needs_clarification"] = True
            draft["question"] = f"{address}, {err.message}"
            draft["warnings"].append(err.message)
        elif err.status in (0, 429) or err.status >= 500:
            draft["warnings"].append("Could not pre-check with TaskPanel; it will check again when you assign.")


_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_TASK_INTENTS = {"create_task", "assign_to_staff", "assign_to_team", "assign_to_pa", "meet_with_pa",
                 "request_for_md", "needs_destination"}


# ------------------------------------------------------------------ does it fit the diary?
def _clean_pending(pending) -> dict | None:
    """The card the phone (or the background brain) is holding. Untrusted: only the fields used here are checked."""
    if not isinstance(pending, dict) or not isinstance(pending.get("action"), str):
        return None
    p = dict(pending)
    p["due_date"] = _safe_date(str(p.get("due_date") or ""))
    p["due_time"] = _safe_time(str(p.get("due_time") or ""))
    p["kind"] = p.get("kind") if p.get("kind") in _KINDS else Kind.TASK
    p["title"] = str(p.get("title") or "")[:200]
    p["duration_minutes"] = int(p["duration_minutes"]) if isinstance(p.get("duration_minutes"), int) and p["duration_minutes"] > 0 else None
    p["changes"] = {k: str(v) for k, v in (p.get("changes") or {}).items() if k in _CHANGE_FIELDS} if isinstance(p.get("changes"), dict) else {}
    p["targets"] = [t for t in (p.get("targets") or []) if isinstance(t, dict) and isinstance(t.get("id"), str)] \
        if isinstance(p.get("targets"), list) else []
    if not p["targets"] and isinstance(p.get("target"), dict) and isinstance(p["target"].get("id"), str):
        p["targets"] = [p["target"]]
    p["schedule"] = p.get("schedule") if isinstance(p.get("schedule"), dict) else {}
    p["warnings"] = [str(w) for w in p.get("warnings") or []] if isinstance(p.get("warnings"), list) else []
    return p


def _target_task(t: dict):
    try:
        if t.get("scope") == "private":
            return PrivateTask.objects.get(id=t["id"])
        return SharedTask.objects.select_related("staff").get(id=t["id"])
    except (ObjectDoesNotExist, ValueError, KeyError, TypeError):
        return None


def _slot_of(user: User, draft: dict):
    """(whose diary, kind, due_date, due_time, minutes, ids to leave out) for what the draft adds or moves."""
    whose = _SLOT_ACTIONS.get(draft.get("action", ""))
    if not whose:
        return None
    if draft["action"] == "change_task":
        targets = draft.get("targets") or []
        changes = draft.get("changes") or {}
        if len(targets) != 1 or not ({"due_date", "due_time"} & changes.keys()):
            return None
        task = _target_task(targets[0])
        if task is None:
            return None
        if isinstance(task, SharedTask):
            if task.assignee_type == SharedTask.Assignee.STAFF:
                return None  # a staff member's diary is not ours to judge
            if task.assignee_type == SharedTask.Assignee.PA and task.origin != SharedTask.Origin.MD_MEET_PA:
                whose = "pa" if user.is_md else "self"
        local = tu.to_local(task.due_at) if task.due_at else None
        due_date = _safe_date(changes.get("due_date") or "") or (local.date().isoformat() if local else
                                                                  task.due_date.isoformat() if task.due_date else "")
        due_time = _safe_time(changes.get("due_time") or "") or (local.strftime("%H:%M") if local else "")
        kind = changes.get("kind") if changes.get("kind") in _KINDS else task.kind
        return whose, kind, due_date, due_time, task.estimated_minutes, [str(task.id)]
    return whose, draft.get("kind") or Kind.TASK, draft.get("due_date") or "", draft.get("due_time") or "", \
        draft.get("duration_minutes"), []


def _insert_notes(reply: str, notes: list[str], hindi: bool = False) -> str:
    """Put short notes before the closing question of a reply ("... Shall I save it?")."""
    if not notes:
        return reply
    text = " ".join(notes)
    end = "।" if hindi else "."
    m = re.match(rf"^(.*[{end}!])\s+([^{end}!?]*\?)\s*$", reply.strip(), re.S)
    if m:
        return f"{m.group(1)} {text} {m.group(2)}"
    return f"{reply.strip()} {text}".strip()


def _advise_schedule(user: User, draft: dict, address: str, asked: int = 1) -> None:
    """Look at the diary before a timed item is saved or moved: clash -> offer the next free slot; a meeting or call
    with no time -> offer one; a very full day or a weekly off -> say so. The draft keeps the user's own words;
    only the spoken question changes, and nothing is moved until he answers."""
    if draft.get("needs_clarification"):
        return
    slot = _slot_of(user, draft)
    if not slot:
        return
    whose, kind, due_date, due_time, minutes, exclude = slot
    if not due_date and kind not in (Kind.MEETING, Kind.CALL):
        return  # a task with no day needs no slot
    subject = {"pa": services.get_pa(), "md": services.get_md()}.get(whose, user)
    if subject is None:
        return
    prefs = services.get_prefs(subject)
    review = scheduling.review(subject, kind=kind, due_date=due_date, due_time=due_time, minutes=minutes, prefs=prefs,
                               exclude_ids=exclude)
    anonymous = whose == "md"  # Mansi only ever hears that Sir is busy, never with what
    pa = services.get_pa()
    pa_name = pa.spoken_name if pa else "your PA"
    address_hi = "सर" if user.is_md else user.spoken_name
    item = (draft.get("targets") or [{}])[0].get("title", "") if draft["action"] == "change_task" else draft.get("title", "")
    item = item or "this"
    if whose == "pa" and draft["action"] != "change_task":
        item = f"{item} for {pa_name}"
    d = date.fromisoformat(due_date) if due_date else None
    day_en = tu.fmt_day(d) if d else ""
    day_cap = day_en[:1].upper() + day_en[1:]
    day_hi = tu.fmt_day_hi(d) if d else ""
    when_en = _due_label(due_date, due_time)
    when_hi = f"{day_hi} {tu.fmt_time_hi(tu.combine_local(d, time.fromisoformat(due_time)))}" if d and due_time else day_hi

    sug = review.get("suggested")
    alt_en = alt_hi = ""
    if sug:
        s_local = tu.to_local(sug["start"])
        alt_en = tu.fmt_time(sug["start"]) if sug["same_day"] or not d else f"{tu.fmt_day(s_local.date())} at {tu.fmt_time(sug['start'])}"
        alt_hi = tu.fmt_time_hi(sug["start"]) if sug["same_day"] or not d else f"{tu.fmt_day_hi(s_local.date())} {tu.fmt_time_hi(sug['start'])}"

    notes_en, notes_hi, warnings = [], [], []
    load = review.get("load") or {}
    if load.get("heavy"):
        n = load["count"]
        who = {"self": "", "pa": f"{pa_name} ", "md": "Sir "}[whose]
        notes_en.append(f"{who}{day_en if who else day_cap} already has {n} items.")
        notes_hi.append(f"{day_hi} पहले से {n} काम हैं।")
        warnings.append(f"{day_cap} already has {n} items")
    if review.get("off_day"):
        notes_en.append(f"{day_cap} is a weekly off.")
        notes_hi.append(f"{day_hi} छुट्टी का दिन है।")
        warnings.append(f"{day_cap} is a weekly off")

    question_en = question_hi = ""
    clashes = review.get("clashes") or []
    if clashes:
        c = clashes[0]
        span = f"{tu.fmt_time(c['start'])}–{tu.fmt_time(c['end'])}"
        if anonymous:
            head_en = f"{address}, {item} {when_en}: Sir is busy then ({span})."
            head_hi = f"{address_hi}, {when_hi} {item}: सर उस समय बिज़ी हैं।"
            warnings.append(f"Sir is busy {span}")
        else:
            has = {"self": "you already have", "pa": f"{pa_name} already has"}.get(whose, "there is already")
            head_en = f"{address}, {item} {when_en}: {has} {c['title']} then."
            head_hi = f"{address_hi}, {when_hi} {item}: उस समय {c['title']} पहले से है।"
            warnings.append(f"Overlaps with {c['title']} ({span})")
        tail_en = f" Shall I put it at {alt_en} instead?" if sug else " Shall I save it anyway?"
        tail_hi = f" {alt_hi} रख दूँ?" if sug else " फिर भी सेव कर दूँ?"
        question_en = _insert_notes(head_en + tail_en, notes_en)
        question_hi = _insert_notes(head_hi + tail_hi, notes_hi, hindi=True)
    elif review.get("needs_slot") and sug:
        s_en, e_en = tu.fmt_time(sug["start"]), tu.fmt_time(sug["end"])
        s_hi, e_hi = tu.fmt_time_hi(sug["start"]), tu.fmt_time_hi(sug["end"])
        s_day = tu.to_local(sug["start"]).date()
        free = {"self": "you're free", "pa": f"{pa_name} is free", "md": "Sir is free"}[whose]
        free_hi = {"self": "आप", "pa": pa_name, "md": "सर"}[whose]
        if d:
            head_en = f"{address}, {item} {day_en}: {free} {s_en}–{e_en}. Shall I put it at {s_en}?"
            head_hi = f"{address_hi}, {day_hi} {free_hi} {s_hi} से {e_hi} तक फ्री हैं। {s_hi} रख दूँ?"
        else:
            head_en = f"{address}, {item}: {free} {tu.fmt_day(s_day)} {s_en}–{e_en}. Shall I put it then?"
            head_hi = f"{address_hi}, {tu.fmt_day_hi(s_day)} {free_hi} {s_hi} से {e_hi} तक फ्री हैं। तब रख दूँ?"
        question_en = _insert_notes(head_en, notes_en)
        question_hi = _insert_notes(head_hi, notes_hi, hindi=True)

    draft["schedule"] = {
        "for": whose,
        "asked": asked,
        "question": question_en,
        "question_hi": question_hi,
        "notes": notes_en,
        "notes_hi": notes_hi,
        "clashes": [{"title": "" if anonymous else c["title"], "start": c["start"].isoformat(), "end": c["end"].isoformat(),
                     "label": f"{tu.fmt_time(c['start'])}–{tu.fmt_time(c['end'])}"} for c in clashes[:3]],
        "suggested": ({"due_date": sug["due_date"], "due_time": sug["due_time"], "label": alt_en} if sug else None),
        "load": {"count": load.get("count", 0), "heavy": bool(load.get("heavy"))} if load else None,
        "off_day": bool(review.get("off_day")),
        "base_reply": draft.get("spoken_reply", ""),
    }
    draft["warnings"] = list(draft.get("warnings") or []) + warnings
    if question_en:
        draft["spoken_reply"] = question_en
    elif notes_en and draft.get("spoken_reply"):
        draft["spoken_reply"] = _insert_notes(draft["spoken_reply"], notes_en)


def _rebuilt(pending: dict, new: dict) -> dict:
    """The waiting card with a new day / time, ready to be looked at again."""
    trial = {k: v for k, v in pending.items()}
    if trial.get("action") == "change_task":
        trial["changes"] = {**(trial.get("changes") or {}), **new}
    else:
        trial.update(new)
        trial["due_label"] = _due_label(trial.get("due_date", ""), trial.get("due_time", ""))
    trial.update({"needs_clarification": False, "question": "", "schedule": None, "warnings": [], "overrides": {},
                  "done_reply": "", "done_speak": "", "done_lang": "en"})
    return trial


def _confirm_with_pending(user: User, cmd: dict, pending: dict | None, address: str, draft: dict) -> dict:
    """A yes while a card waits. A plain yes takes the slot JARVIS offered (if it offered one); a time in the reply
    replaces the card's; keep_original saves it exactly as first said. Returns the draft to send back, which is a
    fresh question when the new time collides as well (asked at most twice)."""
    draft["overrides"] = {}
    if not pending:
        return draft
    sched = pending.get("schedule") or {}
    if cmd.get("keep_original"):
        return draft
    d, t = _safe_date(str(cmd.get("due_date") or "")), _safe_time(str(cmd.get("due_time") or ""))
    offered = sched.get("suggested") or {}
    if not d and not t:
        if _safe_date(str(offered.get("due_date") or "")) and _safe_time(str(offered.get("due_time") or "")):
            draft["overrides"] = {"due_date": offered["due_date"], "due_time": offered["due_time"]}
        return draft
    is_change = pending.get("action") == "change_task"
    base = (pending.get("changes") or {}) if is_change else pending
    cur_date = _safe_date(str(base.get("due_date") or "")) or _safe_date(str(offered.get("due_date") or "")) \
        or tu.today_local().isoformat()
    cur_time = _safe_time(str(base.get("due_time") or ""))
    new = {"due_date": d or cur_date}
    if t or cur_time:
        new["due_time"] = t or cur_time
    draft["overrides"] = new
    asked = int(sched.get("asked") or 0)
    if new.get("due_time") and asked < MAX_SCHEDULE_QUESTIONS and pending.get("action") in _SLOT_ACTIONS:
        trial = _rebuilt(pending, new)
        _advise_schedule(user, trial, address, asked=asked + 1)
        if (trial.get("schedule") or {}).get("clashes") and trial["schedule"].get("question"):
            lang = pending.get("reply_language") if pending.get("reply_language") in {"en", "hi"} else "en"
            trial["reply_language"] = lang
            hi = trial["schedule"]["question_hi"]
            trial["speak_text"], trial["speak_lang"] = (hi, "hi") if lang == "hi" and hi else (trial["spoken_reply"], "en")
            return trial
    return draft


def build_draft(user: User, text: str, previous: str = "", history: list | None = None,
                awaiting: str = "", choices: dict | None = None, include_private: bool = True,
                pending: dict | None = None) -> dict:
    """awaiting: short description of a draft JARVIS has just read back and is waiting to have confirmed.
    choices: {"intent": ..., "items": [{"id", "title"}]} when JARVIS just asked "which one?" — the text is the answer.
    include_private=False: phone locked; the MD's private items are kept out of the model's view.
    pending: the waiting card itself (so a yes can take the time JARVIS offered, or a new time can be checked)."""
    full_text = f"{previous}. {text}".strip(". ") if previous else text
    card = _clean_pending(pending) if awaiting else None  # the waiting card (not to be confused with pending requests)
    ctx = services.ai_context(user, include_private=include_private)
    # The last few turns of THIS conversation, so follow-ups like "aur Neha ko bhi" make sense.
    ctx["conversation"] = ai_brain.history_text(history)
    model_text = full_text
    if choices and choices.get("items"):
        ctx["choices"] = {"intent": choices["intent"], "items": [{"id": i["id"], "title": i["title"]} for i in choices["items"][:10]]}
        model_text = text  # the answer on its own; the choices carry the question
    if awaiting:
        # The reply goes to the model on its own (so a side question is not glued onto the draft);
        # the draft and its original sentence are background.
        ctx["awaiting_confirmation"] = {"draft": awaiting[:400], "original_sentence": previous[:1000]}
        sched = (card or {}).get("schedule") or {}
        if sched.get("question"):
            base = card.get("changes") if card.get("action") == "change_task" else card
            ctx["awaiting_confirmation"]["jarvis_asked"] = str(sched["question"])[:300]
            ctx["awaiting_confirmation"]["original_time"] = {"due_date": base.get("due_date", ""), "due_time": base.get("due_time", "")}
            if sched.get("suggested"):
                ctx["awaiting_confirmation"]["offered_time"] = {"due_date": sched["suggested"].get("due_date", ""),
                                                                "due_time": sched["suggested"].get("due_time", "")}
        model_text = text
    staff_names = [s["name"] for s in ctx["staff"]]
    pending = ctx.get("pending_requests", [])
    cmd = ai_brain.interpret(model_text, ctx, staff_names, has_pending_requests=bool(pending), user=user)
    if awaiting and cmd.get("engine") == "rules" and not hinglish_parser.yes_no(text):
        # Offline parser has no context: re-read the change together with the original sentence.
        cmd = ai_brain.interpret(full_text, ctx, staff_names, has_pending_requests=bool(pending), user=user)
    if awaiting and cmd.get("engine") == "rules":
        # Offline: only a plain short yes / no is understood; anything else is treated as a change.
        answer = hinglish_parser.yes_no(text)
        if answer:
            cmd["intent"] = "confirm_pending" if answer == "yes" else "cancel_pending"
    if cmd.get("intent") in {"confirm_pending", "cancel_pending"} and not awaiting:
        cmd["intent"] = "unknown"  # nothing is waiting to be confirmed
    if choices and choices.get("items") and cmd.get("target_refs") and cmd.get("intent") in {"unknown", "query", "small_talk"}:
        cmd["intent"] = choices["intent"]  # the model picked items but forgot to carry the pending intent

    # While a draft waits, a change keeps the original sentence (it becomes the task description);
    # a side question or a brand-new topic stands on its own.
    if awaiting and cmd.get("intent") not in _TASK_INTENTS:
        full_text = text
    address = "Sir" if user.is_md else user.spoken_name
    intent = _route_intent(user, cmd, full_text)

    due_date = _safe_date(cmd.get("due_date", ""))
    due_time = _safe_time(cmd.get("due_time", ""))
    if due_time and not due_date:
        due_date = tu.today_local().isoformat()
    kind = cmd.get("kind") if cmd.get("kind") in _KINDS else Kind.TASK
    priority = cmd.get("priority") if cmd.get("priority") in _PRIORITIES else Priority.NORMAL

    draft = {
        "engine": cmd.get("engine", "rules"),
        "intent": intent,
        "transcript": full_text,
        "action": "answer",
        "title": (cmd.get("title") or "").strip()[:200],
        "notes": (cmd.get("notes") or "").strip()[:2000],
        "kind": kind,
        "priority": priority,
        "due_date": due_date,
        "due_time": due_time,
        "due_label": _due_label(due_date, due_time),
        "duration_minutes": max(0, int(cmd.get("duration_minutes") or 0)) or None,
        "remind_before_minutes": max(0, int(cmd.get("remind_before_minutes") or 0)) or None,
        "announcement": (cmd.get("reminder_line") or "").strip()[:300],
        "staff": None,
        "staff_candidates": [],
        "category_key": "",
        "categories": [],
        "warnings": [],
        "pa_name": "",
        "team": None,
        "target": None,
        "targets": [],          # every item this applies to (complete / accept / decline / change / delete can take several)
        "target_candidates": [],
        "changes": {},          # change_task: the new values
        "schedule": None,       # diary check: clash / free slot / full day (see _advise_schedule)
        "overrides": {},        # confirm_pending: day / time to apply to the waiting card before saving
        "view": None,           # data the phone draws for this answer (timeline / chart / table), see datacards
        "setting_key": "",      # set_setting
        "setting_value": "",
        "needs_clarification": bool(cmd.get("needs_clarification")),
        "question": (cmd.get("clarification_question") or "").strip(),
        "spoken_reply": (cmd.get("spoken_reply") or "").strip(),
        "ask_text": "",       # general question for /assistant/ask/
        "memory_text": "",    # fact to remember (after the user confirms)
        "reply_language": cmd.get("reply_language") if cmd.get("reply_language") in {"en", "hi"} else "en",
        "speak_text": "",     # what the voice says (Devanagari when the reply is Hindi)
        "speak_lang": "en",
        "done_reply": (cmd.get("done_reply") or "").strip()[:300],  # said after a successful save
        "done_speak": "",
        "done_lang": "en",
    }

    if intent == "create_task":
        draft["action"] = "private_task"
    elif intent == "request_for_md":
        draft["action"] = "md_request"
    elif intent == "assign_to_staff":
        draft["action"] = "delegation"
        _prepare_delegation(draft, cmd, user, address)
    elif intent == "assign_to_team":
        draft["action"] = "team_delegation"
        _prepare_team(draft, cmd, address)
    elif intent in {"assign_to_pa", "meet_with_pa"}:
        draft["action"] = "pa_meeting" if intent == "meet_with_pa" else "pa_task"
        if intent == "meet_with_pa":
            draft["kind"] = Kind.MEETING
        pa = services.get_pa()
        draft["pa_name"] = pa.spoken_name if pa else ""
        if not pa:
            draft["needs_clarification"] = True
            draft["question"] = f"{address}, there is no PA account set up yet."
        elif intent == "meet_with_pa" and not draft["title"].lower().startswith("meeting"):
            draft["title"] = draft["title"] or f"Meeting with {pa.spoken_name}"
    elif intent == "needs_destination":
        draft["action"] = "choose_destination"
        draft["needs_clarification"] = True
        draft["question"] = draft["question"] or (
            f"{address}, should I add this to your personal tasks, give it to your PA, or assign it to someone in the team?"
            if user.is_md else f"{address}, should I send this to Sir, or assign it to someone in the team?"
        )
    elif intent in {"accept_request", "decline_request"}:
        draft["action"] = intent
        ids = {r["id"]: r for r in pending}
        chosen = _chosen(cmd, ids)
        verb = "accept" if intent == "accept_request" else "decline"
        if chosen:
            draft["targets"] = chosen
        elif len(pending) == 1:
            draft["targets"] = [pending[0]]
        elif pending:
            draft["needs_clarification"] = True
            draft["target_candidates"] = pending[:5]
            draft["question"] = f"{address}, which request? " + "; ".join(r["title"] for r in pending[:3]) + ". Or all of them?"
        else:
            draft["action"] = "answer"
            draft["spoken_reply"] = f"{address}, there are no pending requests right now."
        if draft["targets"]:
            draft["target"] = draft["targets"][0]
            if len(draft["targets"]) == 1:
                draft["spoken_reply"] = f"{address}, {verb} the request: {draft['target']['title']}?"
            else:
                draft["spoken_reply"] = (f"{address}, {verb} these {len(draft['targets'])} requests: "
                                         + "; ".join(t["title"] for t in draft["targets"]) + "?")
    elif intent == "complete_task":
        draft["action"] = "complete"
        open_items = ctx["open_items"]["overdue"] + ctx["open_items"]["today"] + ctx["open_items"]["upcoming"]
        by_id = {i["id"]: i for i in open_items}
        chosen = _chosen(cmd, by_id)
        matches = chosen or _match_items(draft["title"] or full_text, open_items)
        if chosen or len(matches) == 1:
            draft["targets"] = matches
            draft["target"] = matches[0]
            if len(matches) == 1:
                draft["spoken_reply"] = f"{address}, mark '{matches[0]['title']}' as done?"
            else:
                draft["spoken_reply"] = (f"{address}, mark these {len(matches)} as done: "
                                         + "; ".join(m["title"] for m in matches) + "?")
        else:
            draft["needs_clarification"] = True
            draft["target_candidates"] = matches[:5]
            draft["question"] = (f"{address}, which one did you finish: " + "; ".join(m["title"] for m in matches[:3])
                                 + ". Or both?" if matches else f"{address}, which task did you finish?")
    elif intent in EDIT_ACTIONS:
        draft["action"] = intent
        open_items = ctx["open_items"]["overdue"] + ctx["open_items"]["today"] + ctx["open_items"]["upcoming"]
        by_id = {i["id"]: i for i in open_items}
        chosen = _chosen(cmd, by_id)
        matches = chosen or _match_items(full_text, open_items)
        if chosen or len(matches) == 1:
            draft["targets"] = matches
            draft["target"] = matches[0]
            names = "; ".join(m["title"] for m in matches)
            if intent == "change_task":
                draft["changes"] = _changes_from(cmd)
                if not draft["changes"]:
                    draft["needs_clarification"] = True
                    draft["question"] = f"{address}, what should I change about '{names}': the time, the day, the priority or the name?"
                else:
                    draft["spoken_reply"] = f"{address}, {describe_changes(draft['changes'])} for '{names}'?"
            else:
                # Deleting always asks first. It is a cancel, not an erase, so it can be undone from the app.
                draft["spoken_reply"] = (f"{address}, delete '{names}'? It will be cancelled, not erased."
                                         if len(matches) == 1 else f"{address}, delete these {len(matches)}: {names}?")
        else:
            draft["needs_clarification"] = True
            draft["target_candidates"] = matches[:5]
            verb = "change" if intent == "change_task" else "delete"
            draft["question"] = (f"{address}, which one should I {verb}: " + "; ".join(m["title"] for m in matches[:3]) + "?"
                                 if matches else f"{address}, which task should I {verb}?")
    elif intent == "set_setting":
        draft["action"] = "set_setting"
        key = (cmd.get("setting_key") or "").strip()
        value = (cmd.get("setting_value") or "").strip()
        try:
            patch = services.expand_pref_patch({key: value}, services.get_prefs(user)) if key else {}
            clean = dict(services.coerce_pref(k, v) for k, v in patch.items())
            if not clean:
                raise services.ServiceError("invalid", "Which setting should I change?", 422)
            draft["setting_key"] = key
            draft["setting_value"] = value
            draft["setting_patch"] = clean
            if not draft["spoken_reply"]:
                what = ", ".join(services.describe_pref(k, v) for k, v in clean.items())
                draft["spoken_reply"] = f"Done {address}: {what}."
        except services.ServiceError as err:
            draft["needs_clarification"] = True
            draft["question"] = f"{address}, {err.message}"
    elif intent == "briefing":
        draft["action"] = "briefing"
        draft["spoken_reply"] = draft["spoken_reply"] or f"Here is your briefing, {address}."
        draft["view"] = datacards.build(user, "today", include_private)
    elif intent == "general_question":
        # Answered by /assistant/ask/ (web search). The answering step asks for missing details itself.
        draft["action"] = "ask"
        draft["ask_text"] = ((cmd.get("question") or "").strip() or full_text)[:1000]
        draft["needs_clarification"] = False
        draft["question"] = ""
        draft["spoken_reply"] = draft["spoken_reply"] or f"One moment, {address}."
    elif intent == "remember":
        draft["action"] = "remember"
        draft["memory_text"] = " ".join((cmd.get("memory_text") or "").split())[:300]
        if not draft["memory_text"]:
            draft["needs_clarification"] = True
            draft["question"] = f"{address}, what should I remember?"
        elif not draft["spoken_reply"]:
            draft["spoken_reply"] = f"{address}, I'll remember this: {draft['memory_text']} Okay?"
    elif intent == "confirm_pending":
        draft["action"] = "confirm_pending"  # the phone saves the draft it is holding
        draft["needs_clarification"] = False
        draft["question"] = ""
        result = _confirm_with_pending(user, cmd, card, address, draft)
        if result is not draft:
            return result  # the new time collides too: a fresh question, same card
    elif intent == "cancel_pending":
        draft["action"] = "cancel_pending"
        draft["needs_clarification"] = False
        draft["question"] = ""
        draft["spoken_reply"] = draft["spoken_reply"] or f"Okay {address}, cancelled. Nothing was saved."
    elif intent == "dismiss_alert":
        draft["action"] = "dismiss"  # the phone clears the alert on screen
        draft["needs_clarification"] = False
        draft["question"] = ""
        draft["spoken_reply"] = draft["spoken_reply"] or "Done."
    elif intent == "end_conversation":
        draft["action"] = "end"
        draft["needs_clarification"] = False
        draft["question"] = ""
        draft["spoken_reply"] = draft["spoken_reply"] or f"Okay {address}. Just say Hello JARVIS whenever you need me."
    else:
        draft["action"] = "answer"
        if intent == "query":
            # The spoken answer is short; the screen gets the data (timeline, chart or table) that fits the question.
            kind = cmd.get("view_kind") if cmd.get("view_kind") in datacards.VIEW_KINDS else datacards.guess_kind(full_text)
            draft["view"] = datacards.build(user, kind, include_private)
        if not draft["spoken_reply"]:
            if intent == "query":
                today = ctx["open_items"]["today"]
                overdue = ctx["open_items"]["overdue"]
                if today:
                    firsts = ", ".join(f"{i['title']} {i['due_label']}".strip() for i in today[:3])
                    draft["spoken_reply"] = f"{address}, today you have {len(today)} item{'s' if len(today) > 1 else ''}: {firsts}."
                else:
                    draft["spoken_reply"] = f"{address}, nothing is scheduled for today."
                if overdue:
                    draft["spoken_reply"] += f" {len(overdue)} overdue."
            else:
                draft["spoken_reply"] = f"Sorry {address}, I didn't catch that. Please say it again or type it."

    if draft["action"] in TASK_ACTIONS and draft["due_date"] and draft["due_time"]:
        when = tu.combine_local(date.fromisoformat(draft["due_date"]), time.fromisoformat(draft["due_time"]))
        if when <= tu.now_local() and not draft["needs_clarification"]:
            draft["needs_clarification"] = True
            label = tu.fmt_time(when)
            draft["question"] = (f"{address}, {label} {tu.fmt_day(when.date())} has already passed. "
                                 f"Tomorrow at {label}, or a different time?")

    if draft["action"] in TASK_ACTIONS:
        if not draft["title"]:
            draft["needs_clarification"] = True
            draft["question"] = draft["question"] or f"{address}, what should the task say?"
        pa_name = draft.get("pa_name") or "your PA"
        if draft["action"] == "pa_meeting":
            # Always state clearly that the PA will be informed.
            when = f" {draft['due_label']}" if draft["due_label"] else ""
            draft["spoken_reply"] = f"{address}, meeting with {pa_name}{when}. I'll inform {pa_name} too. Shall I save it?"
        elif draft["action"] != "team_delegation" and (not draft["spoken_reply"] or draft["engine"] == "rules"):
            what = {
                "private_task": "Personal task",
                "md_request": "Request for MD",
                "delegation": f"Assign to {draft['staff']['name']}" if draft["staff"] else "Assign to staff",
                "pa_task": f"Task for {pa_name}",
            }[draft["action"]]
            bits = [b for b in (draft["due_label"], f"{priority.lower()} priority" if priority != "NORMAL" else "") if b]
            draft["spoken_reply"] = f"{address}, {what}: {draft['title']}" + (f", {', '.join(bits)}" if bits else "") + ". Shall I save it?"

    # Does it fit the diary? (clash, free slot, a very full day) — may replace the question above.
    if draft["action"] in _SLOT_ACTIONS and not draft["needs_clarification"]:
        _advise_schedule(user, draft, address)

    if draft["needs_clarification"] and draft["question"]:
        draft["spoken_reply"] = draft["question"]

    # Voice text: the model's Devanagari version is used only when the final reply is still the model's own
    # sentence (server-built confirmations are English and are spoken as written).
    hindi = (cmd.get("spoken_reply_hi") or "").strip()
    sched = draft.get("schedule") or {}
    model_reply = (cmd.get("spoken_reply") or "").strip()
    base_reply = sched.get("base_reply", draft["spoken_reply"]) if sched.get("notes_hi") and not sched.get("question") else draft["spoken_reply"]
    if draft["reply_language"] == "hi" and sched.get("question_hi"):
        draft["speak_text"], draft["speak_lang"] = sched["question_hi"], "hi"
    elif draft["reply_language"] == "hi" and hindi and _DEVANAGARI.search(hindi) and base_reply == model_reply:
        draft["speak_text"], draft["speak_lang"] = _insert_notes(hindi, sched.get("notes_hi") or [], hindi=True), "hi"
    else:
        draft["speak_text"], draft["speak_lang"] = draft["spoken_reply"], "en"
    done_hi = (cmd.get("done_reply_hi") or "").strip()
    if draft["done_reply"]:
        use_hi = draft["reply_language"] == "hi" and bool(_DEVANAGARI.search(done_hi))
        draft["done_speak"], draft["done_lang"] = (done_hi, "hi") if use_hi else (draft["done_reply"], "en")
    # Fixed Hindi lines for the two short replies the model sometimes leaves without a Devanagari version.
    if draft["reply_language"] == "hi" and draft["speak_lang"] != "hi":
        sir = " सर" if user.is_md else ""
        if draft["action"] == "ask":
            draft["speak_text"], draft["speak_lang"] = f"एक सेकंड{sir}, देख रहा हूँ।", "hi"
        elif draft["action"] == "end":
            draft["speak_text"], draft["speak_lang"] = f"ठीक है{sir}। जब भी ज़रूरत हो, बस हेलो जार्विस बोलिए।", "hi"
    return draft
