"""Turns one instruction into a draft the user confirms on screen.

Nothing here writes business data. The draft says what WOULD happen; the phone
then calls the normal, permission-checked endpoint with an Idempotency-Key.
"""
import difflib
import re
from datetime import date, time, timedelta

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist

from . import ai_brain, datacards, hinglish_parser, scheduling, services
from . import lang as lang_mod
from . import timeutils as tu
from .models import Kind, Priority, PrivateTask, SharedTask, TaskCategory, User
from .taskpanel.client import TaskPanelError, get_client as tp_client
from .taskpanel.contracts import ALL_STAFF_WORDS, build_validate_body

TASK_ACTIONS = {"private_task", "md_request", "delegation", "pa_task", "pa_meeting", "team_delegation", "pa_self_task"}
EDIT_ACTIONS = {"change_task", "delete_task"}
_CHANGE_FIELDS = {"title", "due_date", "due_time", "priority", "kind"}
# Drafts that put something in somebody's diary, and whose diary it is: the speaker's own, the PA's
# (Sir giving Mansi work) or the MD's (Mansi asking for Sir's time; she only ever hears "busy", never why).
_SLOT_ACTIONS = {"private_task": "self", "pa_meeting": "self", "pa_task": "pa", "md_request": "md", "change_task": "self",
                 "pa_self_task": "self"}
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


def _due_label_hi(due_date: str, due_time: str) -> str:
    if not due_date:
        return ""
    d = date.fromisoformat(due_date)
    if due_time:
        return tu.due_phrase_hi("TIMED", d, tu.combine_local(d, time.fromisoformat(due_time)))
    return tu.fmt_day_hi(d)


_PRIORITY_HI = {"URGENT": "अर्जेंट", "HIGH": "हाई", "NORMAL": "नॉर्मल", "LOW": "लो"}
_KIND_HI = {"TASK": "टास्क", "MEETING": "मीटिंग", "CALL": "कॉल", "PAYMENT": "पेमेंट", "FOLLOW_UP": "फ़ॉलो-अप"}


def _say(draft: dict, en: str, hi: str = "") -> None:
    """A reply the server wrote, with its Hindi twin: the twin is what the voice says when the speaker's language
    is Hindi (see lang.py), the English line is what the screen shows and the English voice says."""
    draft["spoken_reply"], draft["reply_en"], draft["reply_hi"] = en, en, hi


def _ask(draft: dict, en: str, hi: str = "") -> None:
    """A question the server itself needs answered, with its Hindi twin."""
    draft["needs_clarification"], draft["question"], draft["question_hi"] = True, en, hi


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


def describe_changes_hi(changes: dict) -> str:
    """The Hindi twin of describe_changes, as a question stem: "कल 4 बजे पर ले जाऊँ", "प्रायोरिटी हाई कर दूँ"."""
    bits = []
    if "title" in changes:
        bits.append(f"नाम '{changes['title']}' कर दूँ")
    if "due_date" in changes or "due_time" in changes:
        d = changes.get("due_date")
        t = changes.get("due_time")
        when = _due_label_hi(d, t) if d else (tu.fmt_time_hi(tu.combine_local(tu.today_local(), time.fromisoformat(t))) if t else "")
        bits.append(f"{when} पर ले जाऊँ" if d else f"समय {when} कर दूँ")
    if "priority" in changes:
        bits.append(f"प्रायोरिटी {_PRIORITY_HI.get(changes['priority'], changes['priority'].lower())} कर दूँ")
    if "kind" in changes:
        bits.append(f"टाइप {_KIND_HI.get(changes['kind'], changes['kind'].lower())} कर दूँ")
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


_FOR_SIR = re.compile(r"\b(sir|md|boss|sahab|saab)\b", re.IGNORECASE)
# Mansi speaking about herself ("mera reminder", "I have to", "mujhe yaad dilana").
_PA_OWN = re.compile(
    r"\b(mera|meri|mere|mujhe|mujhko|maine|khud|apna|apni|apne|personal|my|mine|myself|for me|remind me|"
    r"i have to|i need to|i will|i'll|i must)\b", re.IGNORECASE)


def _last_cue(pattern: re.Pattern, text: str) -> int:
    """Where the pattern was last said (-1 if never): in "…. mere liye" the answer comes last and wins."""
    found = [m.start() for m in pattern.finditer(text)]
    return found[-1] if found else -1


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
        # Whose is it? For Sir, her own, or the team: it is never assumed. The model judges the meaning
        # ("Sir ki cab book karni hai" is HER job); the words must back it up, otherwise JARVIS asks.
        sir_at, own_at = _last_cue(_FOR_SIR, text), _last_cue(_PA_OWN, text)
        for_sir, own = sir_at >= 0, own_at >= 0
        if intent == "meet_with_pa":
            intent = "request_for_md"
        elif intent == "create_task" and cmd.get("engine") == "rules":
            # The offline parser cannot judge meaning: her words decide, the last one said wins.
            intent = "needs_destination" if not (own or for_sir) else "pa_own" if own_at > sir_at else "request_for_md"
        elif intent == "create_task":       # the model says it is her own
            intent = "pa_own" if own or for_sir else "needs_destination"
        elif intent == "request_for_md" and not for_sir:
            intent = "pa_own" if own else "needs_destination"
        if intent in {"accept_request", "decline_request", "assign_to_pa"}:
            intent = "unknown"
    elif intent == "request_for_md":
        intent = "create_task"
    return intent


def _prepare_team(draft: dict, cmd: dict, address: str, address_hi: str):
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
        if not label:
            _ask(draft, f"{address}, I couldn't find anyone in that team. Which team or person should do this?",
                 f"{address_hi}, उस टीम में कोई नहीं मिला। ये किस टीम या किस व्यक्ति को देना है?")
        else:
            _ask(draft, f"{address}, everyone in {label} is on leave. Who else should do this?",
                 f"{address_hi}, {label} में सब छुट्टी पर हैं। ये किसे देना है?")
        return
    skipped_txt = f" {len(skipped)} on leave will be skipped." if skipped else ""
    skipped_hi = f" {len(skipped)} छुट्टी पर हैं, वो छूट जाएँगे।" if skipped else ""
    due_hi = _due_label_hi(draft["due_date"], draft["due_time"])
    _say(draft,
         f"{address}, this will go to {len(members)} {'person' if len(members) == 1 else 'people'} in {label}: {draft['title']}"
         + (f", {draft['due_label']}" if draft["due_label"] else "") + f".{skipped_txt} Shall I assign it?",
         f"{address_hi}, ये {label} के {len(members)} लोगों को जाएगा: {draft['title']}"
         + (f", {due_hi}" if due_hi else "") + f"।{skipped_hi} असाइन कर दूँ?")


def _staff_option(s) -> dict:
    return {"id": s.id, "name": s.name, "department": s.department, "on_leave": s.on_leave,
            "leave_until": s.leave_until.isoformat() if s.leave_until else None}


def _prepare_delegation(draft: dict, cmd: dict, user: User, address: str, address_hi: str):
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
        options = " or ".join(f"{s.name} from {s.department}" for s in matches[:3])
        options_hi = " या ".join(f"{s.department} वाले {s.name}" for s in matches[:3])
        _ask(draft, f"{address}, which {name}: {options}?", f"{address_hi}, कौन से {name}: {options_hi}?")
        return
    elif suggestions:
        draft["staff_candidates"] = [_staff_option(s) for s in suggestions]
        if len(suggestions) == 1:
            _ask(draft, f"{address}, did you mean {suggestions[0].name} from {suggestions[0].department}?",
                 f"{address_hi}, आपका मतलब {suggestions[0].department} वाले {suggestions[0].name} से था?")
        else:
            _ask(draft, f"{address}, I couldn't find {name}. Did you mean " + " or ".join(s.name for s in suggestions[:3]) + "?",
                 f"{address_hi}, {name} नहीं मिले। आपका मतलब " + " या ".join(s.name for s in suggestions[:3]) + " से था?")
        return
    else:
        if name:
            _ask(draft, f"{address}, I couldn't find {name} in the TaskPanel staff list. Who should do this?",
                 f"{address_hi}, {name} TaskPanel की स्टाफ़ लिस्ट में नहीं मिले। ये किसे देना है?")
        else:
            _ask(draft, f"{address}, who should I assign this to?", f"{address_hi}, ये किसे असाइन करूँ?")
        return

    if person.on_leave:
        until = tu.fmt_day(person.leave_until) if person.leave_until else "further notice"
        until_hi = tu.fmt_day_hi(person.leave_until) if person.leave_until else "अगली सूचना"
        draft["warnings"].append(f"{person.name} is on leave until {until}.")
        _ask(draft, f"{address}, {person.name} is on leave until {until}. Should someone else do this?",
             f"{address_hi}, {person.name} {until_hi} तक छुट्टी पर हैं। किसी और को दे दूँ?")
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
    anonymous = whose == "md"  # the PA only ever hears that the MD is busy, never with what
    pa = services.get_pa()
    pa_name = pa.spoken_name if pa else "your PA"
    address_hi = services.address_hi_of(user)
    sir, sir_hi = services.md_address(), services.md_address_hi()
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
        who = {"self": "", "pa": f"{pa_name} ", "md": f"{sir} "}[whose]
        notes_en.append(f"{who}{day_en if who else day_cap} already has {n} items.")
        notes_hi.append(f"{day_hi} पहले से {n} काम हैं।")
        warnings.append(f"{day_cap} already has {n} items")
    if review.get("off_day"):
        notes_en.append(f"{day_cap} is a weekly off.")
        notes_hi.append(f"{day_hi} छुट्टी का दिन है।")
        warnings.append(f"{day_cap} is a weekly off")

    question_en = question_hi = ""
    clashes = review.get("clashes") or []
    if whose == "self" and draft["action"] != "change_task" and d:
        twin = _similar_item(subject, draft.get("title", ""), d, {c.get("id") for c in clashes})
        if twin:
            notes_en.append(f"You already have {twin} {day_en}.")
            notes_hi.append(f"{twin} {day_hi} पहले से है।")
            warnings.append(f"Similar item already on that day: {twin}")
    if clashes:
        c = clashes[0]
        span = f"{tu.fmt_time(c['start'])}–{tu.fmt_time(c['end'])}"
        if anonymous:
            head_en = f"{address}, {item} {when_en}: {sir} is busy then ({span})."
            head_hi = f"{address_hi}, {when_hi} {item}: {sir_hi} उस समय बिज़ी हैं।"
            warnings.append(f"{sir} is busy {span}")
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
        free = {"self": "you're free", "pa": f"{pa_name} is free", "md": f"{sir} is free"}[whose]
        free_hi = {"self": "आप", "pa": pa_name, "md": sir_hi}[whose]
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


def _similar_item(user: User, title: str, day: date, skip_ids=()) -> str:
    """The title of an open item on that day that looks like the same thing ("Meeting with Sharma ji" twice)."""
    want = " ".join(re.findall(r"\w+", (title or "").lower()))
    if len(want) < 6:
        return ""
    for item in scheduling.commitments(user, day) + scheduling.dated_items(user, day):
        if item.get("id") in skip_ids:
            continue
        have = " ".join(re.findall(r"\w+", item["title"].lower()))
        if difflib.SequenceMatcher(None, want, have).ratio() >= 0.8:
            return item["title"]
    return ""


def _person_label(name: str) -> str:
    name = " ".join((name or "").split())
    return name.title() if name == name.lower() else name


def _check_people(user: User, cmd: dict, draft: dict, address: str, history: list | None) -> None:
    """Every person named must be someone JARVIS knows for this speaker: staff, the PA, a saved contact, or a
    name already in their own tasks. Otherwise ask who they are (once), offer the nearest known name, and
    remember the answer. Whatever the speaker explains about a person is saved as a contact straight away."""
    saved = []
    for c in (cmd.get("new_contacts") or [])[:3]:
        if isinstance(c, dict) and str(c.get("name") or "").strip():
            try:
                services.add_contact(user, str(c["name"]), str(c.get("note") or ""))
                saved.append(_person_label(str(c["name"])))
            except services.ServiceError:
                pass
    # The person doing the work is resolved by the delegation step (it has its own "which Rahul?" question).
    doer = services.norm_person(cmd.get("staff_name") or "") if draft["action"] == "delegation" else ""
    names: list[str] = []
    for n in (cmd.get("people") or [])[:4]:
        if isinstance(n, str) and n.strip() and n.strip().lower() not in {x.lower() for x in names} \
                and not (doer and services.norm_person(n) == doer):
            names.append(n.strip())
    draft["people"] = names
    draft["contacts_saved"] = saved
    # A question the server itself built (which staff member, nobody in that team, no PA yet) is answered first.
    model_question = (cmd.get("clarification_question") or "").strip()
    if draft["needs_clarification"] and draft["action"] != "choose_destination" and draft["question"] != model_question:
        return
    last_jarvis = " ".join(str(t.get("text", "")) for t in (history or [])[-3:] if t.get("role") == "jarvis").lower()
    address_hi = services.address_hi_of(user)
    sir, sir_hi = services.md_address(), services.md_address_hi()
    for name in names:
        label = _person_label(name)
        found = services.resolve_person(user, name)
        if found["status"] == "known":
            continue
        if found["status"] == "ambiguous":
            if f"which {label}".lower() in last_jarvis:
                continue  # asked already; the answer is in the sentence now, do not ask in a loop
            draft["needs_clarification"] = True
            draft["question"] = f"{address}, which {label}: {' or '.join(found['options'])}?"
            draft["question_hi"] = f"{address_hi}, कौन से {label}: {' या '.join(found['options'])}?"
            return
        if f"don't know any {label}".lower() in last_jarvis or f"किसी {label} को नहीं जानता".lower() in last_jarvis:
            # Asked once already and the speaker carried on: take the name as an outside contact and move on.
            try:
                services.add_contact(user, label, "")
                saved.append(label)
            except services.ServiceError:
                pass
            continue
        hint = f" Or did you mean {found['suggestions'][0]}?" if found["suggestions"] else ""
        hint_hi = f" या आपका मतलब {found['suggestions'][0]} था?" if found["suggestions"] else ""
        whose = whose_hi = ""
        if draft["action"] == "choose_destination":
            whose = (" And should this go in your personal tasks, to your PA, or to the team?" if user.is_md
                     else f" And is this for {sir}, for you, or for someone in the team?")
            whose_hi = (" और इसे आपके पर्सनल में रखूँ, PA को दूँ, या टीम को?" if user.is_md
                        else f" और ये {sir_hi} के लिए है, आपके लिए, या टीम में किसी के लिए?")
        draft["needs_clarification"] = True
        draft["question"] = f"{address}, I don't know any {label}. Is {label} on the staff, or an outside contact?{hint}{whose}"
        draft["question_hi"] = (f"{address_hi}, मैं किसी {label} को नहीं जानता। क्या {label} स्टाफ़ में हैं, "
                                f"या बाहर के कॉन्टैक्ट?{hint_hi}{whose_hi}")
        return
    if saved:
        draft["warnings"] = list(draft.get("warnings") or []) + [f"Saved as a contact: {', '.join(saved)}"]


def _rebuilt(pending: dict, new: dict) -> dict:
    """The waiting card with a new day / time, ready to be looked at again."""
    trial = {k: v for k, v in pending.items()}
    if trial.get("action") == "change_task":
        trial["changes"] = {**(trial.get("changes") or {}), **new}
    else:
        trial.update(new)
        trial["due_label"] = _due_label(trial.get("due_date", ""), trial.get("due_time", ""))
    trial.update({"needs_clarification": False, "question": "", "question_hi": "", "schedule": None, "warnings": [],
                  "overrides": {}, "done_reply": "", "done_speak": "", "done_lang": "en"})
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


_SCREEN_ACTIONS = {"export_pdf", "export_csv", "share", "open_item", "open_settings", "go_home"}


def _clean_screen(screen) -> dict | None:
    """What the app says is on its screen (a data card): untrusted, so only short plain fields are kept. The
    card's filters (the request it was built from) come along, so the next question can narrow the same list."""
    if not isinstance(screen, dict):
        return None
    items = []
    for it in (screen.get("items") if isinstance(screen.get("items"), list) else [])[:15]:
        if isinstance(it, dict) and it.get("id") and it.get("title"):
            items.append({"id": str(it["id"])[:64], "title": str(it["title"])[:120]})
    kind = str(screen.get("kind") or "")[:20]
    if not kind:
        return None
    out = {"kind": kind, "title": str(screen.get("title") or "")[:80], "items": items}
    filters = screen.get("filters")
    if isinstance(filters, dict):
        from . import listviews
        req_kind = str(filters.get("kind") or kind)[:20]
        out["filters"] = listviews.request_of(req_kind if req_kind in datacards.VIEW_KINDS else kind, listviews.clean_params(filters))
    return out


def build_draft(user: User, text: str, previous: str = "", history: list | None = None,
                awaiting: str = "", choices: dict | None = None, include_private: bool = True,
                pending: dict | None = None, screen: dict | None = None) -> dict:
    """awaiting: short description of a draft JARVIS has just read back and is waiting to have confirmed.
    choices: {"intent": ..., "items": [{"id", "title"}]} when JARVIS just asked "which one?" — the text is the answer.
    include_private=False: phone locked; the MD's private items are kept out of the model's view.
    pending: the waiting card itself (so a yes can take the time JARVIS offered, or a new time can be checked)."""
    full_text = f"{previous}. {text}".strip(". ") if previous else text
    card = _clean_pending(pending) if awaiting else None  # the waiting card (not to be confused with pending requests)
    ctx = services.ai_context(user, include_private=include_private)
    # The language to answer in, from the sentence itself (or lately); the model is told, not asked.
    ctx["reply_language"] = lang_mod.lang_for(user, text)
    # The last few turns of THIS conversation, so follow-ups like "aur Neha ko bhi" make sense.
    ctx["conversation"] = ai_brain.history_text(history)
    # The card he is looking at, so "is list ka PDF", "pehla wala" or a bare name from the list make sense.
    on_screen = _clean_screen(screen)
    if on_screen:
        ctx["on_screen"] = on_screen
    # The list JARVIS answered with last (its filters), so "sirf meetings" / "aur Sir ke diye hue" / "sab dikhao"
    # start from the same list even when the phone sent no card.
    last = services.last_view(user, include_private=include_private)
    if last and not (on_screen or {}).get("filters"):
        ctx["last_list"] = last
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
    address = services.address_of(user)
    address_hi = services.address_hi_of(user)
    sir, sir_hi = services.md_address(), services.md_address_hi()
    intent = _route_intent(user, cmd, full_text)
    # Which language JARVIS answers in: a pinned setting, else this sentence, else the model's reading, else lately.
    heard = lang_mod.detect(text) or lang_mod.clean(cmd.get("reply_language"))
    reply_language = lang_mod.lang_for(user, text, cmd.get("reply_language"))
    lang_mod.remember(user, heard)

    due_date = _safe_date(cmd.get("due_date", ""))
    due_time = _safe_time(cmd.get("due_time", ""))
    # "In 5 minutes": the exact moment from now, seconds included, so the reminder fires when he said and no earlier
    # (a lead would put it in the past; "tomorrow?" is never asked because the moment is ahead by construction).
    due_in = max(0, min(int(cmd.get("due_in_minutes") or 0), 7 * 24 * 60))
    exact_at = (tu.now_local() + timedelta(minutes=due_in)).replace(microsecond=0) if due_in else None
    if exact_at:
        due_date, due_time = exact_at.date().isoformat(), exact_at.strftime("%H:%M")
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
        "due_at": exact_at.isoformat() if exact_at else "",       # the exact moment, when it was given as "in N minutes"
        "due_in_minutes": due_in,
        "due_label": (f"in {tu.minutes_phrase(due_in)} ({tu.fmt_time(exact_at)})" if exact_at else _due_label(due_date, due_time)),
        "duration_minutes": max(0, int(cmd.get("duration_minutes") or 0)) or None,
        "remind_before_minutes": 0 if exact_at else (max(0, int(cmd.get("remind_before_minutes") or 0)) or None),
        "snooze_until": "",     # snooze: when the reminder or alert comes back
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
        "people": [],           # everyone named in it (checked against staff, contacts and past tasks)
        "contacts_saved": [],   # people the speaker just explained, now remembered
        "question_hi": "",      # Devanagari version of a server-built question, for the Hindi voice
        "reply_en": "",         # a server-built reply and its Devanagari twin (see _say)
        "reply_hi": "",
        "schedule": None,       # diary check: clash / free slot / full day (see _advise_schedule)
        "overrides": {},        # confirm_pending: day / time to apply to the waiting card before saving
        "view": None,           # data the phone draws for this answer (timeline / chart / table), see datacards
        "screen_action": "",    # something to do on the app's screen (export, share, open an item, go somewhere)
        "setting_key": "",      # set_setting
        "setting_value": "",
        "needs_clarification": bool(cmd.get("needs_clarification")),
        "question": (cmd.get("clarification_question") or "").strip(),
        "spoken_reply": (cmd.get("spoken_reply") or "").strip(),
        "ask_text": "",       # general question for /assistant/ask/
        "memory_text": "",    # fact to remember (after the user confirms)
        "reply_language": reply_language,
        "speak_text": "",     # what the voice says (Devanagari when the reply is Hindi)
        "speak_lang": "en",
        "done_reply": (cmd.get("done_reply") or "").strip()[:300],  # said after a successful save
        "done_speak": "",
        "done_lang": "en",
    }

    summary_hi = ""  # Hindi twin of a server-written answer (lists and charts), for the Hindi voice

    if intent == "create_task":
        draft["action"] = "private_task"
    elif intent == "request_for_md":
        draft["action"] = "md_request"
    elif intent == "pa_own":
        draft["action"] = "pa_self_task"  # Mansi's own task: on her list, visible to Sir, no approval needed
    elif intent == "assign_to_staff":
        draft["action"] = "delegation"
        _prepare_delegation(draft, cmd, user, address, address_hi)
    elif intent == "assign_to_team":
        draft["action"] = "team_delegation"
        _prepare_team(draft, cmd, address, address_hi)
    elif intent in {"assign_to_pa", "meet_with_pa"}:
        draft["action"] = "pa_meeting" if intent == "meet_with_pa" else "pa_task"
        if intent == "meet_with_pa":
            draft["kind"] = Kind.MEETING
        pa = services.get_pa()
        draft["pa_name"] = pa.spoken_name if pa else ""
        if not pa:
            _ask(draft, f"{address}, there is no PA account set up yet.", f"{address_hi}, अभी कोई PA अकाउंट बना नहीं है।")
        elif intent == "meet_with_pa" and not draft["title"].lower().startswith("meeting"):
            draft["title"] = draft["title"] or f"Meeting with {pa.spoken_name}"
    elif intent == "needs_destination":
        draft["action"] = "choose_destination"
        draft["needs_clarification"] = True
        if cmd.get("intent") != "needs_destination":
            draft["question"] = ""  # the model was asking something else; whose it is comes first
        if not draft["question"]:
            if user.is_md:
                _ask(draft, f"{address}, should I add this to your personal tasks, give it to your PA, or assign it to someone in the team?",
                     f"{address_hi}, इसे आपके पर्सनल में रखूँ, PA को दूँ, या टीम में किसी को?")
            else:
                _ask(draft, f"{address}, is this for {sir}, for you, or for someone in the team?",
                     f"{address_hi}, ये {sir_hi} के लिए है, आपके लिए, या टीम में किसी के लिए?")
    elif intent in {"accept_request", "decline_request"}:
        draft["action"] = intent
        ids = {r["id"]: r for r in pending}
        chosen = _chosen(cmd, ids)
        verb = "accept" if intent == "accept_request" else "decline"
        verb_hi = "मंज़ूर कर दूँ" if intent == "accept_request" else "मना कर दूँ"
        if chosen:
            draft["targets"] = chosen
        elif len(pending) == 1:
            draft["targets"] = [pending[0]]
        elif pending:
            draft["target_candidates"] = pending[:5]
            names = "; ".join(r["title"] for r in pending[:3])
            _ask(draft, f"{address}, which request? {names}. Or all of them?", f"{address_hi}, कौन सी रिक्वेस्ट? {names}। या सभी?")
        else:
            draft["action"] = "answer"
            _say(draft, f"{address}, there are no pending requests right now.", f"{address_hi}, अभी कोई रिक्वेस्ट पेंडिंग नहीं है।")
        if draft["targets"]:
            draft["target"] = draft["targets"][0]
            if len(draft["targets"]) == 1:
                _say(draft, f"{address}, {verb} the request: {draft['target']['title']}?",
                     f"{address_hi}, रिक्वेस्ट {draft['target']['title']} {verb_hi}?")
            else:
                names = "; ".join(t["title"] for t in draft["targets"])
                _say(draft, f"{address}, {verb} these {len(draft['targets'])} requests: {names}?",
                     f"{address_hi}, ये {len(draft['targets'])} रिक्वेस्ट {verb_hi}: {names}?")
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
                _say(draft, f"{address}, mark '{matches[0]['title']}' as done?", f"{address_hi}, '{matches[0]['title']}' पूरा हुआ मार्क कर दूँ?")
            else:
                names = "; ".join(m["title"] for m in matches)
                _say(draft, f"{address}, mark these {len(matches)} as done: {names}?",
                     f"{address_hi}, ये {len(matches)} पूरे हुए मार्क कर दूँ: {names}?")
        else:
            draft["target_candidates"] = matches[:5]
            if matches:
                names = "; ".join(m["title"] for m in matches[:3])
                _ask(draft, f"{address}, which one did you finish: {names}. Or both?", f"{address_hi}, कौन सा पूरा हुआ: {names}। या दोनों?")
            else:
                _ask(draft, f"{address}, which task did you finish?", f"{address_hi}, कौन सा टास्क पूरा हुआ?")
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
                    _ask(draft, f"{address}, what should I change about '{names}': the time, the day, the priority or the name?",
                         f"{address_hi}, '{names}' में क्या बदलूँ: समय, दिन, प्रायोरिटी या नाम?")
                else:
                    _say(draft, f"{address}, {describe_changes(draft['changes'])} for '{names}'?",
                         f"{address_hi}, '{names}' {describe_changes_hi(draft['changes'])}?")
            elif len(matches) == 1:
                # Deleting always asks first. It is a cancel, not an erase, so it can be undone from the app.
                _say(draft, f"{address}, delete '{names}'? It will be cancelled, not erased.",
                     f"{address_hi}, '{names}' हटा दूँ? ये कैंसल होगा, मिटेगा नहीं।")
            else:
                _say(draft, f"{address}, delete these {len(matches)}: {names}?", f"{address_hi}, ये {len(matches)} हटा दूँ: {names}?")
        else:
            draft["target_candidates"] = matches[:5]
            verb = "change" if intent == "change_task" else "delete"
            verb_hi = "बदलूँ" if intent == "change_task" else "हटाऊँ"
            if matches:
                names = "; ".join(m["title"] for m in matches[:3])
                _ask(draft, f"{address}, which one should I {verb}: {names}?", f"{address_hi}, कौन सा {verb_hi}: {names}?")
            else:
                _ask(draft, f"{address}, which task should I {verb}?", f"{address_hi}, कौन सा टास्क {verb_hi}?")
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
                _say(draft, f"Done {address}: {what}.", f"हो गया {address_hi}: {what}।")
        except services.ServiceError as err:
            _ask(draft, f"{address}, {err.message}")
    elif intent == "briefing":
        draft["action"] = "briefing"
        if not draft["spoken_reply"]:
            _say(draft, f"Here is your briefing, {address}.", f"{address_hi}, ये रही आपकी ब्रीफ़िंग।")
        draft["view"] = datacards.build(user, "today", include_private)
    elif intent == "general_question":
        # Answered by /assistant/ask/ (web search). The answering step asks for missing details itself.
        draft["action"] = "ask"
        draft["ask_text"] = ((cmd.get("question") or "").strip() or full_text)[:1000]
        draft["needs_clarification"] = False
        draft["question"] = ""
        if not draft["spoken_reply"]:
            _say(draft, f"One moment, {address}.", f"एक सेकंड {address_hi}, देख रहा हूँ।")
    elif intent == "remember":
        draft["action"] = "remember"
        draft["memory_text"] = " ".join((cmd.get("memory_text") or "").split())[:300]
        if not draft["memory_text"]:
            _ask(draft, f"{address}, what should I remember?", f"{address_hi}, क्या याद रखूँ?")
        elif not draft["spoken_reply"]:
            _say(draft, f"{address}, I'll remember this: {draft['memory_text']} Okay?",
                 f"{address_hi}, मैं ये याद रखूँगा: {draft['memory_text']} ठीक है?")
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
        if not draft["spoken_reply"]:
            _say(draft, f"Okay {address}, cancelled. Nothing was saved.", f"ठीक है {address_hi}, कैंसल कर दिया। कुछ सेव नहीं हुआ।")
    elif intent == "snooze":
        # "Later": the reminder or alert JARVIS just spoke comes back when he said: a duration, a moment, or his
        # usual snooze. The phone (or the background conversation) applies it to the thing that was asked about.
        draft["action"] = "snooze"
        draft["needs_clarification"] = False
        draft["question"] = ""
        prefs = services.get_prefs(user)
        now_local = tu.now_local()
        minutes = max(0, min(int(cmd.get("snooze_minutes") or 0), 7 * 24 * 60))
        if minutes:
            until = now_local + timedelta(minutes=minutes)
            services.set_pref_quiet(user, "snoozeMinutes", max(1, min(minutes, 1440)))  # learned: his usual "later"
        elif due_date and due_time:
            until = tu.combine_local(date.fromisoformat(due_date), time.fromisoformat(due_time))
        elif due_date:
            until = tu.combine_local(date.fromisoformat(due_date), services.day_reminder_time(prefs))
        else:
            until = now_local + timedelta(minutes=int(prefs.get("snoozeMinutes", 15)))
        if until <= now_local:
            until += timedelta(days=1)  # "4 baje" said at 5 PM means tomorrow at 4
        draft["snooze_until"] = until.replace(microsecond=0).isoformat()
        _say(draft, f"Okay {address}, I'll remind you {services.moment_label(until)}.",
             f"ठीक है {address_hi}, {services.moment_label_hi(until)} याद दिला दूँगा।")
    elif intent == "dismiss_alert":
        draft["action"] = "dismiss"  # the phone clears the alert on screen
        draft["needs_clarification"] = False
        draft["question"] = ""
        if not draft["spoken_reply"]:
            _say(draft, "Done.", "ठीक है।")
    elif intent == "screen_action":
        # Voice works the screen: the app does it, nothing is saved or changed on the server.
        act = cmd.get("screen_action") if cmd.get("screen_action") in _SCREEN_ACTIONS else ""
        draft["action"] = "screen" if act else "answer"
        draft["screen_action"] = act
        draft["needs_clarification"] = False
        draft["question"] = ""
        if act in ("export_pdf", "export_csv", "share") and not on_screen:
            draft["action"], draft["screen_action"] = "answer", ""
            _say(draft, f"{address}, there is no list on screen. Ask me for one first, then I can download or share it.",
                 f"{address_hi}, स्क्रीन पर कोई लिस्ट नहीं है। पहले कोई लिस्ट माँगिए, फिर मैं डाउनलोड या शेयर कर सकता हूँ।")
        elif act == "open_item":
            known = {i["id"]: i for i in ctx["open_items"]["overdue"] + ctx["open_items"]["today"] + ctx["open_items"]["upcoming"]}
            for i in (on_screen or {}).get("items", []):
                known.setdefault(i["id"], i)
            chosen = _chosen(cmd, known)
            if chosen:
                draft["target"], draft["targets"] = chosen[0], chosen[:1]
                if not draft["spoken_reply"]:
                    _say(draft, f"Opening {chosen[0]['title']}.", f"{chosen[0]['title']} खोल रहा हूँ।")
            else:
                draft["action"], draft["screen_action"] = "answer", ""
                _say(draft, f"{address}, which one should I open?", f"{address_hi}, कौन सा खोलूँ?")
        elif not act:
            _say(draft, f"Sorry {address}, I can't do that on the screen yet.", f"माफ़ कीजिए {address_hi}, ये अभी स्क्रीन पर नहीं कर सकता।")
        if not draft["spoken_reply"]:
            en = {"export_pdf": f"Making the PDF, {address}.", "export_csv": f"Making the sheet, {address}.",
                  "share": f"Opening share, {address}.", "open_settings": f"Opening settings, {address}."}.get(act, f"Okay {address}.")
            hi = {"export_pdf": f"PDF बना रहा हूँ, {address_hi}।", "export_csv": f"शीट बना रहा हूँ, {address_hi}।",
                  "share": f"शेयर खोल रहा हूँ, {address_hi}।", "open_settings": f"सेटिंग्स खोल रहा हूँ, {address_hi}।"}.get(act, f"ठीक है {address_hi}।")
            _say(draft, en, hi)
    elif intent == "end_conversation":
        draft["action"] = "end"
        draft["needs_clarification"] = False
        draft["question"] = ""
        if not draft["spoken_reply"]:
            _say(draft, f"Okay {address}. Just say Hello JARVIS whenever you need me.",
                 f"ठीक है {address_hi}। जब भी ज़रूरत हो, बस हेलो जार्विस बोलिए।")
    else:
        draft["action"] = "answer"
        if intent == "query":
            # The spoken answer is short; the screen gets the data (timeline, chart or table) that fits the question.
            # The model only fills in a request form (which template, whose, which period); the server fills the
            # template from the database, so the same chart serves every person and every day.
            kind = cmd.get("view_kind") if cmd.get("view_kind") in datacards.VIEW_KINDS else datacards.guess_kind(full_text)
            # The items he pointed at ("that meeting", "the second one"): only ids JARVIS itself has shown or listed.
            known = {i["id"] for i in ctx["open_items"]["overdue"] + ctx["open_items"]["today"] + ctx["open_items"]["upcoming"]}
            known |= {i["id"] for i in (on_screen or {}).get("items", [])}
            ids = [r for r in (cmd.get("target_refs") or []) if isinstance(r, str) and r in known]
            if not ids and cmd.get("target_ref") in known:
                ids = [cmd["target_ref"]]
            request = {"who": cmd.get("view_who"), "from": cmd.get("view_from"), "to": cmd.get("view_to"),
                       "by": cmd.get("view_by"), "filter": cmd.get("view_filter"), "group": cmd.get("view_group"),
                       "types": cmd.get("view_types"), "from_whom": cmd.get("view_from_whom"),
                       "priority": cmd.get("view_priority"), "words": cmd.get("view_words"), "ids": ids}
            view = datacards.build(user, kind, include_private, params=request, missing=cmd.get("view_missing") or "")
            if view and view.get("ask"):
                # "Ramesh" fits two people: ask which, show nothing yet.
                draft["needs_clarification"] = True
                draft["question"], draft["question_hi"] = view["ask"], view.get("ask_hi", "")
                view = None
            elif view and view.get("summary"):
                # The sentence comes from the real numbers, never from the model's own counting.
                draft["spoken_reply"], summary_hi = view["summary"], view.get("summary_hi", "")
            if view:
                names_private = any(r.get("private") for r in (view.get("rows") or [])) or any(
                    t.get("private") for t in (view.get("timeline") or []))
                services.remember_last_view(user, view.get("request") or {"kind": kind}, view.get("summary", ""), names_private)
                services.learn_view(user, view.get("kind") or kind)  # the lists he asks for most come first on the home screen
            draft["view"] = view
        if not draft["spoken_reply"]:
            if intent == "query":
                today = ctx["open_items"]["today"]
                overdue = ctx["open_items"]["overdue"]
                if today:
                    firsts = ", ".join(f"{i['title']} {i['due_label']}".strip() for i in today[:3])
                    en = f"{address}, today you have {len(today)} item{'s' if len(today) > 1 else ''}: {firsts}."
                    hi = f"{address_hi}, आज आपके {len(today)} काम हैं: {firsts}।"
                else:
                    en, hi = f"{address}, nothing is scheduled for today.", f"{address_hi}, आज कुछ भी शेड्यूल नहीं है।"
                if overdue:
                    en += f" {len(overdue)} overdue."
                    hi += f" {len(overdue)} लेट हैं।"
                _say(draft, en, hi)
            else:
                _say(draft, f"Sorry {address}, I didn't catch that. Please say it again or type it.",
                     f"माफ़ कीजिए {address_hi}, समझ नहीं आया। फिर से बोलिए या टाइप कर दीजिए।")

    # Does JARVIS know the people named? Never book something with a name nobody recognises.
    if draft["action"] in TASK_ACTIONS or draft["action"] == "choose_destination":
        _check_people(user, cmd, draft, address, history)

    if draft["action"] in TASK_ACTIONS and draft["due_date"] and draft["due_time"]:
        when = tu.combine_local(date.fromisoformat(draft["due_date"]), time.fromisoformat(draft["due_time"]))
        if when <= tu.now_local() and not draft["needs_clarification"]:
            label, label_hi = tu.fmt_time(when), tu.fmt_time_hi(when)
            _ask(draft, f"{address}, {label} {tu.fmt_day(when.date())} has already passed. Tomorrow at {label}, or a different time?",
                 f"{address_hi}, {tu.fmt_day_hi(when.date())} {label_hi} निकल चुका है। कल {label_hi}, या कोई और समय?")

    if draft["action"] in TASK_ACTIONS:
        if not draft["title"] and not draft["question"]:
            _ask(draft, f"{address}, what should the task say?", f"{address_hi}, टास्क में क्या लिखूँ?")
        elif not draft["title"]:
            draft["needs_clarification"] = True
        pa_name = draft.get("pa_name") or "your PA"
        due_hi = (f"{tu.minutes_phrase_hi(due_in)} में ({tu.fmt_time_hi(exact_at)})" if exact_at
                  else _due_label_hi(draft["due_date"], draft["due_time"]))
        if draft["action"] == "pa_meeting":
            # Always state clearly that the PA will be informed.
            when = f" {draft['due_label']}" if draft["due_label"] else ""
            _say(draft, f"{address}, meeting with {pa_name}{when}. I'll inform {pa_name} too. Shall I save it?",
                 f"{address_hi}, {pa_name} के साथ मीटिंग" + (f" {due_hi}" if due_hi else "") + f"। {pa_name} को भी बता दूँगा। सेव कर दूँ?")
        elif draft["action"] != "team_delegation" and (not draft["spoken_reply"] or draft["engine"] == "rules"):
            what = {
                "private_task": "Personal task",
                "md_request": "Request for MD",
                "delegation": f"Assign to {draft['staff']['name']}" if draft["staff"] else "Assign to staff",
                "pa_task": f"Task for {pa_name}",
                "pa_self_task": "Your own task",
            }[draft["action"]]
            what_hi = {
                "private_task": "पर्सनल टास्क",
                "md_request": f"{sir_hi} के लिए रिक्वेस्ट",
                "delegation": f"{draft['staff']['name']} को असाइन" if draft["staff"] else "स्टाफ़ को असाइन",
                "pa_task": f"{pa_name} के लिए टास्क",
                "pa_self_task": "आपका अपना टास्क",
            }[draft["action"]]
            bits = [b for b in (draft["due_label"], f"{priority.lower()} priority" if priority != "NORMAL" else "") if b]
            bits_hi = [b for b in (due_hi, f"{_PRIORITY_HI[priority]} प्रायोरिटी" if priority != "NORMAL" else "") if b]
            _say(draft, f"{address}, {what}: {draft['title']}" + (f", {', '.join(bits)}" if bits else "") + ". Shall I save it?",
                 f"{address_hi}, {what_hi}: {draft['title']}" + (f", {', '.join(bits_hi)}" if bits_hi else "") + "। सेव कर दूँ?")

    # Does it fit the diary? (clash, free slot, a very full day) — may replace the question above.
    if draft["action"] in _SLOT_ACTIONS and not draft["needs_clarification"]:
        _advise_schedule(user, draft, address)

    if draft["needs_clarification"] and draft["question"]:
        draft["spoken_reply"] = draft["question"]

    # Voice text, when the reply language is Hindi: a server-built question or answer has its own Devanagari twin;
    # the model's Devanagari version is used only when the final reply is still the model's own sentence.
    hindi = (cmd.get("spoken_reply_hi") or "").strip()
    sched = draft.get("schedule") or {}
    model_reply = (cmd.get("spoken_reply") or "").strip()
    base_reply = sched.get("base_reply", draft["spoken_reply"]) if sched.get("notes_hi") and not sched.get("question") else draft["spoken_reply"]
    if draft["reply_language"] == "hi" and draft["needs_clarification"] and draft.get("question_hi") \
            and draft["spoken_reply"] == draft["question"]:
        draft["speak_text"], draft["speak_lang"] = draft["question_hi"], "hi"
    elif draft["reply_language"] == "hi" and summary_hi and draft["spoken_reply"] == (draft.get("view") or {}).get("summary"):
        draft["speak_text"], draft["speak_lang"] = summary_hi, "hi"  # the server-written answer, in Hindi
    elif draft["reply_language"] == "hi" and sched.get("question_hi"):
        draft["speak_text"], draft["speak_lang"] = sched["question_hi"], "hi"
    elif draft["reply_language"] == "hi" and hindi and _DEVANAGARI.search(hindi) and base_reply == model_reply:
        draft["speak_text"], draft["speak_lang"] = _insert_notes(hindi, sched.get("notes_hi") or [], hindi=True), "hi"
    elif draft["reply_language"] == "hi" and draft["reply_hi"] and base_reply == draft["reply_en"]:
        draft["speak_text"], draft["speak_lang"] = _insert_notes(draft["reply_hi"], sched.get("notes_hi") or [], hindi=True), "hi"
    else:
        draft["speak_text"], draft["speak_lang"] = draft["spoken_reply"], "en"
    done_hi = (cmd.get("done_reply_hi") or "").strip()
    if draft["done_reply"]:
        use_hi = draft["reply_language"] == "hi" and bool(_DEVANAGARI.search(done_hi))
        draft["done_speak"], draft["done_lang"] = (done_hi, "hi") if use_hi else (draft["done_reply"], "en")
    # Fixed Hindi lines for the two short replies the model sometimes writes without a Devanagari version.
    if draft["reply_language"] == "hi" and draft["speak_lang"] != "hi":
        if draft["action"] == "ask":
            draft["speak_text"], draft["speak_lang"] = f"एक सेकंड {address_hi}, देख रहा हूँ।", "hi"
        elif draft["action"] == "end":
            draft["speak_text"], draft["speak_lang"] = f"ठीक है {address_hi}। जब भी ज़रूरत हो, बस हेलो जार्विस बोलिए।", "hi"
    return draft
