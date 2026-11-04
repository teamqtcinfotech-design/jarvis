"""The conversation brain for the phone's background listener (voice only, app closed).

One call per thing the user said: ``turn(user, text)`` returns what JARVIS should say and whether to
listen again. The server keeps a short, encrypted memory per user (last turns, the card waiting for a
yes/no, the options after "which one?", missed reminders it offered to read) and performs the action
itself when the user confirms — through the same service functions and permission checks as the API.
"""
import json
import logging
from datetime import date, datetime, time, timedelta

from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from . import ai_brain, assistant, services
from .assistant import TASK_ACTIONS
from .models import ConversationState, IntegrationCommand, PrivateTask, SharedTask, StaffMember, Status, User
from .reminders import reminders_for
from .services import ServiceError

log = logging.getLogger("jarvis.conversation")

STATE_TTL = timedelta(minutes=10)
MAX_HISTORY = 8
CONFIRMABLE = TASK_ACTIONS | {"accept_request", "decline_request", "complete", "remember", "change_task", "delete_task"}
AI_DONE_OK = {"private_task", "pa_task", "pa_meeting", "md_request", "remember", "accept_request", "decline_request",
              "complete"}
EMPTY = {"history": [], "pending": None, "choices": None, "offer": None}


# ------------------------------------------------------------------ state
def _load(user: User) -> dict:
    row = ConversationState.objects.filter(user=user).first()
    if not row or timezone.now() - row.updated_at > STATE_TTL:
        return {**EMPTY, "history": []}
    try:
        data = json.loads(row.state_json or "{}")
    except ValueError:
        data = {}
    return {k: data.get(k, v if not isinstance(v, list) else []) for k, v in EMPTY.items()}


def _save(user: User, state: dict) -> None:
    ConversationState.objects.update_or_create(
        user=user, defaults={"state_json": json.dumps(state, ensure_ascii=False, default=str)})


def reset(user: User) -> None:
    ConversationState.objects.filter(user=user).delete()


def _resp(show: str, speak: str = "", lang: str = "en", *, listen: bool = True, action: str = "") -> dict:
    return {"show": show, "say": speak or show, "say_lang": lang if speak else "en", "fallback": show,
            "listen": listen, "action": action}


# ------------------------------------------------------------------ describing what is waiting
def _heading(p: dict) -> str:
    pa = p.get("pa_name") or "your PA"
    return {
        "private_task": "Personal task", "pa_task": f"Task for {pa}", "pa_meeting": f"Meeting with {pa}",
        "delegation": f"Task for {(p.get('staff') or {}).get('name', 'a staff member')}",
        "team_delegation": f"Task for {(p.get('team') or {}).get('label', 'the team')}",
        "md_request": "Request to Sir", "accept_request": "Accept request", "decline_request": "Decline request",
        "complete": "Mark as done", "remember": "Remember this", "choose_destination": "Where should this go?",
        "change_task": "Change task", "delete_task": "Delete task",
    }.get(p.get("action", ""), "JARVIS")


def _describe(p: dict) -> str:
    targets = p.get("targets") or ([p["target"]] if p.get("target") else [])
    what = p.get("memory_text") if p.get("action") == "remember" else (
        " + ".join(t["title"] for t in targets) if targets else p.get("title", ""))
    when = f", {p['due_label']}" if p.get("due_label") else ""
    who = f", for {p['staff']['name']}" if p.get("action") == "delegation" and p.get("staff") else ""
    change = f" ({assistant.describe_changes(p['changes'])})" if p.get("action") == "change_task" and p.get("changes") else ""
    return f"{_heading(p)}: {what}{when}{who}{change}"[:400]


def _slim(draft: dict) -> dict:
    return {k: v for k, v in draft.items() if k not in {"categories", "staff_candidates"}}


def _choices_for(draft: dict) -> dict | None:
    intent = {"complete": "complete_task", "accept_request": "accept_request", "decline_request": "decline_request",
              "change_task": "change_task", "delete_task": "delete_task"}.get(draft["action"])
    if intent and draft.get("target_candidates"):
        return {"intent": intent, "items": [{"id": c["id"], "title": c["title"]} for c in draft["target_candidates"][:10]]}
    return None


# ------------------------------------------------------------------ the turn
def turn(user: User, text: str, *, event: str = "", locked: bool = False, private_ok: bool = True) -> dict:
    address = "Sir" if user.is_md else user.spoken_name
    state = _load(user)
    if event == "reset":
        reset(user)
        return _resp("", listen=False, action="reset")
    if event == "wake":
        return _wake(user, state, address, private_ok)

    text = (text or "").strip()
    if not text:
        return _resp(f"{address}?", listen=True, action="nudge")
    state["history"].append({"role": "user", "text": text})
    pending, choices, offer = state.get("pending"), state.get("choices"), state.get("offer")

    previous, awaiting = "", ""
    if offer:
        awaiting = f'JARVIS asked: "{offer["question"]}" ({len(offer["items"])} reminders came while away)'
    elif pending:
        if pending.get("needs_clarification") or pending.get("action") == "choose_destination":
            previous = pending.get("transcript", "")
        else:
            awaiting = _describe(pending)
    draft = assistant.build_draft(user, text, previous, state["history"][:-1], awaiting, choices,
                                  include_private=private_ok)
    action = draft["action"]
    lang = draft.get("speak_lang", "en")
    speak = draft.get("speak_text") or draft["spoken_reply"]
    show = draft["spoken_reply"]
    if offer and action != "confirm_pending":
        state["offer"] = None  # he asked for something else: the reminders stay in his list

    if action == "confirm_pending":
        if offer:
            state["offer"] = None
            resp = _resp(" ".join(i["spoken"] for i in offer["items"]), listen=True, action="read_missed")
        elif pending:
            state["pending"], state["choices"] = None, None
            resp = _execute(user, pending, address)
        else:
            resp = _resp(f"{address}, there's nothing waiting to be confirmed.", listen=True, action="answer")
    elif action == "cancel_pending":
        state["pending"], state["choices"], state["offer"] = None, None, None
        resp = _resp(show or f"Okay {address}, cancelled.", speak, lang, listen=True, action=action)
    elif action == "end":
        state = {**EMPTY, "history": []}
        resp = _resp(show, speak, lang, listen=False, action="end")
    elif action == "ask":
        reader = "the Managing Director" if user.is_md else f"{user.spoken_name}, the MD's personal assistant,"
        ans = ai_brain.answer(draft["ask_text"], state["history"], user=user, address=address, reader=reader,
                              lang=draft.get("reply_language", "en"))
        resp = _resp(ans["text"], ans["speak_text"], ans["speak_lang"], listen=True, action="answer")
    elif action == "briefing":
        facts, fallback = services.briefing_facts(user, "now")
        txt, _engine = ai_brain.briefing(facts, fallback, user=user)
        resp = _resp(txt, listen=True, action="briefing")
    elif action == "set_setting" and not draft["needs_clarification"]:
        try:
            services.set_prefs(user, draft.get("setting_patch") or {})
            resp = _resp(show, speak, lang, listen=True, action="setting_applied")
        except ServiceError as err:
            resp = _resp(f"{address}, {err.message}", listen=True, action="error")
    elif draft["needs_clarification"] or action == "choose_destination":
        state["pending"] = _slim(draft) if (action in CONFIRMABLE or action == "choose_destination") else None
        state["choices"] = _choices_for(draft)
        resp = _resp(show, speak, lang, listen=True, action=action)
    elif action in CONFIRMABLE:
        state["pending"], state["choices"] = _slim(draft), None
        resp = _resp(show, speak, lang, listen=True, action=action)
    else:
        resp = _resp(show, speak, lang, listen=True, action=action)  # an answer, small talk; a waiting card stays

    state["history"].append({"role": "jarvis", "text": resp["show"]})
    state["history"] = state["history"][-MAX_HISTORY:]
    _save(user, state)
    return resp


def _wake(user: User, state: dict, address: str, private_ok: bool) -> dict:
    """"Hey JARVIS": greet, and offer to read the reminders that came while he was away."""
    now = timezone.now()
    missed = [r for r in reminders_for(user, hours=1)["reminders"] if datetime.fromisoformat(r["fire_at"]) <= now]
    greet_show = "Ji Sir?" if user.is_md else f"Ji {user.spoken_name}?"
    greet_hi = "जी सर?" if user.is_md else "जी?"
    if not missed:
        resp = _resp(greet_show, greet_hi, "hi", listen=True, action="greet")
    else:
        items = [{"title": r["title"],
                  "spoken": r["spoken"] if (private_ok or not r["is_private"]) else f"{address}, you have a private reminder."}
                 for r in missed[:10]]
        n = len(missed)
        question = f"While you were away: {n} reminder{'s' if n > 1 else ''}. Shall I read them?"
        state["offer"] = {"question": question, "items": items}
        resp = _resp(f"{greet_show} {question}", f"{greet_hi} जब आप दूर थे: {n} रिमाइंडर। पढ़ कर सुनाऊँ?", "hi",
                     listen=True, action="offer")
    state["history"].append({"role": "jarvis", "text": resp["show"]})
    state["history"] = state["history"][-MAX_HISTORY:]
    _save(user, state)
    return resp


# ------------------------------------------------------------------ doing the work
def _task_data(p: dict) -> dict:
    return {
        "title": (p.get("title") or "").strip(),
        "notes": p.get("notes") or "",
        "kind": p.get("kind") or "TASK",
        "priority": p.get("priority") or "NORMAL",
        "due_date": date.fromisoformat(p["due_date"]) if p.get("due_date") else None,
        "due_time": time.fromisoformat(p["due_time"]) if p.get("due_time") else None,
        "duration_minutes": p.get("duration_minutes") or None,
        "remind_before_minutes": p.get("remind_before_minutes") or None,
        "announcement": p.get("announcement") or "",
    }


def _complete_one(user: User, t: dict) -> bool:
    """Returns True when the completion went to Sir for approval instead of being applied."""
    version = int(t.get("version") or 1)
    if t.get("scope") == "private":
        services.update_private_task(user, t["id"], {"status": Status.COMPLETED}, version)
        return False
    task = SharedTask.objects.get(id=t["id"])
    if task.assignee_type == SharedTask.Assignee.PA:
        if user.is_pa:
            services.update_pa_task_status(user, t["id"], Status.COMPLETED, version)
        else:
            services.update_shared_md_task(user, t["id"], {"status": Status.COMPLETED}, version)
        return False
    if user.is_md:
        services.update_shared_md_task(user, t["id"], {"status": Status.COMPLETED}, version)
        return False
    services.propose_change(user, t["id"], {"status": Status.COMPLETED}, version, "Marked done")
    return True


def _change_one(user: User, t: dict, changes: dict, reason: str) -> bool:
    """Apply a change (or a cancel) to one task. Returns True when it went to Sir for approval instead."""
    version = int(t.get("version") or 1)
    clean = dict(changes)
    if clean.get("due_date"):
        clean["due_date"] = date.fromisoformat(clean["due_date"])
    if clean.get("due_time"):
        clean["due_time"] = time.fromisoformat(clean["due_time"])
    if t.get("scope") == "private":
        services.update_private_task(user, t["id"], clean, version)
        return False
    if user.is_md:
        services.update_shared_md_task(user, t["id"], clean, version)
        return False
    services.propose_change(user, t["id"], clean, version, reason)
    return True


def _execute(user: User, p: dict, address: str) -> dict:
    a = p.get("action")
    targets = p.get("targets") or ([p["target"]] if p.get("target") else [])
    try:
        if a in TASK_ACTIONS and not (p.get("title") or "").strip():
            raise ServiceError("invalid", "the task has no title.", 422)
        data = _task_data(p) if a in TASK_ACTIONS else {}
        if a == "private_task":
            services.create_private_task(user, data, source=PrivateTask.Source.VOICE)
            when = f" I'll remind you {p['due_label']}." if p.get("due_label") else ""
            spoken = f"Saved, {address}.{when}"
        elif a in {"pa_task", "pa_meeting"}:
            data["description"] = p.get("transcript", "")
            services.create_pa_task(user, data, meeting_with_pa=(a == "pa_meeting"))
            pa = p.get("pa_name") or "your PA"
            spoken = (f"Saved, {address}. I've told {pa}, and I'll remind you both." if a == "pa_meeting"
                      else f"Given to {pa}, {address}. I'll remind you both and tell you when it's done.")
        elif a == "md_request":
            services.create_md_request(user, data)
            spoken = f"Sent to Sir, {address}. I'll tell you when he answers."
        elif a == "delegation":
            staff = StaffMember.objects.get(id=(p.get("staff") or {})["id"])
            task, command = services.delegate_to_staff(user, data, staff, category_key=p.get("category_key") or "",
                                                       description=p.get("transcript", ""))
            if command.state == IntegrationCommand.State.REJECTED:
                task.delete()
                raise ServiceError(command.error_code or "rejected",
                                   command.error_message or "the staff app did not accept it.", 422)
            spoken = (f"Done, {address}. {staff.name} has the task now. I'll keep track of it."
                      if command.state == IntegrationCommand.State.SUCCEEDED
                      else f"{address}, sent to {staff.name}. I'm waiting for the staff app to confirm and will tell you.")
        elif a == "team_delegation":
            ids = [m["id"] for m in (p.get("team") or {}).get("members", [])]
            r = services.delegate_to_team(user, data, ids, category_key=p.get("category_key") or "",
                                          description=p.get("transcript", ""))
            n = len(r["assigned"])
            spoken = f"{address}, given to {n} {'person' if n == 1 else 'people'}."
            if r["pending"]:
                spoken += f" {len(r['pending'])} still being confirmed."
            if r["not_assigned"]:
                spoken += " Could not give it to: " + ", ".join(x["name"] for x in r["not_assigned"]) + "."
        elif a in {"accept_request", "decline_request"}:
            if not targets:
                raise ServiceError("invalid", "I don't know which request.", 422)
            for t in targets:
                if a == "accept_request":
                    services.accept_request(user, t["id"])
                else:
                    services.decline_request(user, t["id"])
            verb = "Accepted" if a == "accept_request" else "Declined"
            spoken = (f"{verb}, {address}." + (" It's on your list." if a == "accept_request" else "")
                      if len(targets) == 1 else f"{verb} all {len(targets)}, {address}.")
        elif a == "complete":
            if not targets:
                raise ServiceError("invalid", "I don't know which task.", 422)
            to_sir = sum(1 for t in targets if _complete_one(user, t))
            if len(targets) == 1:
                spoken = f"Sent to Sir to confirm, {address}." if to_sir else f"Marked as done, {address}."
            else:
                spoken = f"Marked {len(targets)} as done, {address}." + (f" {to_sir} sent to Sir to confirm." if to_sir else "")
        elif a == "remember":
            services.add_memory(user, p.get("memory_text", ""))
            spoken = f"Done, {address}. I'll remember that."
        elif a in {"change_task", "delete_task"}:
            if not targets:
                raise ServiceError("invalid", "I don't know which task.", 422)
            if a == "change_task":
                changes = p.get("changes") or {}
                if not changes:
                    raise ServiceError("invalid", "nothing to change.", 422)
                reason = "Changed by voice: " + assistant.describe_changes(changes)
            else:
                changes, reason = {"status": Status.CANCELLED}, "Cancelled by voice"
            to_sir = sum(1 for t in targets if _change_one(user, t, changes, reason))
            what = "Changed" if a == "change_task" else "Deleted"
            if to_sir:
                spoken = f"Sent to Sir to confirm, {address}."
            elif len(targets) == 1:
                spoken = f"{what}, {address}." + (" It's cancelled, not erased." if a == "delete_task" else "")
            else:
                spoken = f"{what} {len(targets)}, {address}."
        else:
            raise ServiceError("invalid", "I can't do that from here.", 422)
    except ServiceError as err:
        return _resp(f"Sorry {address}, it was not saved: {err.message}", listen=True, action="error")
    except ObjectDoesNotExist:
        return _resp(f"Sorry {address}, I couldn't find that any more.", listen=True, action="error")
    except Exception:  # noqa: BLE001 — a background voice loop must always get an answer
        log.exception("background execute failed")
        return _resp(f"Sorry {address}, something went wrong and it was not saved.", listen=True, action="error")

    if p.get("done_reply") and a in AI_DONE_OK:
        return _resp(p["done_reply"], p.get("done_speak") or p["done_reply"], p.get("done_lang", "en"),
                     listen=True, action="saved")
    return _resp(spoken, listen=True, action="saved")
