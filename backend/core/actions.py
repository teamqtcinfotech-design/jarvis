"""Doing what a card says, in one place: the app's "save", the background voice's "yes", and JARVIS's own saving of a
clear instruction ("Sunil ko bolo kal tak bank ka kaam kar de" needs no "shall I save it?") all come here, through the
same permission-checked service functions. Every save is remembered for a minute or two so "undo" can take it back.

A card item is untrusted (it travels through the phone): every person is looked up again by id on the server, and
the service functions refuse anything the speaker could not do by hand.
"""
import json
import logging
from datetime import date, datetime, time, timedelta

from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from . import services
from . import timeutils as tu
from .models import Alert, ConversationState, Priority, PrivateTask, SharedTask, Status, TaskRequest, User
from .services import ServiceError

log = logging.getLogger("jarvis.actions")

UNDO_WINDOW = timedelta(minutes=2)  # said as "a minute" to the person; a little longer in practice

# Card actions that put something new in somebody's list (or send a message) and that JARVIS saves by itself when
# the instruction was complete and clear.
PERSON_ACTIONS = {"person_task", "person_meeting", "own_task", "message"}
# The names older phones (and cards saved before the team) still use, and what they mean now.
LEGACY = {"pa_task": "person_task", "mg_task": "person_task", "pa_meeting": "person_meeting", "pa_self_task": "own_task"}


def _task_data(p: dict) -> dict:
    return {
        "title": (p.get("title") or "").strip(),
        "notes": p.get("notes") or "",
        "kind": p.get("kind") if p.get("kind") in {"TASK", "MEETING", "CALL", "PAYMENT", "FOLLOW_UP"} else "TASK",
        "priority": p.get("priority") if p.get("priority") in {"URGENT", "HIGH", "NORMAL", "LOW"} else "NORMAL",
        "due_date": date.fromisoformat(p["due_date"]) if p.get("due_date") else None,
        "due_time": time.fromisoformat(p["due_time"]) if p.get("due_time") else None,
        "due_at": datetime.fromisoformat(p["due_at"]) if p.get("due_at") else None,  # "in 5 minutes": exact
        "duration_minutes": p.get("duration_minutes") or None,
        "remind_before_minutes": p.get("remind_before_minutes") or None,
        "announcement": p.get("announcement") or "",
        "description": (p.get("transcript") or "")[:5000],
    }


def _people_of(user: User, p: dict) -> list[User]:
    """The people a card item is for, looked up again on the server (never trusted from the phone)."""
    ids = []
    for a in p.get("assignees") or []:
        if isinstance(a, dict):
            try:
                ids.append(int(a.get("id")))
            except (TypeError, ValueError):
                continue
    if not ids:
        legacy = p.get("action")
        person = services.get_pa() if legacy in ("pa_task", "pa_meeting") else services.get_manager() if legacy == "mg_task" else None
        return [person] if person else []
    found = {u.id: u for u in User.objects.filter(id__in=ids, is_active=True)}
    return [found[i] for i in ids if i in found]


def _when_of(p: dict):
    """The moment a message is to be delivered (None = now)."""
    when = None
    if p.get("due_at"):
        try:
            when = datetime.fromisoformat(str(p["due_at"]))
        except ValueError:
            when = None
    if when is None and p.get("due_date") and p.get("due_time"):
        when = tu.combine_local(date.fromisoformat(p["due_date"]), time.fromisoformat(p["due_time"]))
    if when and when <= tu.now_local() + timedelta(seconds=30):
        return None
    return when


def run_item(user: User, p: dict) -> dict:
    """Do one card item. Returns {"en", "hi", "undo": [refs], "saved": True}; raises ServiceError / ObjectDoesNotExist.
    Only the actions of the team (and the speaker's own items) are done here; the rest stay with their callers."""
    action = LEGACY.get(p.get("action"), p.get("action"))
    address, address_hi = services.address_of(user), services.address_hi_of(user)
    due_en = p.get("due_label") or ""
    from .assistant import _due_label_hi  # noqa: PLC0415 — assistant imports this module
    due_hi = _due_label_hi(p.get("due_date") or "", p.get("due_time") or "")
    if action in {"person_task", "person_meeting", "own_task", "private_task", "md_request"} and not (p.get("title") or "").strip():
        raise ServiceError("invalid", "the task has no title.", 422)
    data = _task_data(p)
    if action == "private_task":
        task = services.create_private_task(user, data, source=PrivateTask.Source.VOICE)
        when = f" I'll remind you {due_en}." if due_en else ""
        return {"en": f"Saved, {address}.{when}", "hi": f"सेव हो गया, {address_hi}।" + (f" {due_hi} याद दिला दूँगा।" if due_hi else ""),
                "undo": [{"type": "private", "id": str(task.id)}], "title": task.title}
    if action == "own_task":
        task = services.create_own_task(user, data)
        when = f" I'll remind you {due_en}." if due_en else ""
        return {"en": f"Saved in your own work, {address}.{when}",
                "hi": f"आपके अपने काम में सेव हो गया, {address_hi}।" + (f" {due_hi} याद दिला दूँगा।" if due_hi else ""),
                "undo": [{"type": "shared", "id": str(task.id)}], "title": task.title}
    if action == "md_request":
        req = services.create_md_request(user, {k: v for k, v in data.items() if k != "description"})
        sir, sir_hi, he = services.md_address(), services.md_address_hi(), services.md_pronouns()[0]
        return {"en": f"Sent to {sir}, {address}. I'll tell you when {he} answers.",
                "hi": f"{sir_hi} को भेज दिया, {address_hi}। जवाब आते ही बताऊँगा।",
                "undo": [{"type": "request", "id": str(req.id)}], "title": p.get("title", "")}
    if action in {"person_task", "person_meeting"}:
        people = _people_of(user, p)
        if not people:
            raise ServiceError("no_person", "I don't know who should get this.", 422)
        meeting = action == "person_meeting"
        refs, names = [], []
        for person in people[:services.TEAM_LIMIT]:
            if person.id == user.id:
                task = services.create_own_task(user, data) if not user.is_md else \
                    services.create_private_task(user, data, source=PrivateTask.Source.VOICE)
                refs.append({"type": "shared" if not user.is_md else "private", "id": str(task.id)})
                continue
            if person.is_md and not user.is_md:
                req = services.create_md_request(user, {k: v for k, v in data.items() if k != "description"})
                refs.append({"type": "request", "id": str(req.id)})
                names.append(person)
                continue
            # One person: a note composed in the giver's words; a group: the plain line (no waiting on the AI per person).
            task = services.create_person_task(user, person, data, meeting=meeting, compose_note=len(people) == 1)
            refs.append({"type": "shared", "id": str(task.id)})
            names.append(person)
        who, who_hi = services.names_phrase(names), services.names_phrase(names, hindi=True)
        if meeting:
            en = f"Saved, {address}. I've told {who}, and I'll remind you both."
            hi = f"सेव हो गया, {address_hi}। {who_hi} को बता दिया है, और आप दोनों को याद दिला दूँगा।"
        else:
            when = f" for {due_en}" if due_en else ""
            en = f"Done, {address}: given to {who}{when}. I'll tell you when it's done."
            hi = f"हो गया, {address_hi}: {who_hi} को दे दिया" + (f", {due_hi}" if due_hi else "") + "। पूरा होने पर बताऊँगा।"
        return {"en": en, "hi": hi, "undo": refs, "title": p.get("title", ""), "people": [u.spoken_name for u in names]}
    if action == "message":
        people = _people_of(user, p)
        text = " ".join(str(p.get("message") or p.get("notes") or p.get("title") or "").split())
        when = _when_of(p)
        alerts = services.send_message_to(user, people, text, when=when,
                                          priority=p.get("priority") if p.get("priority") in {"URGENT", "HIGH", "NORMAL", "LOW"} else "NORMAL")
        who, who_hi = services.names_phrase(people), services.names_phrase(people, hindi=True)
        if when:
            en = f"Okay {address}, I'll send it to {who} at {tu.fmt_time(when)}."
            hi = f"ठीक है {address_hi}, {tu.fmt_time_hi(when)} बजे {who_hi} को भेज दूँगा।"
        else:
            en, hi = f"Sent to {who}, {address}.", f"{who_hi} को भेज दिया, {address_hi}।"
        return {"en": en, "hi": hi, "undo": [{"type": "alerts", "ids": [str(a.id) for a in alerts]}],
                "title": text[:80], "people": [u.spoken_name for u in people]}
    raise ServiceError("invalid", "I can't do that here.", 422)


def run_card(user: User, card: dict) -> dict:
    """Do a whole card: the main item and every further one said in the same breath. Returns {"en", "hi", "saved": n,
    "failed": [(title, message)], "undo": [refs]}; what was saved is remembered for undo."""
    items = [card] + [b for b in (card.get("batch") or []) if isinstance(b, dict)]
    saved, failed, refs, lines = 0, [], [], []
    for item in items:
        try:
            r = run_item(user, item)
        except ServiceError as err:
            failed.append((item.get("title") or "one item", err.message))
            continue
        except (ObjectDoesNotExist, ValueError, KeyError) as err:
            log.info("card item not done: %s", type(err).__name__)
            failed.append((item.get("title") or "one item", "it could not be found any more"))
            continue
        saved += 1
        refs += r["undo"]
        lines.append(r)
    address, address_hi = services.address_of(user), services.address_hi_of(user)
    if len(items) == 1 and lines:
        en, hi = lines[0]["en"], lines[0]["hi"]
    elif not failed:
        en, hi = f"Done, {address}: all {saved} saved.", f"हो गया, {address_hi}: सभी {saved} सेव हो गए।"
    else:
        names = "; ".join(f"{t} ({m.rstrip('.')})" for t, m in failed)
        en = f"Saved {saved} of {len(items)}, {address}. Could not save: {names}."
        hi = f"{len(items)} में से {saved} सेव हुए, {address_hi}। ये नहीं हो पाए: {names}।"
    if refs:
        remember_done(user, refs, card.get("title") or "")
    return {"en": en, "hi": hi, "saved": saved, "failed": failed, "undo": refs}


# ------------------------------------------------------------------ undo
def _state(user: User) -> tuple[ConversationState | None, dict]:
    row = ConversationState.objects.filter(user=user).first()
    try:
        return row, (json.loads(row.state_json or "{}") if row else {})
    except ValueError:
        return row, {}


def remember_done(user: User, refs: list, label: str = "") -> None:
    """What was just saved, so "undo" (said, or the Undo button) can take it back within the window."""
    row, data = _state(user)
    data["last_done"] = {"refs": refs[:50], "label": (label or "")[:120], "at": timezone.now().isoformat()}
    ConversationState.objects.update_or_create(user=user, defaults={"state_json": json.dumps(data, ensure_ascii=False, default=str)})


def last_done(user: User) -> dict | None:
    _row, data = _state(user)
    last = data.get("last_done")
    if not isinstance(last, dict) or not last.get("at"):
        return None
    try:
        if timezone.now() - datetime.fromisoformat(last["at"]) > UNDO_WINDOW:
            return None
    except (TypeError, ValueError):
        return None
    return last


def undo_last(user: User) -> dict:
    """Take back what was saved last (within the window). Work and messages that already reached someone are
    withdrawn with a one-line note to them, so nobody acts on something that was a mistake. Returns {"en", "hi",
    "undone": n}; raises ServiceError when there is nothing to undo."""
    last = last_done(user)
    address, address_hi = services.address_of(user), services.address_hi_of(user)
    if not last:
        raise ServiceError("nothing", "there is nothing to undo now.", 422)
    undone = 0
    for ref in last.get("refs") or []:
        try:
            undone += _undo_one(user, ref)
        except (ObjectDoesNotExist, ValueError, ServiceError) as err:
            log.info("undo skipped one item: %s", type(err).__name__)
    row, data = _state(user)
    data.pop("last_done", None)
    if row:
        row.state_json = json.dumps(data, ensure_ascii=False, default=str)
        row.save(update_fields=["state_json", "updated_at"])
    services.audit(user, "undo", "user", user.id, f"{undone} item(s)")
    return {"en": f"Undone, {address}." if undone else f"{address}, that was already changed, so nothing was undone.",
            "hi": f"वापस ले लिया, {address_hi}।" if undone else f"{address_hi}, वो पहले ही बदल चुका था, कुछ वापस नहीं हुआ।",
            "undone": undone}


def _withdrawn_note(user: User, person: User, title: str) -> None:
    who, who_hi = services.giver_words(user)
    Alert.objects.create(recipient=person, sender=user, kind=Alert.Kind.INFO, priority=Priority.NORMAL,
                         title=f"{who} took that back", body=title[:400],
                         spoken_text=f"{services.address_of(person)}, please ignore that: {who} has taken back {title}.",
                         spoken_text_hi=f"{services.address_hi_of(person)}, उसे छोड़ दीजिए: {who_hi} ने {title} वापस ले लिया है।",
                         ref_type="undo", ref_id=str(person.id))


def _undo_one(user: User, ref: dict) -> int:
    kind = ref.get("type")
    if kind == "private":
        PrivateTask.objects.get(id=ref["id"], owner=user).delete()
        return 1
    if kind == "shared":
        task = SharedTask.objects.select_related("assignee_user").get(id=ref["id"], created_by=user)
        person = task.assignee_user
        told = Alert.objects.filter(ref_type="shared_task", ref_id=str(task.id), delivered_at__isnull=False).exists()
        Alert.objects.filter(ref_type="shared_task", ref_id=str(task.id)).delete()
        title = task.title
        task.delete()
        if told and person is not None and person.id != user.id:
            _withdrawn_note(user, person, title)
        return 1
    if kind == "request":
        req = TaskRequest.objects.get(id=ref["id"], requested_by=user)
        if req.state != TaskRequest.State.PENDING:
            return 0
        services.withdraw_request(user, req.id)
        return 1
    if kind == "alerts":
        n = 0
        for alert in Alert.objects.filter(id__in=ref.get("ids") or [], sender=user).select_related("recipient"):
            if alert.delivered_at is not None:
                _withdrawn_note(user, alert.recipient, alert.body[:80])
            alert.delete()
            n += 1
        return n
    if kind == "status":
        task = SharedTask.objects.get(id=ref["id"])
        if task.status == Status.COMPLETED:
            task.status, task.completed_at = ref.get("before") or Status.OPEN, None
            task.version += 1
            task.save(update_fields=["status", "completed_at", "version", "updated_at"])
            return 1
        return 0
    return 0
