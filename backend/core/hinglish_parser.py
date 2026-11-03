"""Deterministic Hindi/English/Hinglish command parser.

Used when the AI brain is unavailable (no internet, provider down, no key).
Returns the same shape as the AI's structured output so the rest of the
pipeline does not care which engine produced it.
"""
import re
from datetime import date, timedelta

from .timeutils import now_local

_FLAGS = re.IGNORECASE

_DAY_PATTERNS = [
    (re.compile(r"\b(day after tomorrow|parso|parson|parsoon)\b", _FLAGS), 2),
    (re.compile(r"\b(tomorrow|tmrw|tmr|kal|kl)\b", _FLAGS), 1),
    (re.compile(r"\b(today|tonight|aaj|aj)\b", _FLAGS), 0),
]
_NEXT_WEEK = re.compile(r"\b(next week|agle hafte|agle week)\b", _FLAGS)

_WEEKDAYS = {
    "monday": 0, "somvar": 0, "somwar": 0,
    "tuesday": 1, "mangalvar": 1, "mangalwar": 1,
    "wednesday": 2, "budhvar": 2, "budhwar": 2,
    "thursday": 3, "guruvar": 3, "guruwar": 3, "veervar": 3,
    "friday": 4, "shukravar": 4, "shukrawar": 4,
    "saturday": 5, "shanivar": 5, "shaniwar": 5,
    "sunday": 6, "ravivar": 6, "raviwar": 6, "itwar": 6,
}
_WEEKDAY_RE = re.compile(r"\b(" + "|".join(_WEEKDAYS) + r")\b", _FLAGS)

_PART_OF_DAY = [
    (re.compile(r"\b(subah|subha|savere|morning)\b", _FLAGS), "morning"),
    (re.compile(r"\b(dopahar|dopehar|afternoon|noon)\b", _FLAGS), "afternoon"),
    (re.compile(r"\b(shaam|sham|evening)\b", _FLAGS), "evening"),
    (re.compile(r"\b(raat|night|tonight)\b", _FLAGS), "night"),
]
_PART_DEFAULT_TIME = {"morning": "10:00", "afternoon": "14:00", "evening": "18:00", "night": "21:00"}

_FRACTION = re.compile(r"\b(saade|sade|sawa|sava|paune|pone)\s+(\d{1,2})\b", _FLAGS)
_SPECIAL_FRACTION = re.compile(r"\b(dedh|dhai|dhaai)\b", _FLAGS)
_CLOCK = re.compile(
    r"\b(\d{1,2})\s*[:.]\s*(\d{2})(?!\s*(?:lakh|lac|crore|cr\b|k\b|rs\b|rupees|%|\d))\s*(am|pm|a\.m\.|p\.m\.)?",
    _FLAGS,
)
_AMPM = re.compile(r"\b(\d{1,2})\s*(am|pm|a\.m\.|p\.m\.)", _FLAGS)
_BAJE = re.compile(r"\b(\d{1,2})\s*(baje|bje|baj|o'?clock)\b", _FLAGS)
_AT_HOUR = re.compile(r"\bat\s+(\d{1,2})\b", _FLAGS)

_PRIORITY = [
    (re.compile(r"\b(urgent|urgently|turant|asap|emergency|immediately)\b", _FLAGS), "URGENT"),
    (re.compile(r"\b(high priority|zaroori|zaruri|jaroori|jaruri|important|imp)\b", _FLAGS), "HIGH"),
    (re.compile(r"\b(low priority|kam zaroori|not urgent|jab time mile)\b", _FLAGS), "LOW"),
]
_PRIORITY_WORDS = re.compile(r"\b(priority|marked|mark it|mark as)\b", _FLAGS)

_KIND = [
    (re.compile(r"\b(meeting|mtg|mulakat|mulaqat|baithak)\b", _FLAGS), "MEETING"),
    (re.compile(r"\b(payment|pay|bill|emi|cheque|bhugtan|transfer|dues)\b", _FLAGS), "PAYMENT"),
    (re.compile(r"\b(call|phone|fone|ring)\b", _FLAGS), "CALL"),
    (re.compile(r"\b(follow ?up|followup)\b", _FLAGS), "FOLLOW_UP"),
]

_ASSIGN = [
    re.compile(r"\b([a-z]+(?:\s+[a-z]+)?)\s+ko\s+(bolo|bol do|kaho|keh do|kah do|assign karo|assign kar do|assign|de do|batao|bata do)\b", _FLAGS),
    re.compile(r"\bassign(?:\s+(?:it|this))?\s+to\s+([a-z]+(?:\s+[a-z]+)?)", _FLAGS),
    re.compile(r"\b(?:tell|ask)\s+([a-z]+(?:\s+[a-z]+)?)\s+to\b", _FLAGS),
]

_ALL_STAFF = {"all staff", "sab staff", "sabhi staff", "poori team", "puri team", "whole team", "sab log", "sabhi log"}

_ACCEPT = re.compile(r"\b(accept|approve|manzoor|manjoor|okay accept|haan accept)\b", _FLAGS)
_DECLINE = re.compile(r"\b(decline|reject|mana kar|nahi chahiye|deny)\b", _FLAGS)
_BRIEFING = re.compile(r"\b(briefing|brief me|summary|summarise|summarize)\b", _FLAGS)
_QUERY = re.compile(
    r"(what(?:'s| is) (?:my|on my)|what do i have|aaj kya|kya hai aaj|mera din|my day|schedule|agenda|kitne task|how many tasks|kya pending)",
    _FLAGS,
)
_COMPLETE = re.compile(r"\b(ho gaya|ho gya|hogaya|done|completed?|khatam|finish(?:ed)?|mark .* done)\b", _FLAGS)

_LEADING_FILLER = re.compile(
    r"^\s*(?:(?:hey |ok |okay )?jarvis[,\s]*|please\s+|plz\s+|mujhe\s+|remind me to\s+|remind me\s+|"
    r"set a reminder to\s+|set reminder\s+|add (?:a )?task to\s+|add (?:a )?task\s+|create (?:a )?task to\s+|"
    r"create (?:a )?task\s+|note\s+)+",
    _FLAGS,
)
_TRAILING_FILLER = re.compile(
    r"(?:\s+(?:yaad dilana|yaad dila dena|yaad dilao|remind me|please|plz|tak|by|at|on|ko|hai))+\s*[.,!?]*$",
    _FLAGS,
)


def _hour_for_part(hour: int, part: str | None) -> int:
    if part in {"afternoon", "evening", "night"} and hour < 12:
        return hour + 12
    if part == "morning" and hour == 12:
        return 0
    if part is None and not (8 <= hour <= 12) and hour < 8:
        # Business-hours heuristic: "3 baje" means 3 PM, "9 baje" means 9 AM.
        return hour + 12
    return hour


def _extract_time(text: str, part: str | None, spans: list) -> tuple[str, bool]:
    """Returns (HH:MM or '', explicit) and records consumed spans."""
    m = _FRACTION.search(text)
    if m:
        word, hour = m.group(1).lower(), int(m.group(2))
        minute = 30 if word in {"saade", "sade"} else 15 if word in {"sawa", "sava"} else 45
        if word in {"paune", "pone"}:
            hour -= 1
        hour = _hour_for_part(hour % 12 or 12, part) if hour else 0
        tail = _BAJE.search(text, m.end() - len(m.group(2)))
        spans.append(m.span())
        if tail and tail.start() <= m.end():
            spans.append(tail.span())
        return f"{hour % 24:02d}:{minute:02d}", True
    m = _SPECIAL_FRACTION.search(text)
    if m:
        hour = 1 if m.group(1).lower() == "dedh" else 2
        spans.append(m.span())
        b = re.search(r"\b(baje|bje)\b", text[m.end():m.end() + 8], _FLAGS)
        if b:
            spans.append((m.end() + b.start(), m.end() + b.end()))
        return f"{_hour_for_part(hour, part or 'afternoon'):02d}:30", True
    for regex in (_CLOCK, _AMPM, _BAJE, _AT_HOUR):
        m = regex.search(text)
        if not m:
            continue
        hour = int(m.group(1))
        if hour > 23:
            continue
        minute = int(m.group(2)) if regex is _CLOCK else 0
        suffix = None
        if regex is _CLOCK and m.group(3):
            suffix = m.group(3)
        elif regex is _AMPM:
            suffix = m.group(2)
        if suffix:
            pm = suffix.lower().startswith("p")
            hour = (hour % 12) + (12 if pm else 0)
        elif hour <= 12:
            hour = _hour_for_part(hour, part)
        spans.append(m.span())
        return f"{hour:02d}:{minute:02d}", True
    if part:
        return _PART_DEFAULT_TIME[part], False
    return "", False


def _remove_spans(text: str, spans: list) -> str:
    out, last = [], 0
    for start, end in sorted(set(spans)):
        if start < last:
            continue
        out.append(text[last:start])
        last = end
    out.append(text[last:])
    return " ".join("".join(out).split())


def parse(text: str, staff_names: list[str] | None = None, has_pending_requests: bool = False) -> dict:
    raw = text.strip()
    now = now_local()
    today = now.date()
    spans: list[tuple[int, int]] = []
    result = {
        "intent": "create_task",
        "title": "",
        "notes": "",
        "kind": "TASK",
        "priority": "NORMAL",
        "due_date": "",
        "due_time": "",
        "duration_minutes": 0,
        "remind_before_minutes": 0,
        "staff_name": "",
        "category_key": "",
        "target_ref": "",
        "needs_clarification": False,
        "clarification_question": "",
        "spoken_reply": "",
        "reminder_line": "",
        "confidence": 0.5,
    }

    if _BRIEFING.search(raw):
        result["intent"] = "briefing"
        return result
    if _QUERY.search(raw):
        result["intent"] = "query"
        return result
    if has_pending_requests and _ACCEPT.search(raw) and len(raw.split()) <= 8:
        result["intent"] = "accept_request"
        return result
    if has_pending_requests and _DECLINE.search(raw) and len(raw.split()) <= 8:
        result["intent"] = "decline_request"
        return result

    # Date
    due: date | None = None
    for regex, offset in _DAY_PATTERNS:
        m = regex.search(raw)
        if m:
            due = today + timedelta(days=offset)
            spans.append(m.span())
            break
    if due is None:
        m = _WEEKDAY_RE.search(raw)
        if m:
            target = _WEEKDAYS[m.group(1).lower()]
            ahead = (target - today.weekday()) % 7 or 7
            due = today + timedelta(days=ahead)
            spans.append(m.span())
    if due is None:
        m = _NEXT_WEEK.search(raw)
        if m:
            due = today + timedelta(days=7)
            spans.append(m.span())

    # Part of day + time
    part = None
    for regex, name in _PART_OF_DAY:
        m = regex.search(raw)
        if m:
            part = name
            spans.append(m.span())
            break
    hhmm, _explicit = _extract_time(raw, part, spans)
    if hhmm and due is None:
        # A time without a day means today. If it has already passed, the server asks instead of
        # silently moving it to tomorrow (TaskPanel guide §3 step 6).
        due = today

    # Priority
    for regex, value in _PRIORITY:
        m = regex.search(raw)
        if m:
            result["priority"] = value
            spans.append(m.span())
            break
    for m in _PRIORITY_WORDS.finditer(raw):
        spans.append(m.span())

    # Kind
    for regex, value in _KIND:
        if regex.search(raw):
            result["kind"] = value
            break

    # Assignment to staff
    staff_lower = [s.lower() for s in (staff_names or [])]
    for regex in _ASSIGN:
        m = regex.search(raw)
        if not m:
            continue
        candidate = m.group(1).strip()
        words = candidate.split()
        if candidate.lower() in _ALL_STAFF or words[-1].lower() in {"sab", "sabko", "sabhi", "everyone", "all", "team"}:
            result["staff_name"] = candidate
            result["intent"] = "assign_to_staff"
            spans.append(m.span())
            break
        # Prefer a known staff first name / full name inside the captured phrase.
        chosen = ""
        for i in range(len(words)):
            phrase = " ".join(words[i:]).lower()
            if any(s == phrase or s.split()[0] == phrase for s in staff_lower):
                chosen = " ".join(words[i:])
                break
        if not chosen and staff_lower:
            last = words[-1].lower()
            if any(s.split()[0] == last for s in staff_lower):
                chosen = words[-1]
        # "X ko bolo" always means delegation. An unknown or partial name still goes to the server,
        # which asks "Did you mean …?" instead of silently saving a personal task.
        result["staff_name"] = chosen or words[-1]
        result["intent"] = "assign_to_staff"
        spans.append(m.span())
        break

    if result["intent"] == "create_task" and _COMPLETE.search(raw) and not due and not hhmm:
        result["intent"] = "complete_task"

    title = _remove_spans(raw, spans)
    title = _LEADING_FILLER.sub("", title)
    title = _TRAILING_FILLER.sub("", title)
    title = re.sub(r"\s*[,;]\s*$", "", title).strip(" ,.-")
    if not title:
        title = raw
    result["title"] = title[:1].upper() + title[1:200]
    if due:
        result["due_date"] = due.isoformat()
    result["due_time"] = hhmm
    if result["kind"] == "MEETING":
        result["duration_minutes"] = 60
    return result
