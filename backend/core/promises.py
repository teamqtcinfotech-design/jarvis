"""Answers to the questions JARVIS asks about work between people, read from the person's own words.

  promise   asked of the person a task was given to, once they see it: "Shall I tell Sir when you'll do it?"
            "kal shaam tak" -> kept on the task, Sir hears it; "nahi ho payega, bank band hai" -> Sir hears the reason
            and is asked whether to give it to someone else; a bare "haan" -> "By when?"; "nahi" -> nothing more.
  unseen    asked of the sender when it rang for its full time unseen: "Ring again, or give it to someone else?"
  reassign  asked of the giver after "can't do it": "Shall I give it to someone else?" ("haan, Sunil ko")

Any answer to "promise" counts as seen. The AI reads the words when it is reachable (one small request of its own; the
command schema is never touched), a rule reading covers the rest, and words that are about something else entirely
go back to the normal conversation (handled = False).
"""
import json
import logging
import re
from datetime import date, datetime, time, timedelta

import anthropic
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from . import ai_brain, ai_usage, hinglish_parser, services
from . import lang as lang_mod
from . import timeutils as tu
from .models import Alert, AiUsage, SharedTask, User
from .services import ServiceError

log = logging.getLogger("jarvis.promises")

FREE_KINDS = {"promise", "unseen", "reassign"}
ANSWERS = ("time", "cannot", "yes", "no", "give", "other")

READ_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "enum": list(ANSWERS)},
        "due_date": {"type": "string", "description": "YYYY-MM-DD, or empty"},
        "due_time": {"type": "string", "description": "HH:MM (24h), or empty"},
        "in_minutes": {"type": "integer", "description": "for 'in N minutes / hours'; -1 when not used"},
        "reason": {"type": "string"},
        "person": {"type": "string"},
    },
    "required": ["answer", "due_date", "due_time", "in_minutes", "reason", "person"],
    "additionalProperties": False,
}

READ_PROMPT = """You read one short spoken answer to a question JARVIS (an office assistant in India) has just asked, and say what it means. The answer is English, Hindi or Hinglish as a phone's speech recogniser wrote it (often Hindi in Roman letters, sometimes misheard).

The question is one of:
- promise: JARVIS asked the person a task was given to: "Shall I tell <giver> when you'll do it?"
- unseen: JARVIS asked the giver: "<person> hasn't seen it yet. Ring again, or give it to someone else?"
- reassign: JARVIS asked the giver: "<person> can't do it. Shall I give it to someone else?"

answer:
- "time" (promise only): they say by when they will do it ("kal shaam tak", "by Friday", "2 ghante mein", "abhi karta hoon", "haan kal tak kar dunga"). Fill due_date (YYYY-MM-DD) and due_time (HH:MM, 24h; empty when no time of day was said), or in_minutes for "in N minutes / hours" ("abhi", "turant", "right now" = 0). Parts of the day: subah / morning 10:00, dopahar / afternoon 14:00, shaam / evening 18:00, raat / night 21:00; "aaj tak" / "end of day" = today 19:00. A time already past today with no day said means tomorrow.
- "cannot" (promise only): they can't or won't be able to do it ("nahi ho payega", "can't do it", "mere se nahi hoga", "time nahi hai"). reason = why, short, as they said it; empty when they gave no reason.
- "give" (unseen and reassign only): they name someone to give it to ("Sunil ko de do", "give it to Kiran", "haan accountant ko"). person = the name or job as said.
- "yes": a plain yes with nothing more ("haan", "haan bata do", "ok", "theek hai", "yes, tell him", "ji"). For unseen: ring again / try again / "haan". For promise: a yes with no time. For reassign: a yes with no name.
- "no": a plain no ("nahi", "rehne do", "leave it", "mat batao", "chhodo").
- "other": not an answer to the question at all (a new instruction, a question, something unrelated).
Fill every field; use "" (or -1 for in_minutes) for what does not apply."""


# ------------------------------------------------------------------ reading the words
def _empty(answer: str = "other") -> dict:
    return {"answer": answer, "due_date": "", "due_time": "", "in_minutes": -1, "reason": "", "person": ""}


def _read_ai(text: str, kind: str, question: str, title: str, user=None) -> dict | None:
    if not ai_brain.ai_available():
        return None
    now = tu.now_local()
    content = (f"<now>{now:%A %d %B %Y, %H:%M} (India)</now>\n<question>{kind}: {question}</question>\n"
               f"<task>{title}</task>\n<answer>{text[:500]}</answer>")
    try:
        response = ai_brain._get_client().messages.create(
            model=settings.JARVIS_AI_COMMAND_MODEL,
            max_tokens=400,
            system=READ_PROMPT,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": READ_SCHEMA}},
        )
        ai_usage.record(user, AiUsage.Kind.COMMAND, response)
        if response.stop_reason in ("refusal", "max_tokens"):
            log.warning("answer reader stopped: %s", response.stop_reason)
            return None
        data = json.loads(ai_brain._first_text(response))
    except anthropic.APIStatusError as exc:
        log.warning("AI API error %s reading an answer: %s", exc.status_code, str(getattr(exc, "message", ""))[:300])
        return None
    except (anthropic.APIConnectionError, json.JSONDecodeError, StopIteration) as exc:
        log.warning("answer reader unavailable: %s", type(exc).__name__)
        return None
    out = _empty()
    out.update({k: data.get(k, out[k]) for k in out})
    if out["answer"] not in ANSWERS:
        out["answer"] = "other"
    return out


def _norm(text: str) -> str:
    t = " ".join(str(text or "").lower().replace("’", "'").split())
    return re.sub(r"\b(nahin|nhi|nai)\b", "nahi", t)


_CANNOT = (
    "nahi ho paye", "nahi ho payega", "nahi ho paega", "nahi ho paayega", "nahi ho payegi", "nahi ho sakta",
    "nahi ho sakti", "nahi ho sakega", "nahi ho payenge", "nahi hoga", "nahi hogi", "nahi kar paunga",
    "nahi kar paungi", "nahi kar paaunga", "nahi kar paaungi", "nahi kar sakta", "nahi kar sakti", "nahi kar sakte",
    "nahi kar payenge", "nahi kar paenge", "nahi kar paayenge", "nahi kar pa", "can't", "cant do", "cannot", "can not",
    "won't be able", "wont be able", "unable", "not possible", "possible nahi", "mushkil", "nahi ho pa",
    "नहीं हो पा", "नहीं होगा", "नहीं हो सकता", "नहीं कर पा", "नहीं कर सकत", "मुश्किल",
)
_BECAUSE = re.compile(r"\b(?:kyunki|kyuki|kyonki|kyon ki|kyu ki|kyuki|because|since|coz|cause|reason|wajah)\b|क्योंकि|वजह",
                      re.IGNORECASE)
_IN_MINUTES = re.compile(r"\b(\d{1,3}|ek|do|teen|char|paanch|aadha|aadhe|half an?|an?)\s*"
                         r"(minute|minutes|min|mins|ghante|ghanta|ghnte|hour|hours|hr|hrs)\b", re.IGNORECASE)
_NUMBER_WORDS = {"ek": 1, "a": 1, "an": 1, "do": 2, "teen": 3, "char": 4, "paanch": 5}
_RIGHT_NOW = re.compile(r"\b(abhi|turant|right now|immediately|right away|asap)\b|अभी|तुरंत", re.IGNORECASE)
_QUESTION = re.compile(r"\?|\b(what|where|who|how|why|which|kya|kaun|kaunsa|konsa|kitna|kitne|kitni|kahan|kaise|"
                       r"weather|mausam)\b", re.IGNORECASE)
_RING = re.compile(r"\b(ring|phir se|fir se|dobara|again|try|call)\b|फिर से|दोबारा|रिंग", re.IGNORECASE)
_NOT_A_NAME = {
    "haan", "han", "ha", "ji", "yes", "ok", "okay", "theek", "thik", "hai", "no", "nahi", "na", "ko", "ke", "ki", "de",
    "do", "dedo", "dena", "dijiye", "give", "it", "to", "him", "her", "them", "please", "assign", "karo", "kar", "kardo",
    "someone", "else", "kisi", "aur", "ring", "again", "phir", "se", "the", "a", "an", "bhej", "bhejo", "send", "this",
    "that", "ye", "yeh", "wo", "woh", "usko", "isko", "unko", "inko", "ab", "abhi", "sir", "madam", "maam",
}


def _minutes_in(t: str) -> int:
    m = _IN_MINUTES.search(t)
    if m:
        raw, unit = m.group(1).lower(), m.group(2).lower()
        if raw in ("aadha", "aadhe") or raw.startswith("half"):
            n = 0.5
        else:
            n = int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw, 1)
        return int(n * (60 if unit.startswith(("g", "h")) else 1))
    return 15 if _RIGHT_NOW.search(t) else -1


_GIVE_TO = re.compile(r"\b([a-z]+(?:\s+[a-z]+)?)\s+ko\s+(?:de|dedo|dijiye|dena|bhej|bhejo|assign|give)\b"
                      r"|\b(?:give|assign|send)\s+(?:it\s+)?to\s+([a-z]+(?:\s+[a-z]+)?)", re.IGNORECASE)
_YES_START = {"haan", "han", "ha", "haanji", "yes", "yeah", "ok", "okay", "ji", "theek", "thik", "bilkul", "sure", "हाँ", "हां", "जी"}
_NO_START = {"nahi", "no", "nope", "mat", "rehne", "chhodo", "chodo", "na", "leave", "नहीं", "मत", "रहने"}


def _person_in(words: list[str], user=None) -> str:
    """The words that name exactly one person on the team ("Sunil", "accountant"), or ""."""
    useful = [w for w in words if w not in _NOT_A_NAME and w not in _NO_START]
    candidates = [f"{a} {b}" for a, b in zip(useful, useful[1:])] + useful
    for c in candidates:
        found = services.find_people(c, exclude=user)
        if found["status"] in ("known", "close") and len(found["people"]) == 1:
            return c
    return ""


def _read_rules(text: str, kind: str, user=None) -> dict:
    t = _norm(text)
    out = _empty()
    if not t:
        return out
    words = re.findall(r"[^\W\d_]+", t)
    if kind == "promise":
        if any(w in t for w in _CANNOT):
            out["answer"] = "cannot"
            parts = _BECAUSE.split(text, maxsplit=1)
            out["reason"] = parts[1].strip(" ,.।") if len(parts) > 1 else ""
            return out
        if _QUESTION.search(t):
            return out  # a question of their own ("what is the weather today"), not a time
        minutes = _minutes_in(t)
        if minutes >= 0:
            out.update(answer="time", in_minutes=minutes)
            return out
        parsed = hinglish_parser.parse(text)
        if parsed.get("due_date"):
            out.update(answer="time", due_date=parsed["due_date"], due_time=parsed.get("due_time") or "")
            if re.search(r"\baaj tak\b|\bend of (the )?day\b|आज तक", t) and not out["due_time"]:
                out["due_time"] = "19:00"
            return out
    else:
        person = _person_in(words, user)
        if person:
            out.update(answer="give", person=person)
            return out
        m = _GIVE_TO.search(t)
        if m:  # a name JARVIS does not know: still "give", so it can say so and ask who
            out.update(answer="give", person=(m.group(1) or m.group(2) or "").strip())
            return out
        if kind == "unseen" and _RING.search(t):
            out["answer"] = "yes"
            return out
    yn = hinglish_parser.yes_no(text)
    if yn:
        out["answer"] = yn
    elif len(words) <= 4 and words and words[0] in _YES_START:
        out["answer"] = "yes"
    elif len(words) <= 4 and words and words[0] in _NO_START:
        out["answer"] = "no"
    return out


def read_answer(text: str, kind: str, question: str = "", title: str = "", user=None) -> dict:
    """{"answer": time | cannot | yes | no | give | other, "due_date", "due_time", "in_minutes", "reason", "person"}."""
    read = _read_ai(text, kind, question, title, user)
    if read is None:
        return _read_rules(text, kind, user)
    if read["answer"] == "other":
        # The model found no answer, yet the words are a plain yes or no ("haan bata do"): that is the answer.
        # (Nothing else is taken from the rules here: a question about "today" must not become a promise.)
        rules = _read_rules(text, kind, user)
        if rules["answer"] in ("yes", "no"):
            return rules
    return read


def _when(read: dict) -> tuple[date, datetime | None] | None:
    """The moment promised: (day, exact time or None)."""
    now = tu.now_local()
    try:
        minutes = int(read.get("in_minutes") if read.get("in_minutes") is not None else -1)
    except (TypeError, ValueError):
        minutes = -1
    if minutes >= 0:
        at = now + timedelta(minutes=max(minutes, 15))  # "abhi" = within a quarter of an hour
        at = at.replace(second=0, microsecond=0) + timedelta(minutes=(5 - at.minute % 5) % 5)  # a time people say
        return at.date(), at
    try:
        day = date.fromisoformat(read["due_date"]) if read.get("due_date") else None
        at_time = time.fromisoformat(read["due_time"]) if read.get("due_time") else None
    except ValueError:
        return None
    if day is None and at_time is None:
        return None
    if day is None:
        day = now.date()
    at = tu.combine_local(day, at_time) if at_time else None
    if at is not None and at < now - timedelta(minutes=1) and day == now.date():
        day, at = day + timedelta(days=1), at + timedelta(days=1)  # "6 baje tak" said at 7 PM: tomorrow
    if day < now.date():
        return None
    return day, at


# ------------------------------------------------------------------ doing what the answer says
def _ack(alert: Alert) -> None:
    if alert.acknowledged_at is None:
        alert.acknowledged_at = timezone.now()
        Alert.objects.filter(id=alert.id).update(acknowledged_at=alert.acknowledged_at)


def _reply(user: User, said_in: str, en: str, hi: str, ask: dict | None = None) -> dict:
    hindi = said_in == "hi" and bool(hi)
    return {"handled": True, "show": en, "say": hi if hindi else en, "lang": "hi" if hindi else "en", "ask": ask}


def _task(task_id) -> SharedTask | None:
    try:
        return SharedTask.objects.select_related("assignee_user", "created_by").filter(id=task_id).first() if task_id else None
    except (ValueError, ValidationError):
        return None


def _give(by: User, task: SharedTask, words: str, kind: str, said_in: str) -> dict:
    address, address_hi = services.address_of(by), services.address_hi_of(by)
    keep = {"kind": kind, "en": "Who should get it?", "hi": "किसे दूँ?", "task": str(task.id)}
    found = services.find_people(words, exclude=by) if words.strip() else {"status": "unknown", "people": [], "options": []}
    if found["status"] == "ambiguous":
        options = " or ".join(found["options"])
        return _reply(by, said_in, f"Which {words}: {options}?", f"कौन से {words}: {options.replace(' or ', ' या ')}?",
                      ask={**keep, "en": f"Which {words}: {options}?", "hi": f"कौन से {words}?"})
    if found["status"] not in ("known", "close"):
        hint = f" Did you mean {found['options'][0]}?" if found.get("options") else ""
        return _reply(by, said_in, f"I don't know anyone called {words} on the team.{hint} Who should get it?",
                      f"टीम में {words} नाम का कोई नहीं है। किसे दूँ?", ask=keep)
    person = found["people"][0]
    old = task.assignee_user
    try:
        services.reassign_task(by, task.id, person)
    except ServiceError as err:
        return _reply(by, said_in, f"Sorry {address}, {err.message}", f"माफ़ कीजिए {address_hi}, {err.message}")
    told = f" I've told {old.spoken_name}." if old is not None else ""
    told_hi = f" {old.spoken_name} को बता दिया।" if old is not None else ""
    return _reply(by, said_in, f"Done, {address}: given to {person.spoken_name}.{told}",
                  f"हो गया, {address_hi}: {person.spoken_name} को दे दिया।{told_hi}")


def _promise(user: User, alert: Alert, read: dict, said_in: str) -> dict:
    address, address_hi = services.address_of(user), services.address_hi_of(user)
    q = alert.question if isinstance(alert.question, dict) else {}
    task = services.task_of(alert) or _task(q.get("task"))  # a chasing nudge names its task in the question
    answer = read["answer"]
    if answer == "other":
        services.mark_seen(user, alert)  # they heard it and went on to something else: it was seen
        return {"handled": False}
    if task is None or task.is_closed or task.assignee_user_id != user.id:
        services.mark_seen(user, alert)
        _ack(alert)
        return _reply(user, said_in, f"Okay {address}.", f"ठीक है {address_hi}।")
    giver, giver_hi = services.giver_words(task.created_by)
    if answer == "time":
        when = _when(read)
        if when is not None:
            try:
                task = services.record_promise(user, task.id, *when)
            except ServiceError as err:
                return _reply(user, said_in, f"Sorry {address}, {err.message}", f"माफ़ कीजिए {address_hi}, {err.message}")
            _ack(alert)
            label = tu.due_phrase(task.promise_kind, task.promise_date, task.promise_at)
            label_hi = tu.due_phrase_hi(task.promise_kind, task.promise_date, task.promise_at)
            return _reply(user, said_in, f"Okay {address}, I've told {giver}: by {label}.",
                          f"ठीक है {address_hi}, {giver_hi} को बता दिया: {label_hi} तक।")
        answer = "yes"  # a time JARVIS could not place: ask again
    if answer == "cannot":
        try:
            services.record_cannot(user, task.id, read.get("reason") or "")
        except ServiceError as err:
            return _reply(user, said_in, f"Sorry {address}, {err.message}", f"माफ़ कीजिए {address_hi}, {err.message}")
        _ack(alert)
        return _reply(user, said_in, f"Okay {address}, I've told {giver} you can't do it.",
                      f"ठीक है {address_hi}, {giver_hi} को बता दिया कि आप ये नहीं कर पाएँगे।")
    services.mark_seen(user, alert)
    if answer == "yes":
        return _reply(user, said_in, "By when?", "कब तक?", ask={"kind": "promise", "en": "By when?", "hi": "कब तक?"})
    _ack(alert)  # "no" (or a name, which means nothing here)
    return _reply(user, said_in, f"Okay {address}.", f"ठीक है {address_hi}।")


def _unseen(user: User, alert: Alert, read: dict, said_in: str) -> dict:
    address, address_hi = services.address_of(user), services.address_hi_of(user)
    q = alert.question if isinstance(alert.question, dict) else {}
    answer = read["answer"]
    if answer == "other":
        return {"handled": False}
    try:
        original = Alert.objects.select_related("recipient").filter(id=q.get("alert")).first() if q.get("alert") else None
    except (ValueError, ValidationError):
        original = None
    task = _task(q.get("task"))
    name = original.recipient.spoken_name if original else "them"
    if answer == "give" and task is not None:
        out = _give(user, task, read.get("person") or "", "unseen", said_in)
        if not out.get("ask"):
            _ack(alert)
        return out
    _ack(alert)
    if answer in ("yes", "time") and original is not None:
        result = services.ring_again(user, original)
        lines = {
            "rang": (f"Okay {address}, ringing {name} again.", f"ठीक है {address_hi}, {name} को फिर से रिंग कर रहा हूँ।"),
            "seen": (f"{address}, {name} has seen it now.", f"{address_hi}, {name} ने अब देख लिया है।"),
            "closed": (f"{address}, that is no longer open.", f"{address_hi}, वो अब खुला नहीं है।"),
            "off": (f"{address}, {name} has switched ringing off. It is in their list.",
                    f"{address_hi}, {name} ने रिंग बंद रखी है। उनकी लिस्ट में है।"),
        }[result]
        return _reply(user, said_in, *lines)
    return _reply(user, said_in, f"Okay {address}, leaving it.", f"ठीक है {address_hi}, रहने देता हूँ।")


def _reassign(user: User, alert: Alert, read: dict, said_in: str) -> dict:
    address, address_hi = services.address_of(user), services.address_hi_of(user)
    q = alert.question if isinstance(alert.question, dict) else {}
    task = _task(q.get("task"))
    answer = read["answer"]
    if answer == "other":
        return {"handled": False}
    if task is None or task.is_closed:
        _ack(alert)
        return _reply(user, said_in, f"{address}, that is no longer open.", f"{address_hi}, वो अब खुला नहीं है।")
    if answer == "give":
        out = _give(user, task, read.get("person") or "", "reassign", said_in)
        if not out.get("ask"):
            _ack(alert)
        return out
    if answer in ("yes", "time"):
        return _reply(user, said_in, "Who should get it?", "किसे दूँ?",
                      ask={"kind": "reassign", "en": "Who should get it?", "hi": "किसे दूँ?", "task": str(task.id)})
    _ack(alert)
    name = task.assignee_user.spoken_name if task.assignee_user else "them"
    return _reply(user, said_in, f"Okay {address}, it stays with {name}.", f"ठीक है {address_hi}, {name} के पास ही रहेगा।")


def answer(user: User, alert: Alert, text: str) -> dict:
    """The words said right after JARVIS asked one of these questions. Returns {"handled": False} when they are about
    something else; otherwise {"handled": True, "show", "say", "lang", "ask"} where "ask" is a follow-up question still
    waiting ("By when?", "Who should get it?") or None."""
    if alert.recipient_id != user.id:
        return {"handled": False}
    q = services.alert_question(alert) or {}
    kind = q.get("kind")
    if kind not in FREE_KINDS or not (text or "").strip():
        return {"handled": False}
    task = services.task_of(alert) or _task((alert.question or {}).get("task") if isinstance(alert.question, dict) else None)
    read = read_answer(text, kind, q.get("en", ""), task.title if task else alert.title, user)
    said_in = lang_mod.lang_for(user, text)
    if kind == "promise":
        return _promise(user, alert, read, said_in)
    if kind == "unseen":
        return _unseen(user, alert, read, said_in)
    return _reassign(user, alert, read, said_in)
