"""Turns one instruction into a draft the user confirms on screen.

Nothing here writes business data. The draft says what WOULD happen; the phone
then calls the normal, permission-checked endpoint with an Idempotency-Key.
"""
import re
from datetime import date, time

from django.conf import settings

from . import ai_brain, hinglish_parser, services
from . import timeutils as tu
from .models import Kind, Priority, TaskCategory, User
from .taskpanel.client import TaskPanelError, get_client as tp_client
from .taskpanel.contracts import ALL_STAFF_WORDS, build_validate_body

TASK_ACTIONS = {"private_task", "md_request", "delegation", "pa_task", "pa_meeting", "team_delegation"}

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


def build_draft(user: User, text: str, previous: str = "", history: list | None = None,
                awaiting: str = "") -> dict:
    """awaiting: short description of a draft JARVIS has just read back and is waiting to have confirmed."""
    full_text = f"{previous}. {text}".strip(". ") if previous else text
    ctx = services.ai_context(user)
    # The last few turns of THIS conversation, so follow-ups like "aur Neha ko bhi" make sense.
    ctx["conversation"] = ai_brain.history_text(history)
    model_text = full_text
    if awaiting:
        # The reply goes to the model on its own (so a side question is not glued onto the draft);
        # the draft and its original sentence are background.
        ctx["awaiting_confirmation"] = {"draft": awaiting[:400], "original_sentence": previous[:1000]}
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
        "target_candidates": [],
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
        ref = cmd.get("target_ref", "")
        if ref in ids:
            draft["target"] = ids[ref]
        elif len(pending) == 1:
            draft["target"] = pending[0]
        elif pending:
            draft["needs_clarification"] = True
            draft["target_candidates"] = pending[:5]
            draft["question"] = f"{address}, which request? " + "; ".join(r["title"] for r in pending[:3]) + "."
        else:
            draft["action"] = "answer"
            draft["spoken_reply"] = f"{address}, there are no pending requests right now."
        if draft["target"] and not draft["spoken_reply"]:
            verb = "accept" if intent == "accept_request" else "decline"
            draft["spoken_reply"] = f"{address}, {verb} the request: {draft['target']['title']}?"
    elif intent == "complete_task":
        draft["action"] = "complete"
        open_items = ctx["open_items"]["overdue"] + ctx["open_items"]["today"] + ctx["open_items"]["upcoming"]
        by_id = {i["id"]: i for i in open_items}
        ref = cmd.get("target_ref", "")
        matches = [by_id[ref]] if ref in by_id else _match_items(draft["title"] or full_text, open_items)
        if len(matches) == 1:
            draft["target"] = matches[0]
            if not draft["spoken_reply"]:
                draft["spoken_reply"] = f"{address}, mark '{matches[0]['title']}' as done?"
        else:
            draft["needs_clarification"] = True
            draft["target_candidates"] = matches[:5]
            draft["question"] = f"{address}, which task did you finish?"
    elif intent == "briefing":
        draft["action"] = "briefing"
        draft["spoken_reply"] = draft["spoken_reply"] or f"Here is your briefing, {address}."
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
    elif intent == "cancel_pending":
        draft["action"] = "cancel_pending"
        draft["needs_clarification"] = False
        draft["question"] = ""
        draft["spoken_reply"] = draft["spoken_reply"] or f"Okay {address}, cancelled. Nothing was saved."
    elif intent == "end_conversation":
        draft["action"] = "end"
        draft["needs_clarification"] = False
        draft["question"] = ""
        draft["spoken_reply"] = draft["spoken_reply"] or f"Okay {address}. Just say Hello JARVIS whenever you need me."
    else:
        draft["action"] = "answer"
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

    if draft["needs_clarification"] and draft["question"]:
        draft["spoken_reply"] = draft["question"]

    # Voice text: the model's Devanagari version is used only when the final reply is still the model's own
    # sentence (server-built confirmations are English and are spoken as written).
    hindi = (cmd.get("spoken_reply_hi") or "").strip()
    if (draft["reply_language"] == "hi" and hindi and _DEVANAGARI.search(hindi)
            and draft["spoken_reply"] == (cmd.get("spoken_reply") or "").strip()):
        draft["speak_text"], draft["speak_lang"] = hindi, "hi"
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
