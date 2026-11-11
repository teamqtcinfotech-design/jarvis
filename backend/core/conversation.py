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
from . import lang as lang_mod
from . import timeutils as tu
from .assistant import TASK_ACTIONS
from .models import Alert, ConversationState, IntegrationCommand, PrivateTask, ReminderAck, SharedTask, StaffMember, Status, User
from .reminders import reminders_for
from .services import ServiceError

log = logging.getLogger("jarvis.conversation")

STATE_TTL = timedelta(minutes=10)
MAX_HISTORY = 8
CONFIRMABLE = TASK_ACTIONS | {"accept_request", "decline_request", "complete", "remember", "change_task", "delete_task"}
AI_DONE_OK = {"private_task", "pa_task", "pa_meeting", "md_request", "remember", "accept_request", "decline_request",
              "complete", "pa_self_task"}
EMPTY = {"history": [], "pending": None, "choices": None, "offer": None,
         "last_view": None,  # the list JARVIS showed last (see services.remember_last_view); kept, not used here
         "asked": None}      # the reminder / alert the phone's background part just spoke and asked about


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


def _twin(lang: str, hi: str) -> tuple[str, str]:
    """(speak, lang) for _resp: the Hindi twin when the person's language is Hindi, else the shown line itself."""
    return (hi, "hi") if lang == "hi" and hi else ("", "en")


# ------------------------------------------------------------------ describing what is waiting
def _heading(p: dict) -> str:
    pa = p.get("pa_name") or "your PA"
    return {
        "private_task": "Personal task", "pa_task": f"Task for {pa}", "pa_meeting": f"Meeting with {pa}",
        "delegation": f"Task for {(p.get('staff') or {}).get('name', 'a staff member')}",
        "team_delegation": f"Task for {(p.get('team') or {}).get('label', 'the team')}",
        "md_request": f"Request to {services.md_address()}", "pa_self_task": "Your own task",
        "accept_request": "Accept request", "decline_request": "Decline request",
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
def turn(user: User, text: str, *, event: str = "", locked: bool = False, private_ok: bool = True,
         about: dict | None = None) -> dict:
    address, address_hi = services.address_of(user), services.address_hi_of(user)
    state = _load(user)
    if event == "reset":
        reset(user)
        return _resp("", listen=False, action="reset")
    if event == "wake":
        return _wake(user, state, address, private_ok)

    if event == "ask" and about:
        # The background part has just spoken a reminder or an alert and its question; the next words answer it.
        state["asked"] = {k: str(about.get(k) or "")[:300] for k in ("kind", "key", "id", "title", "question")}
        state["history"].append({"role": "jarvis", "text": f"{about.get('title') or ''} {about.get('question') or ''}".strip()})
        _save(user, state)
        return _resp("", listen=True, action="asking")

    text = (text or "").strip()
    if not text:
        return _resp(f"{address}?", *_twin(lang_mod.current(user), f"जी {address_hi}?"), listen=True, action="nudge")
    state["history"].append({"role": "user", "text": text})
    pending, choices, offer, asked = state.get("pending"), state.get("choices"), state.get("offer"), state.get("asked")

    previous, awaiting = "", ""
    if asked:
        awaiting = f'JARVIS asked: "{asked.get("question") or "Done, or remind you later?"}" about "{asked.get("title", "")}"'
    elif offer:
        awaiting = f'JARVIS asked: "{offer["question"]}" ({len(offer["items"])} reminders came while away)'
    elif pending:
        if pending.get("needs_clarification") or pending.get("action") == "choose_destination":
            previous = pending.get("transcript", "")
        else:
            awaiting = _describe(pending)
    draft = assistant.build_draft(user, text, previous, state["history"][:-1], awaiting, choices,
                                  include_private=private_ok, pending=pending if awaiting and not (offer or asked) else None)
    action = draft["action"]
    lang = draft.get("speak_lang", "en")
    speak = draft.get("speak_text") or draft["spoken_reply"]
    show = draft["spoken_reply"]
    said_in = draft.get("reply_language", "en")  # the language of this sentence: every line below follows it
    if offer and action != "confirm_pending":
        state["offer"] = None  # he asked for something else: the reminders stay in his list
    if asked and action not in {"confirm_pending", "cancel_pending", "snooze"}:
        state["asked"] = None  # he asked for something else: the reminder stays where it is

    if action == "snooze":
        until = datetime.fromisoformat(draft["snooze_until"])
        state["asked"] = None
        resp = _snooze(user, asked, until, show, speak, lang, said_in, address, address_hi)
    elif action == "confirm_pending":
        if asked:
            state["asked"] = None
            resp = _answer_asked(user, asked, True, address, address_hi, said_in)
        elif offer:
            state["offer"] = None
            read = " ".join(i["spoken"] for i in offer["items"])
            resp = _resp(read, read, "hi" if lang_mod.DEVANAGARI.search(read) else "en", listen=True, action="read_missed")
        elif pending:
            state["pending"], state["choices"] = None, None
            resp = _execute(user, _with_overrides(pending, draft.get("overrides") or {}), address, said_in)
        else:
            resp = _resp(f"{address}, there's nothing waiting to be confirmed.",
                         *_twin(said_in, f"{address_hi}, कुछ भी कन्फ़र्म होने के लिए नहीं है।"), listen=True, action="answer")
    elif action == "cancel_pending" and asked:
        state["asked"] = None
        resp = _answer_asked(user, asked, False, address, address_hi, said_in)
    elif action == "cancel_pending":
        state["pending"], state["choices"], state["offer"] = None, None, None
        resp = _resp(show or f"Okay {address}, cancelled.", speak, lang, listen=True, action=action)
    elif action == "end":
        state = {**EMPTY, "history": []}
        resp = _resp(show, speak, lang, listen=False, action="end")
    elif action == "dismiss":
        resp = _resp(show or "Done.", speak, lang, listen=True, action="dismiss")
    elif action == "screen":
        # Downloading, sharing or opening something needs the screen: this voice-only conversation has none.
        resp = _resp(f"{address}, please open JARVIS for that. With the app closed I can only talk.",
                     *_twin(said_in, f"{address_hi}, उसके लिए JARVIS खोलिए। ऐप बंद हो तो मैं सिर्फ़ बात कर सकता हूँ।"),
                     listen=True, action="answer")
    elif action == "ask":
        reader = "the Managing Director" if user.is_md else f"{user.spoken_name}, the MD's personal assistant,"
        ans = ai_brain.answer(draft["ask_text"], state["history"], user=user, address=address, reader=reader, lang=said_in)
        resp = _resp(ans["text"], ans["speak_text"], ans["speak_lang"], listen=True, action="answer")
    elif action == "briefing":
        facts, fallback = services.briefing_facts(user, "now")
        txt, _engine = ai_brain.briefing(facts, fallback, user=user)
        resp = _resp(txt, txt, "hi" if lang_mod.DEVANAGARI.search(txt) else "en", listen=True, action="briefing")
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


OFFER_MISSED_ON_WAKE = False  # he asked for "Hey JARVIS" to be listen-only: no reminders, no offers


def _wake(user: User, state: dict, address: str, private_ok: bool) -> dict:
    """"Hey JARVIS": greet and listen. (Offering missed reminders here is switched off on request.)"""
    now = timezone.now()
    missed = ([r for r in reminders_for(user, hours=1)["reminders"] if datetime.fromisoformat(r["fire_at"]) <= now]
              if OFFER_MISSED_ON_WAKE else [])
    greet_show = f"Ji {address}?"
    greet_hi = f"जी {services.address_hi_of(user)}?" if user.is_md else "जी?"
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
def _with_overrides(p: dict, over: dict) -> dict:
    """The waiting card with the day / time he settled on ("haan" to the offered slot, or "6 baje")."""
    if not over:
        return p
    p = dict(p)
    if p.get("action") == "change_task":
        p["changes"] = {**(p.get("changes") or {}), **over}
    else:
        p.update(over)
        p["due_label"] = assistant._due_label(p.get("due_date", ""), p.get("due_time", ""))
    p["done_reply"] = ""  # the AI's line named the old time
    return p


def _task_data(p: dict) -> dict:
    return {
        "title": (p.get("title") or "").strip(),
        "notes": p.get("notes") or "",
        "kind": p.get("kind") or "TASK",
        "priority": p.get("priority") or "NORMAL",
        "due_date": date.fromisoformat(p["due_date"]) if p.get("due_date") else None,
        "due_time": time.fromisoformat(p["due_time"]) if p.get("due_time") else None,
        "due_at": datetime.fromisoformat(p["due_at"]) if p.get("due_at") else None,  # "in 5 minutes": exact
        "duration_minutes": p.get("duration_minutes") or None,
        "remind_before_minutes": p.get("remind_before_minutes") or None,
        "announcement": p.get("announcement") or "",
    }


# ------------------------------------------------------------------ answering what the background part asked
def _task_ref_from_key(key: str) -> dict | None:
    """A reminder key ("private:<id>:t1700000000:due") -> the task it is about, with its current version."""
    parts = (key or "").split(":")
    if len(parts) < 2 or parts[0] not in ("private", "shared"):
        return None
    model = PrivateTask if parts[0] == "private" else SharedTask
    task = model.objects.filter(id=parts[1]).first()
    return {"scope": parts[0], "id": str(task.id), "version": task.version, "title": task.title} if task else None


def _snooze(user: User, asked: dict | None, until: datetime, show: str, speak: str, lang: str, said_in: str,
            address: str, address_hi: str) -> dict:
    """"Later": the thing that was just asked about, else whatever JARVIS said last, comes back at `until`."""
    try:
        if asked and asked.get("kind") == "reminder" and asked.get("key"):
            services.snooze_reminder(user, asked["key"], until)
        elif asked and asked.get("kind") == "alert" and asked.get("id"):
            services.snooze_alert(user, asked["id"], until)
        elif services.snooze_last_told(user, until) is None:
            return _resp(f"{address}, I'm not sure which reminder you mean. Say its name and I'll set it.",
                         *_twin(said_in, f"{address_hi}, समझ नहीं आया कौन सा रिमाइंडर। उसका नाम बोलिए, मैं रख दूँगा।"),
                         listen=True, action="answer")
    except (ObjectDoesNotExist, ValueError):
        return _resp(f"Sorry {address}, I couldn't find that any more.",
                     *_twin(said_in, f"माफ़ कीजिए {address_hi}, वो अब नहीं मिला।"), listen=True, action="error")
    return _resp(show, speak, lang, listen=True, action="snoozed")


def _answer_asked(user: User, asked: dict, yes: bool, address: str, address_hi: str, lang: str) -> dict:
    """His yes or no to the question the background part asked after a reminder or an alert."""
    kind, title = asked.get("kind"), asked.get("title", "")
    sir, sir_hi = services.md_address(), services.md_address_hi()
    try:
        if kind == "reminder":
            key = asked.get("key", "")
            if not yes:
                ReminderAck.objects.get_or_create(user=user, key=key)  # leave it: it goes quiet, stays on the list
                return _resp(f"Okay {address}, leaving it.", *_twin(lang, f"ठीक है {address_hi}, रहने देता हूँ।"), listen=True, action="answer")
            ref = _task_ref_from_key(key)
            if ref is None:
                raise ObjectDoesNotExist
            to_sir = _complete_one(user, ref)
            ReminderAck.objects.get_or_create(user=user, key=key)
            if to_sir:
                return _resp(f"Sent to {sir} to confirm, {address}.", *_twin(lang, f"{sir_hi} को कन्फ़र्म करने के लिए भेज दिया, {address_hi}।"),
                             listen=True, action="saved")
            return _resp(f"Marked as done, {address}.", *_twin(lang, f"पूरा हुआ मार्क कर दिया, {address_hi}।"), listen=True, action="saved")
        alert = Alert.objects.get(id=asked.get("id"), recipient=user)
        q = services.alert_question(alert) or {}
        if q.get("kind") == "accept" and alert.ref_id:
            if yes:
                services.accept_request(user, alert.ref_id)
                line = (f"Accepted, {address}. It's on your list.", f"मंज़ूर कर दिया, {address_hi}। आपकी लिस्ट में आ गया।")
            else:
                services.decline_request(user, alert.ref_id)
                line = (f"Declined, {address}.", f"मना कर दिया, {address_hi}।")
        elif q.get("kind") == "move" and yes and isinstance(q.get("target"), dict):
            t = q["target"]
            _change_one(user, {"scope": t.get("scope"), "id": t.get("id"), "version": t.get("version") or 1},
                        {"due_date": q.get("to") or (tu.today_local() + timedelta(days=1)).isoformat()}, "Moved from the morning briefing")
            line = (f"Moved {t.get('title', title)} to tomorrow, {address}.", f"{t.get('title', title)} कल पर ले गया, {address_hi}।")
        elif q.get("kind") == "seen" and yes:
            services.tell_sender_seen(user, alert)
            line = (f"Told {sir}, {address}.", f"{sir_hi} को बता दिया, {address_hi}।")
        else:
            line = (f"Okay {address}.", f"ठीक है {address_hi}।")
        if not alert.acknowledged_at:
            alert.acknowledged_at = timezone.now()
            alert.save(update_fields=["acknowledged_at"])
        return _resp(line[0], *_twin(lang, line[1]), listen=True, action="saved")
    except ServiceError as err:
        return _resp(f"Sorry {address}, {err.message}", *_twin(lang, f"माफ़ कीजिए {address_hi}, {err.message}"), listen=True, action="error")
    except ObjectDoesNotExist:
        return _resp(f"Sorry {address}, I couldn't find that any more.",
                     *_twin(lang, f"माफ़ कीजिए {address_hi}, वो अब नहीं मिला।"), listen=True, action="error")


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
    own = SharedTask.objects.filter(id=t["id"], origin=SharedTask.Origin.PA_SELF, created_by=user).exists()
    if own:  # her own task: she changes it herself
        services.update_pa_self_task(user, t["id"], clean, version)
        return False
    if SharedTask.objects.filter(id=t["id"], assignee_type=SharedTask.Assignee.PA).exists():
        he = services.md_pronouns()[0]
        raise ServiceError("forbidden", f"only {services.md_address()} can change a task {he} gave you. You can update its status.", 403)
    services.propose_change(user, t["id"], clean, version, reason)
    return True


def _execute(user: User, p: dict, address: str, lang: str = "en") -> dict:
    """Do what the card says. lang: the language of the "yes" just spoken; the confirmation follows it."""
    a = p.get("action")
    targets = p.get("targets") or ([p["target"]] if p.get("target") else [])
    sir, sir_hi, he = services.md_address(), services.md_address_hi(), services.md_pronouns()[0]
    address_hi = services.address_hi_of(user)
    hi = ""
    try:
        if a in TASK_ACTIONS and not (p.get("title") or "").strip():
            raise ServiceError("invalid", "the task has no title.", 422)
        data = _task_data(p) if a in TASK_ACTIONS else {}
        due_hi = assistant._due_label_hi(p.get("due_date") or "", p.get("due_time") or "")  # noqa: SLF001
        if a == "private_task":
            services.create_private_task(user, data, source=PrivateTask.Source.VOICE)
            when = f" I'll remind you {p['due_label']}." if p.get("due_label") else ""
            spoken = f"Saved, {address}.{when}"
            hi = f"सेव हो गया, {address_hi}।" + (f" {due_hi} याद दिला दूँगा।" if due_hi else "")
        elif a in {"pa_task", "pa_meeting"}:
            data["description"] = p.get("transcript", "")
            services.create_pa_task(user, data, meeting_with_pa=(a == "pa_meeting"))
            pa = p.get("pa_name") or "your PA"
            if a == "pa_meeting":
                spoken = f"Saved, {address}. I've told {pa}, and I'll remind you both."
                hi = f"सेव हो गया, {address_hi}। {pa} को बता दिया है, और आप दोनों को याद दिला दूँगा।"
            else:
                spoken = f"Given to {pa}, {address}. I'll remind you both and tell you when it's done."
                hi = f"{pa} को दे दिया, {address_hi}। आप दोनों को याद दिलाऊँगा और पूरा होने पर बताऊँगा।"
        elif a == "md_request":
            services.create_md_request(user, data)
            spoken = f"Sent to {sir}, {address}. I'll tell you when {he} answers."
            hi = f"{sir_hi} को भेज दिया, {address_hi}। जवाब आते ही बताऊँगा।"
        elif a == "pa_self_task":
            data["description"] = p.get("transcript", "")
            services.create_pa_self_task(user, data)
            when = f" I'll remind you {p['due_label']}." if p.get("due_label") else ""
            spoken = f"Saved in your own tasks, {address}.{when}"
            hi = f"आपके अपने टास्क में सेव हो गया, {address_hi}।" + (f" {due_hi} याद दिला दूँगा।" if due_hi else "")
        elif a == "delegation":
            staff = StaffMember.objects.get(id=(p.get("staff") or {})["id"])
            task, command = services.delegate_to_staff(user, data, staff, category_key=p.get("category_key") or "",
                                                       description=p.get("transcript", ""))
            if command.state == IntegrationCommand.State.REJECTED:
                task.delete()
                raise ServiceError(command.error_code or "rejected",
                                   command.error_message or "the staff app did not accept it.", 422)
            if command.state == IntegrationCommand.State.SUCCEEDED:
                spoken = f"Done, {address}. {staff.name} has the task now. I'll keep track of it."
                hi = f"हो गया, {address_hi}। {staff.name} के पास टास्क पहुँच गया। मैं नज़र रखूँगा।"
            else:
                spoken = f"{address}, sent to {staff.name}. I'm waiting for the staff app to confirm and will tell you."
                hi = f"{address_hi}, {staff.name} को भेज दिया। स्टाफ़ ऐप के कन्फ़र्म करने का इंतज़ार है, बता दूँगा।"
        elif a == "team_delegation":
            ids = [m["id"] for m in (p.get("team") or {}).get("members", [])]
            r = services.delegate_to_team(user, data, ids, category_key=p.get("category_key") or "",
                                          description=p.get("transcript", ""))
            n = len(r["assigned"])
            spoken = f"{address}, given to {n} {'person' if n == 1 else 'people'}."
            hi = f"{address_hi}, {n} लोगों को दे दिया।"
            if r["pending"]:
                spoken += f" {len(r['pending'])} still being confirmed."
                hi += f" {len(r['pending'])} अभी कन्फ़र्म होने बाकी हैं।"
            if r["not_assigned"]:
                names = ", ".join(x["name"] for x in r["not_assigned"])
                spoken += f" Could not give it to: {names}."
                hi += f" इन्हें नहीं दे पाया: {names}।"
        elif a in {"accept_request", "decline_request"}:
            if not targets:
                raise ServiceError("invalid", "I don't know which request.", 422)
            for t in targets:
                if a == "accept_request":
                    services.accept_request(user, t["id"])
                else:
                    services.decline_request(user, t["id"])
            verb = "Accepted" if a == "accept_request" else "Declined"
            verb_hi = "मंज़ूर कर" if a == "accept_request" else "मना कर"
            if len(targets) == 1:
                spoken = f"{verb}, {address}." + (" It's on your list." if a == "accept_request" else "")
                hi = f"{verb_hi} दिया, {address_hi}।" + (" आपकी लिस्ट में आ गया।" if a == "accept_request" else "")
            else:
                spoken = f"{verb} all {len(targets)}, {address}."
                hi = f"सभी {len(targets)} {verb_hi} दिए, {address_hi}।"
        elif a == "complete":
            if not targets:
                raise ServiceError("invalid", "I don't know which task.", 422)
            to_sir = sum(1 for t in targets if _complete_one(user, t))
            if len(targets) == 1:
                spoken = f"Sent to {sir} to confirm, {address}." if to_sir else f"Marked as done, {address}."
                hi = (f"{sir_hi} को कन्फ़र्म करने के लिए भेज दिया, {address_hi}।" if to_sir
                      else f"पूरा हुआ मार्क कर दिया, {address_hi}।")
            else:
                spoken = f"Marked {len(targets)} as done, {address}." + (f" {to_sir} sent to {sir} to confirm." if to_sir else "")
                hi = f"{len(targets)} पूरे हुए मार्क कर दिए, {address_hi}।" + (f" {to_sir} {sir_hi} को कन्फ़र्म करने भेजे।" if to_sir else "")
        elif a == "remember":
            services.add_memory(user, p.get("memory_text", ""))
            spoken = f"Done, {address}. I'll remember that."
            hi = f"हो गया, {address_hi}। याद रखूँगा।"
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
            what_hi = "बदल" if a == "change_task" else "हटा"
            if to_sir:
                spoken = f"Sent to {sir} to confirm, {address}."
                hi = f"{sir_hi} को कन्फ़र्म करने के लिए भेज दिया, {address_hi}।"
            elif len(targets) == 1:
                spoken = f"{what}, {address}." + (" It's cancelled, not erased." if a == "delete_task" else "")
                hi = f"{what_hi} दिया, {address_hi}।" + (" कैंसल हुआ है, मिटा नहीं।" if a == "delete_task" else "")
            else:
                spoken = f"{what} {len(targets)}, {address}."
                hi = f"{len(targets)} {what_hi} दिए, {address_hi}।"
        else:
            raise ServiceError("invalid", "I can't do that from here.", 422)
    except ServiceError as err:
        return _resp(f"Sorry {address}, it was not saved: {err.message}",
                     *_twin(lang, f"माफ़ कीजिए {address_hi}, सेव नहीं हुआ: {err.message}"), listen=True, action="error")
    except ObjectDoesNotExist:
        return _resp(f"Sorry {address}, I couldn't find that any more.",
                     *_twin(lang, f"माफ़ कीजिए {address_hi}, वो अब नहीं मिला।"), listen=True, action="error")
    except Exception:  # noqa: BLE001 — a background voice loop must always get an answer
        log.exception("background execute failed")
        return _resp(f"Sorry {address}, something went wrong and it was not saved.",
                     *_twin(lang, f"माफ़ कीजिए {address_hi}, कुछ गड़बड़ हो गई और सेव नहीं हुआ।"), listen=True, action="error")

    if p.get("done_reply") and a in AI_DONE_OK:
        return _resp(p["done_reply"], p.get("done_speak") or p["done_reply"], p.get("done_lang", "en"),
                     listen=True, action="saved")
    return _resp(spoken, *_twin(lang, hi), listen=True, action="saved")
