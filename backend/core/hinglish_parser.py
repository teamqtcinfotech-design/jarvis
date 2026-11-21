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
# A calendar date (Nov 2026): "12 December", "12 tarikh December ko", "December 12", "12 Dec 2027", "12/12", "25 tarikh".
_MONTHS = {
    "january": 1, "jan": 1, "janvari": 1, "february": 2, "feb": 2, "farvari": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "aprail": 4,
    "may": 5, "june": 6, "jun": 6, "joon": 6, "july": 7, "jul": 7, "julai": 7, "august": 8, "aug": 8, "agast": 8,
    "september": 9, "sept": 9, "sep": 9, "sitambar": 9, "october": 10, "oct": 10, "aktubar": 10, "november": 11, "nov": 11, "navambar": 11,
    "december": 12, "dec": 12, "disambar": 12,
}
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_DM = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:tarikh|tareekh|tarik)?\s*(?:ko\s+)?({_MONTH_RE})\b(?:\s*,?\s*(\d{{4}}))?", _FLAGS)
# "December 12" but never "may 4 baje" / "mar 5 pm" (a month word before a clock time is not a date).
_DATE_MD = re.compile(rf"\b({_MONTH_RE})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?!\s*(?:baje|bje|baj|am|pm|a\.m|p\.m|o'?clock|:|\.\d))(?:\s*,?\s*(\d{{4}}))?", _FLAGS)
_DATE_NUM = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}))?\b")
_TARIKH = re.compile(r"\b(\d{1,2})\s*(?:tarikh|tareekh|tarik)\b", _FLAGS)
_NEXT_MONTH = re.compile(r"\b(next month|agle mahine|agle month|agle maheene)\b", _FLAGS)


def _calendar_date(raw: str, today: date, spans: list) -> date | None:
    """The date the words name outright, or None. A day and month without a year is the coming one (this year, or
    next year once it has passed); "25 tarikh" alone is this month's 25th, or next month's once it has passed."""
    def make(d: int, mo: int, year: int | None) -> date | None:
        try:
            if year:
                return date(year, mo, d)
            cand = date(today.year, mo, d)
            return cand if cand >= today else date(today.year + 1, mo, d)
        except ValueError:
            return None

    m = _DATE_DM.search(raw)
    if m and m.group(2).lower() in _MONTHS:
        got = make(int(m.group(1)), _MONTHS[m.group(2).lower()], int(m.group(3)) if m.group(3) else None)
        if got:
            spans.append(m.span())
            return got
    m = _DATE_MD.search(raw)
    if m and m.group(1).lower() in _MONTHS:
        got = make(int(m.group(2)), _MONTHS[m.group(1).lower()], int(m.group(3)) if m.group(3) else None)
        if got:
            spans.append(m.span())
            return got
    m = _DATE_NUM.search(raw)
    if m and 1 <= int(m.group(2)) <= 12:
        got = make(int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None)
        if got:
            spans.append(m.span())
            return got
    m = _TARIKH.search(raw)
    if m:
        day = int(m.group(1))
        months_ahead = 1 if _NEXT_MONTH.search(raw) else 0
        for _ in range(3):
            mo, yr = (today.month - 1 + months_ahead) % 12 + 1, today.year + (today.month - 1 + months_ahead) // 12
            try:
                cand = date(yr, mo, day)
            except ValueError:
                months_ahead += 1
                continue
            if cand >= today or _NEXT_MONTH.search(raw):
                spans.append(m.span())
                return cand
            months_ahead += 1
    return None

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

# "saadhe gyarah" is the usual way to say 11:30 in Roman Hinglish (Nov 2026): every spelling of the half, quarter and
# three-quarter words, and the hour said as a word ("gyarah", "barah") before "baje" or after one of these.
_HALF_WORDS = {"saade", "sade", "saadhe", "sadhe", "saadha", "sadha", "sarhe", "saarhe"}
_QUARTER_WORDS = {"sawa", "sava", "savva", "sawwa"}
_THREE_QUARTER_WORDS = {"paune", "pone", "pauney", "pauna"}
_FRACTION_WORDS = "|".join(sorted(_HALF_WORDS | _QUARTER_WORDS | _THREE_QUARTER_WORDS, key=len, reverse=True))
_FRACTION = re.compile(rf"\b({_FRACTION_WORDS})\s+(\d{{1,2}})\b", _FLAGS)
_SPECIAL_FRACTION = re.compile(r"\b(dedh|dhai|dhaai)\b", _FLAGS)
_HOUR_WORDS = {"ek": 1, "do": 2, "teen": 3, "tin": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5, "chhe": 6, "che": 6, "cheh": 6,
               "chhah": 6, "saat": 7, "sat": 7, "aath": 8, "ath": 8, "nau": 9, "das": 10, "dus": 10, "gyarah": 11, "gyaarah": 11,
               "gyara": 11, "barah": 12, "baarah": 12, "bara": 12}
_HOUR_WORD_RE = "|".join(sorted(_HOUR_WORDS, key=len, reverse=True))
# Only an hour word that stands where an hour stands is a number ("kar do" is never "kar 2"): after a fraction word,
# or right before "baje".
_HOUR_AFTER_FRACTION = re.compile(rf"\b({_FRACTION_WORDS})\s+({_HOUR_WORD_RE})\b", _FLAGS)
_HOUR_BEFORE_BAJE = re.compile(rf"\b({_HOUR_WORD_RE})\s+(baje|bje|baj)\b", _FLAGS)


def _hours_as_digits(text: str) -> str:
    """"saadhe gyarah baje" -> "saadhe 11 baje", "paanch baje" -> "5 baje"."""
    text = _HOUR_AFTER_FRACTION.sub(lambda m: f"{m.group(1)} {_HOUR_WORDS[m.group(2).lower()]}", text)
    return _HOUR_BEFORE_BAJE.sub(lambda m: f"{_HOUR_WORDS[m.group(1).lower()]} {m.group(2)}", text)
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
        minute = 30 if word in _HALF_WORDS else 15 if word in _QUARTER_WORDS else 45
        if word in _THREE_QUARTER_WORDS:
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
    raw = _hours_as_digits(text.strip())
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

    # Date: a calendar date first ("12 December"), then the relative words, the weekday, "next week"
    due: date | None = _calendar_date(raw, today, spans)
    if due is None:
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
    result["day_named"] = due is not None  # the words themselves named a day (the live line keeps the model's date otherwise)
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


# Offline fallback only (the AI decides yes / no / change from meaning when it is reachable).
_YES = {"haan", "han", "ha", "haanji", "ji", "yes", "yeah", "yep", "ok", "okay", "theek", "thik", "hai", "sahi", "save",
        "karo", "kar", "do", "confirm", "bilkul", "done", "go", "ahead", "chalo", "bhej", "please", "sir", "sure",
        "हाँ", "हां", "जी", "ठीक", "है", "करो", "कर", "दो"}
_NO = {"nahi", "nahin", "nai", "na", "no", "cancel", "mat", "karo", "rehne", "do", "ruko", "stop", "chhodo", "chodo",
       "nope", "ji", "please", "kar", "नहीं", "मत", "रहने", "दो"}


def yes_no(text: str) -> str | None:
    """'yes' / 'no' for a short reply to "shall I save it?", else None (treat it as a change)."""
    words = [w for w in re.sub(r"[.,!?।]", " ", (text or "").lower()).split() if w]
    if not words or len(words) > 5:
        return None
    if all(w in _YES for w in words):
        return "yes"
    if all(w in _NO for w in words):
        return "no"
    return None
