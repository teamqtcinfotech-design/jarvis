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
PERSON_ACTIONS = {"person_task", "person_meeting", "own_task", "message", "organise"}
# The names older phones (and cards saved before the team) still use, and what they mean now.
LEGACY = {"pa_task": "person_task", "mg_task": "person_task", "pa_meeting": "person_meeting", "pa_self_task": "own_task"}


def _task_data(p: dict) -> dict:
    data = {
        "title": (p.get("title") or "").strip(),
        "notes": p.get("notes") or "",
        "kind": p.get("kind") if p.get("kind") in {"TASK", "MEETING", "CALL", "PAYMENT", "FOLLOW_UP", "APPROVAL"} else "TASK",
        "priority": p.get("priority") if p.get("priority") in {"URGENT", "HIGH", "NORMAL", "LOW"} else "NORMAL",
        "due_date": date.fromisoformat(p["due_date"]) if p.get("due_date") else None,
        "due_time": time.fromisoformat(p["due_time"]) if p.get("due_time") else None,
        "due_at": datetime.fromisoformat(p["due_at"]) if p.get("due_at") else None,  # "in 5 minutes": exact
        "duration_minutes": p.get("duration_minutes") or None,
        "remind_before_minutes": p.get("remind_before_minutes") or None,
        "announcement": p.get("announcement") or "",
        "description": (p.get("transcript") or "")[:5000],
    }
    # Nov 2026: the repeat rule, the checklist, the amount (each only when the card carries it).
    if p.get("repeat"):
        data["repeat"] = str(p["repeat"])[:40]
    if p.get("repeat_until"):
        try:
            data["repeat_until"] = date.fromisoformat(str(p["repeat_until"])[:10])
        except ValueError:
            pass
    if isinstance(p.get("steps"), list) and p["steps"]:
        data["steps"] = [s for s in p["steps"] if isinstance(s, (str, dict))][:40]
    if p.get("amount"):
        data["amount"] = p["amount"]
    return data


def _repeat_tail(data: dict) -> tuple[str, str]:
    """" It repeats every month on the 10th." (and the Hindi twin) for a repeating item, else nothing."""
    from . import recurrence  # noqa: PLC0415
    en, hi = recurrence.describe(recurrence.settle(str(data.get("repeat") or ""), data.get("due_date") or tu.today_local()))
    return (f" It repeats {en}.", f" ये {hi} दोहराया जाएगा।") if en else ("", "")


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
    due_en = p.get("due_label") or ""
    from .assistant import _due_label_hi  # noqa: PLC0415 — assistant imports this module
    due_hi = _due_label_hi(p.get("due_date") or "", p.get("due_time") or "")
    if action in {"person_task", "person_meeting", "own_task", "private_task", "md_request"} and not (p.get("title") or "").strip():
        raise ServiceError("invalid", "the task has no title.", 422)
    data = _task_data(p)
    rep_en, rep_hi = _repeat_tail(data)
    # Every line here states who and what, short (his rule, 24 Nov 2026): never a bare "done" or "sent".
    if action == "private_task":
        task = services.create_private_task(user, data, source=PrivateTask.Source.VOICE)
        when = f" I'll remind you {due_en}." if due_en else ""
        return {"en": f"Saved: {task.title}.{when}{rep_en}", "hi": f"सेव हो गया: {task.title}।" + (f" {due_hi} याद दिला दूँगा।" if due_hi else "") + rep_hi,
                "undo": [{"type": "private", "id": str(task.id)}], "title": task.title}
    if action == "own_task":
        task = services.create_own_task(user, data)
        when = f" I'll remind you {due_en}." if due_en else ""
        return {"en": f"Saved in your own work: {task.title}.{when}{rep_en}",
                "hi": f"आपके अपने काम में सेव हो गया: {task.title}।" + (f" {due_hi} याद दिला दूँगा।" if due_hi else "") + rep_hi,
                "undo": [{"type": "shared", "id": str(task.id)}], "title": task.title}
    if action == "md_request":
        # Work for the MD from anyone else: straight onto his list (ringing until he sees it), like work for anyone.
        md = services.get_md()
        if md is None or md.id == user.id:
            raise ServiceError("no_person", "I don't know who should get this.", 422)
        task = services.create_person_task(user, md, data)
        sir, sir_hi, he = services.md_address(), services.md_address_hi(), services.md_pronouns()[0]
        if task.kind == "APPROVAL":
            amt = services.rupees(task.amount)
            return {"en": f"I've asked {sir}'s approval" + (f" for {amt}" if amt else "") + f". I'll tell you what {he} decides.",
                    "hi": f"{sir_hi} से" + (f" {amt} की" if amt else "") + " मंज़ूरी माँग ली है। फ़ैसला आने पर बताऊँगा।",
                    "undo": [{"type": "shared", "id": str(task.id)}], "title": task.title, "people": [md.spoken_name]}
        when = f" for {due_en}" if due_en else ""
        return {"en": f"It's on {sir}'s list{when}: {task.title}.{rep_en}",
                "hi": f"{sir_hi} की लिस्ट में डाल दिया" + (f", {due_hi}" if due_hi else "") + f": {task.title}।{rep_hi}",
                "undo": [{"type": "shared", "id": str(task.id)}], "title": task.title, "people": [md.spoken_name]}
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
            # One person (the MD included): a note composed in the giver's words; a group: the plain line (no waiting
            # on the AI per person).
            task = services.create_person_task(user, person, data, meeting=meeting, compose_note=len(people) == 1)
            refs.append({"type": "shared", "id": str(task.id)})
            names.append(person)
        who, who_hi = services.names_phrase(names), services.names_phrase(names, hindi=True)
        title = " ".join(str(p.get("title") or "").split())
        if not names:
            en, hi = f"Saved: {title}.{rep_en}", f"सेव हो गया: {title}।{rep_hi}"
        elif meeting:
            # "Himanshu ko 4 baje aane ke liye bol diya hai": who was told to come, and when.
            en = f"{who} {'has' if len(names) == 1 else 'have'} been told to come{f' {due_en}' if due_en else ''}.{rep_en}"
            hi = f"{who_hi} को{f' {due_hi}' if due_hi else ''} आने के लिए बोल दिया है।{rep_hi}"
        elif data["kind"] == "APPROVAL":
            amt = services.rupees(services.clean_amount(data.get("amount")))
            en = f"I've asked {who}'s approval" + (f" for {amt}" if amt else "") + ". I'll tell you what they decide."
            hi = f"{who_hi} से" + (f" {amt} की" if amt else "") + " मंज़ूरी माँग ली है। फ़ैसला आने पर बताऊँगा।"
        else:
            # "Nikki ko kal tak reconciliation karne ko bol diya hai": who has what, by when.
            en = f"{who} {'has' if len(names) == 1 else 'have'} it{f' for {due_en}' if due_en else ''}: {title}.{rep_en}"
            hi = f"{who_hi} को{f' {due_hi} तक' if due_hi else ''} {title} करने को बोल दिया है।{rep_hi}"
        return {"en": en, "hi": hi, "undo": refs, "title": p.get("title", ""), "people": [u.spoken_name for u in names]}
    if action == "arrange_request":
        # "Nikki ko bolo Sharmaji wali meeting abhi arrange karwa de": Nikki gets the ask (ringing until seen); the
        # meeting stays where it is until she marks it done, then it moves and everyone concerned is told.
        people = _people_of(user, p)
        about = p.get("about") if isinstance(p.get("about"), dict) else {}
        if not people or not about.get("id"):
            raise ServiceError("no_person", "I don't know who should arrange this.", 422)
        task = services.create_arrange_request(user, people[0], str(about.get("scope") or "shared"), about["id"],
                                               p.get("arrange") if isinstance(p.get("arrange"), dict) else {}, p.get("transcript") or "")
        who = people[0].spoken_name
        meeting = task.arrange.get("about_title") or "the meeting"
        was, was_hi = task.arrange.get("from_label") or "", task.arrange.get("from_label_hi") or ""
        to, to_hi = task.arrange.get("to_label") or "", task.arrange.get("to_label_hi") or ""
        en = (f"I've asked {who} to move your {meeting}" + (f" ({was})" if was else "") + (f" to {to}" if to else "")
              + ". It stays as it is until " + ("she" if services.gender_of(people[0]) == "female" else "he" if services.gender_of(people[0]) == "male" else "they")
              + " confirm" + ("s" if services.gender_of(people[0]) in ("female", "male") else "") + "; I'll tell you then.")
        hi = (f"{who} को कहा है कि आपकी {meeting}" + (f" ({was_hi})" if was_hi else "") + (f" {to_hi} पर कर दे" if to_hi else "")
              + "। कन्फ़र्म होने तक वो वैसी ही रहेगी; तब बताऊँगा।")
        return {"en": en, "hi": hi, "undo": [{"type": "shared", "id": str(task.id)}], "title": task.title, "people": [who]}
    if action == "organise":
        # A department or section change the speaker confirmed (removing people, deleting, merging): see organise.py.
        from . import organise  # noqa: PLC0415 — organise imports this module
        plan = p.get("organise") if isinstance(p.get("organise"), dict) else {}
        r = organise.apply(user, plan)
        return {"en": r["en"], "hi": r["hi"], "undo": r.get("undo") or [], "title": p.get("title") or "", "view": r.get("view")}
    if action == "message":
        people = _people_of(user, p)
        text = " ".join(str(p.get("message") or p.get("notes") or p.get("title") or "").split())
        when = _when_of(p)
        alerts = services.send_message_to(user, people, text, when=when,
                                          priority=p.get("priority") if p.get("priority") in {"URGENT", "HIGH", "NORMAL", "LOW"} else "NORMAL")
        who, who_hi = services.names_phrase(people), services.names_phrase(people, hindi=True)
        if when:
            en = f"I'll send it to {who} at {tu.fmt_time(when)}."
            hi = f"{tu.fmt_time_hi(when)} बजे {who_hi} को भेज दूँगा।"
        else:
            en, hi = f"The message has gone to {who}.", f"ये मैसेज {who_hi} को भेज दिया है।"
        return {"en": en, "hi": hi, "undo": [{"type": "alerts", "ids": [str(a.id) for a in alerts]}],
                "title": text[:80], "people": [u.spoken_name for u in people]}
    raise ServiceError("invalid", "I can't do that here.", 422)


def item_name(item: dict) -> str:
    """How a card item is named when it fails (a message by its words, else its title)."""
    return (item.get("title") or item.get("message") or "one item") if isinstance(item, dict) else "one item"


def _sent_line(lines: list[dict]) -> tuple[str, str]:
    """"Sent to Nikki, Priya and Gauri." / "Nikki, Priya और Gauri को भेज दिया।" for a list done together (his rule, 27 Nov
    2026: the people, never the items one by one); the speaker's own items among them are counted ("and 1 saved on
    your list"). A list that is all his own keeps each one said as it was done, never a bare "all saved" (his rule,
    24 Nov 2026)."""
    people: list[str] = []
    for r in lines:
        for name in r.get("people") or []:
            if name not in people:
                people.append(name)
    own = sum(1 for r in lines if not r.get("people"))
    if not people:
        return " ".join(r["en"] for r in lines), " ".join(r["hi"] for r in lines)
    who = people[0] if len(people) == 1 else ", ".join(people[:-1]) + " and " + people[-1]
    who_hi = people[0] if len(people) == 1 else ", ".join(people[:-1]) + " और " + people[-1]
    en = f"Sent to {who}." + (f" And {own} saved on your list." if own else "")
    hi = f"{who_hi} को भेज दिया।" + (f" और {own} आपकी लिस्ट में सेव हो गए।" if own else "")
    return en, hi


def run_card(user: User, card: dict) -> dict:
    """Do a whole card: the main item and every further one said in the same breath. Returns {"en", "hi", "saved": n,
    "failed": [(title, message)], "undo": [refs]}; what was saved is remembered for undo."""
    items = [card] + [b for b in (card.get("batch") or []) if isinstance(b, dict)]
    saved, failed, refs, lines = 0, [], [], []
    for n, item in enumerate(items):
        sched = item.get("schedule") if isinstance(item.get("schedule"), dict) else {}
        if n > 0 and sched.get("hard") and not card.get("overrides_for_batch"):
            # A further item over a meeting already there: never saved over it. It is left out, named, and he says
            # it again with a time (the main item's clash is settled through the card's own question).
            first = (sched.get("clashes") or [{}])[0]
            failed.append((item_name(item), f"it is at the same time as {first.get('title') or 'another meeting'}; say it again with a time"))
            continue
        try:
            r = run_item(user, item)
        except ServiceError as err:
            failed.append((item_name(item), err.message))
            continue
        except (ObjectDoesNotExist, ValueError, KeyError) as err:
            log.info("card item not done: %s", type(err).__name__)
            failed.append((item_name(item), "it could not be found any more"))
            continue
        saved += 1
        refs += r["undo"]
        lines.append(r)
    if len(items) == 1 and lines:
        en, hi = lines[0]["en"], lines[0]["hi"]
    elif not failed:
        # A list he has already seen and sent: one line naming the people it went to, never the items one by one
        # (his rule, 27 Nov 2026); what was his own is counted, and a list all his own says each as it was done.
        en, hi = _sent_line(lines)
    else:
        names = "; ".join(f"{t} ({m.rstrip('.')})" for t, m in failed)
        en = f"Saved {saved} of {len(items)}. Could not save: {names}."
        hi = f"{len(items)} में से {saved} सेव हुए। ये नहीं हो पाए: {names}।"
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
    kept: list[tuple[str, str]] = []  # (title, staff name): work in the staff panel, which cannot be taken back from here
    for ref in last.get("refs") or []:
        try:
            panel = _panel_task(user, ref)
            if panel is not None:
                kept.append(panel)
                continue
            undone += _undo_one(user, ref)
        except (ObjectDoesNotExist, ValueError, ServiceError) as err:
            log.info("undo skipped one item: %s", type(err).__name__)
    row, data = _state(user)
    data.pop("last_done", None)
    if row:
        row.state_json = json.dumps(data, ensure_ascii=False, default=str)
        row.save(update_fields=["state_json", "updated_at"])
    services.audit(user, "undo", "user", user.id, f"{undone} item(s), {len(kept)} kept in the panel")
    if kept:
        # Honest about the panel (27 Nov 2026): its gateway has no cancel, so a task given to staff stays with them
        # until it is cancelled in the panel itself; JARVIS keeps its record too, so the lists never disagree.
        stays, stays_hi = _stays_line(kept)
        en = (f"Undone, {address}. But {stays}" if undone else f"{address}, {stays}")
        hi = (f"वापस ले लिया, {address_hi}। लेकिन {stays_hi}" if undone else f"{address_hi}, {stays_hi}")
        return {"en": en, "hi": hi, "undone": undone, "kept": [t for t, _s in kept]}
    return {"en": f"Undone, {address}." if undone else f"{address}, that was already changed, so nothing was undone.",
            "hi": f"वापस ले लिया, {address_hi}।" if undone else f"{address_hi}, वो पहले ही बदल चुका था, कुछ वापस नहीं हुआ।",
            "undone": undone}


def _panel_task(user: User, ref: dict) -> tuple[str, str] | None:
    """(title, staff name) when the ref is work given to someone in the staff panel; None otherwise."""
    if ref.get("type") != "shared":
        return None
    task = SharedTask.objects.filter(id=ref.get("id"), created_by=user).select_related("staff").first()
    if task is None or task.assignee_type != SharedTask.Assignee.STAFF:
        return None
    return task.title, task.staff.name if task.staff_id else "the staff"


def _stays_line(kept: list[tuple[str, str]]) -> tuple[str, str]:
    """"X stays with Nikki: I can't take a panel task back from here; it has to be cancelled in the panel." (+ Hindi)."""
    if len(kept) == 1:
        title, who = kept[0]
        return (f"{title} stays with {who}: I can't take a panel task back from here; it has to be cancelled in the panel.",
                f"{title} {who} के पास ही रहेगा: पैनल का टास्क यहाँ से वापस नहीं लिया जा सकता, उसे पैनल में ही कैंसल करना होगा।")
    names = "; ".join(f"{t} ({w})" for t, w in kept[:4])
    return (f"these stay in the panel: {names}. I can't take a panel task back from here; they have to be cancelled in the panel.",
            f"ये पैनल में ही रहेंगे: {names}। पैनल का टास्क यहाँ से वापस नहीं लिया जा सकता, उन्हें पैनल में ही कैंसल करना होगा।")


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
    if kind == "placed":
        # A free-time plan he said yes to: the item goes back to having no slot (or the slot it had before).
        model = PrivateTask if ref.get("scope") == "private" else SharedTask
        task = model.objects.filter(id=ref["id"]).first()
        if task is None:
            return 0
        before = ref.get("before") or {}
        task.scheduled_start = datetime.fromisoformat(before["scheduled_start"]) if before.get("scheduled_start") else None
        task.scheduled_end = datetime.fromisoformat(before["scheduled_end"]) if before.get("scheduled_end") else None
        task.version += 1
        task.save(update_fields=["scheduled_start", "scheduled_end", "version", "updated_at"])
        return 1
    if kind == "cancelled":
        # "Delete it" done at once: undo puts the item back as it was.
        model = PrivateTask if ref.get("scope") == "private" else SharedTask
        task = model.objects.get(id=ref["id"]) if model is SharedTask else model.objects.get(id=ref["id"], owner=user)
        if task.status == Status.CANCELLED:
            task.status = ref.get("before") or Status.OPEN
            task.version += 1
            task.save(update_fields=["status", "version", "updated_at"])
            return 1
        return 0
    if kind == "moved":
        # "Jab free ho tab rakh do" done at once: undo puts the item back at its old day and time.
        before = ref.get("before") if isinstance(ref.get("before"), dict) else {}
        d, t = str(before.get("due_date") or ""), str(before.get("due_time") or "")
        if not d:
            return 0
        changes = {"due_date": date.fromisoformat(d), "due_time": time.fromisoformat(t) if t else None}
        if ref.get("scope") == "private":
            task = PrivateTask.objects.get(id=ref["id"], owner=user)
            services.update_private_task(user, ref["id"], changes, task.version)
        else:
            task = SharedTask.objects.get(id=ref["id"])
            services.change_shared_task(user, ref["id"], changes, task.version)
        return 1
    if kind == "edited":
        # A title, priority, kind or length changed at once by voice: undo puts the old values back.
        before = ref.get("before") if isinstance(ref.get("before"), dict) else {}
        if not before:
            return 0
        if ref.get("scope") == "private":
            task = PrivateTask.objects.get(id=ref["id"], owner=user)
            services.update_private_task(user, ref["id"], before, task.version)
        else:
            task = SharedTask.objects.get(id=ref["id"])
            services.change_shared_task(user, ref["id"], before, task.version)
        return 1
    if kind == "organise":
        from . import organise  # noqa: PLC0415 — organise imports this module
        return organise.undo(user, ref.get("inverse") or {})
    return 0
