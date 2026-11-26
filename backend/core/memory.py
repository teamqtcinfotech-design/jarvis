"""What JARVIS knows for good, and how it learns (26 Nov 2026).

Three things live here:

- The memory block: every lasting fact about this person (what they said to remember, and what JARVIS learned), in
  front of the brain on every turn (core/act.py, core/ask.py), grouped as people, preferences, habits, corrections.
- The night's reading: once a day the thread of the finished day (BrainThread) is read by the strong model for the
  facts worth keeping: who people are to the speaker, how they like things done, regular patterns, and every
  correction they made. The model gets the facts already known and answers with a diff (add / update / remove),
  applied only to what JARVIS learned itself: a note the speaker asked to keep is never changed here.
- Corrections, at once: "galat", "nahi, aise nahi", "mera matlab…" after a reply is read as a correction by the
  brain (the turn is marked), the rule it teaches goes into memory the same minute, and the exchange becomes a
  BankCase: `manage.py bank run` replays it against the real model later, so the mistake stays fixed.
"""
import json
import logging
import re
from datetime import date

import anthropic
from django.conf import settings

from . import ai_usage
from . import timeutils as tu
from .models import AiUsage, BankCase, BrainThread, Memory, User

log = logging.getLogger("jarvis.memory")

LEARNED_LIMIT = 150          # learned facts kept per person (the night's reading prunes what no longer holds)
NOTE_CHARS = 300
TRANSCRIPT_CHARS = 60_000    # the day's thread, in plain words, as the night's reading sees it
RESULT_CHARS = 240           # a tool result, cut, in that transcript

# The words that open a correction of the last reply or action. Short openings only: "nahi" alone in the middle of
# a long sentence is not a correction of JARVIS; "nahi, Sharma ji wali" is.
CORRECTION = re.compile(
    r"^\s*(?:"
    r"galat|ghalat|wrong|that'?s wrong|not that|no[,!.]? not|nahi[,!.]? (?:ye|yeh|wo|woh|us|is|aise|aisa|not|that)|nhi[,!.]? (?:ye|yeh|wo|woh|us|is|aise|aisa)"
    r"|aise nahi|aisa nahi|aise nhi|aisa nhi|ye nahi|yeh nahi|wo nahi|woh nahi|ye wala nahi|wo wala nahi"
    r"|mera matlab|matlab (?:ye|wo|yeh|woh)|i mean|i meant|not (?:this|that) one|no[,!.]? i (?:said|meant)"
    r"|गलत|ग़लत|ऐसे नहीं|ऐसा नहीं|ये नहीं|यह नहीं|वो नहीं|वह नहीं|मेरा मतलब|नहीं[,!.]? (?:ये|यह|वो|वह|उस|इस|ऐसे|ऐसा)"
    r")(?=[\s,.!?।]|$)",  # a whole word (Devanagari vowel signs are not "word" characters to \b)
    re.IGNORECASE,
)

_KIND_LABELS = {
    Memory.Kind.PERSON: "People",
    Memory.Kind.PREFERENCE: "How {address} likes things done",
    Memory.Kind.HABIT: "Habits and regular patterns",
    Memory.Kind.CORRECTION: "Corrections (mistakes made once; never again)",
    Memory.Kind.NOTE: "Notes {address} asked to keep",
}
_KIND_ORDER = (Memory.Kind.NOTE, Memory.Kind.PERSON, Memory.Kind.PREFERENCE, Memory.Kind.HABIT, Memory.Kind.CORRECTION)

LEARN_PROMPT = """You read one day of conversation between {reader} (addressed as "{address}") and JARVIS, their assistant, and keep JARVIS's lasting memory about this person up to date.

Worth keeping, as one short plain sentence each (English or Roman Hinglish, as the speaker talks; names and titles as they are):
- person: who someone is to the speaker ("Mehta ji is the Pune distributor", "Nikki handles GST and bank work").
- preference: how the speaker likes things done, said or shown ("Meetings are 30 minutes unless said", "Say the time, never the date, for today's items").
- habit: a regular pattern seen more than once or stated as a rule ("Mehta ji's meeting is every Friday 4 PM", "Calls the bank on Monday mornings").
- correction: a mistake JARVIS made and what was wanted instead, as a rule for next time ("'Sharma ji wali' means the GST meeting with Sharma ji, not the Sharma Traders call").

Not worth keeping: one-off items of the day (a single meeting, a task given once), numbers, dates and times that belong to one item, anything already in the facts below (unless the day changed it), guesses, phone numbers, bank details, passwords, health or family matters the speaker did not ask JARVIS to remember.

The facts JARVIS already holds are listed with a handle in [brackets]. Facts the speaker asked JARVIS to keep (marked said) are read-only: never update or remove them. For the learned ones: update a fact when the day refined it, remove one the day proved wrong, add what is new. Keep each fact under 200 characters. Add nothing when the day taught nothing: an empty diff is a fine answer.

Reply with ONLY a JSON object, no other text:
{{"add": [{{"kind": "person|preference|habit|correction", "text": "..."}}], "update": [{{"handle": "...", "text": "..."}}], "remove": ["handle", ...]}}"""


def enabled() -> bool:
    return bool(settings.ANTHROPIC_API_KEY and settings.JARVIS_AI_ENABLED)


# ------------------------------------------------------------------------------------------------- the block
def items(user: User) -> list[Memory]:
    """Everything JARVIS knows about this person, oldest first within a kind (the newest learned facts come last)."""
    return sorted(Memory.objects.filter(owner=user), key=lambda m: (_KIND_ORDER.index(m.kind) if m.kind in _KIND_ORDER else 9, m.created_at))


def block(user: User, address: str = "Sir") -> str:
    """The <memory> text for the brain's prompts: grouped by kind; "" when JARVIS knows nothing yet."""
    rows = items(user)
    if not rows:
        return ""
    groups: dict[str, list[str]] = {}
    for m in rows:
        label = _KIND_LABELS.get(m.kind, _KIND_LABELS[Memory.Kind.NOTE]).format(address=address)
        groups.setdefault(label, []).append("- " + " ".join(m.text.split())[:NOTE_CHARS])
    body = "\n".join(f"{label}:\n" + "\n".join(lines) for label, lines in groups.items())
    return f"<memory>\n{body}\n</memory>"


def note(user: User, text: str, kind: str = Memory.Kind.PREFERENCE, source: str = Memory.Source.LEARNED) -> Memory | None:
    """One learned fact, kept unless the same words are already there (or the learned shelf is full)."""
    text = " ".join((text or "").split())[:NOTE_CHARS]
    if not text:
        return None
    if kind not in Memory.Kind.values:
        kind = Memory.Kind.PREFERENCE
    low = text.lower()
    for m in Memory.objects.filter(owner=user):
        if " ".join(m.text.split()).lower() == low:
            return m
    if source == Memory.Source.LEARNED and Memory.objects.filter(owner=user, source=Memory.Source.LEARNED).count() >= LEARNED_LIMIT:
        # Full: the oldest learned fact of the least lasting kind makes room (corrections and people stay longest).
        victims = Memory.objects.filter(owner=user, source=Memory.Source.LEARNED).order_by("created_at")
        for shelf in (Memory.Kind.PREFERENCE, Memory.Kind.HABIT, Memory.Kind.PERSON, Memory.Kind.CORRECTION):
            old = victims.filter(kind=shelf).first()
            if old is not None:
                old.delete()
                break
    return Memory.objects.create(owner=user, text=text, kind=kind, source=source)


# ------------------------------------------------------------------------------------------------- corrections
def is_correction(text: str) -> bool:
    return bool(CORRECTION.search(text or ""))


def split_turns(messages: list) -> list[list]:
    """The thread's messages grouped by turn: each turn starts with the user message that carries <turn now=…>."""
    turns: list[list] = []
    for m in messages:
        content = m.get("content")
        starts = (m.get("role") == "user" and isinstance(content, list) and content
                  and content[0].get("type") == "text" and str(content[0].get("text") or "").lstrip().startswith("<turn "))
        if starts or not turns:
            turns.append([m])
        else:
            turns[-1].append(m)
    return turns


def expected_of(turn_messages: list) -> dict:
    """What a turn did, as the bank checks it: its tool calls (name and input) and the reply it ended with."""
    tools, say_en = [], ""
    for m in turn_messages:
        if m.get("role") != "assistant" or not isinstance(m.get("content"), list):
            continue
        for b in m["content"]:
            if b.get("type") == "tool_use":
                tools.append({"name": b.get("name"), "input": b.get("input") or {}})
            elif b.get("type") == "text":
                try:
                    data = json.loads(str(b.get("text") or ""))
                except ValueError:
                    data = None
                if isinstance(data, dict) and data.get("say_en"):
                    say_en = str(data["say_en"])
    return {"tools": tools, "say_en": say_en}


def record_correction(user: User, day: date, said: str, before: list, turn: list, learned: str = "") -> BankCase | None:
    """The corrected turn and the correction turn, kept as one bank case (nothing when the corrected turn is missing)."""
    if not before or not turn:
        return None
    first = turn[0]
    return BankCase.objects.create(
        user=user, day=day, said=" ".join(said.split())[:1500],
        before_json=json.dumps(before, ensure_ascii=False, default=str),
        turn_json=json.dumps(first, ensure_ascii=False, default=str),
        expected_json=json.dumps(expected_of(turn), ensure_ascii=False, default=str),
        learned=" ".join((learned or "").split())[:NOTE_CHARS],
    )


# ------------------------------------------------------------------------------------------------- the night
def transcript(messages: list) -> str:
    """The day's thread in plain words: what was said, what JARVIS looked up and did, and what it answered."""
    lines: list[str] = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        if m.get("role") == "user":
            for b in content:
                if b.get("type") == "text":
                    text = str(b.get("text") or "")
                    said = re.search(r"<said>(.*?)</said>", text, re.DOTALL)
                    if said:
                        flag = " (correcting JARVIS)" if "<correction>" in text else ""
                        lines.append(f"Speaker{flag}: " + " ".join(said.group(1).split())[:600])
                elif b.get("type") == "tool_result":
                    lines.append("  -> " + " ".join(str(b.get("content") or "").split())[:RESULT_CHARS])
        else:
            for b in content:
                if b.get("type") == "tool_use":
                    args = json.dumps(b.get("input") or {}, ensure_ascii=False)
                    lines.append(f"  JARVIS {b.get('name')}: {args[:300]}")
                elif b.get("type") == "text":
                    try:
                        data = json.loads(str(b.get("text") or ""))
                    except ValueError:
                        data = None
                    if isinstance(data, dict) and data.get("say_en"):
                        lines.append("JARVIS: " + " ".join(str(data["say_en"]).split())[:600])
    text = "\n".join(lines)
    return text[-TRANSCRIPT_CHARS:] if len(text) > TRANSCRIPT_CHARS else text


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=settings.JARVIS_AI_ANSWER_TIMEOUT_SECONDS, max_retries=1)


def _ask_model(user: User, prompt: str, body: str) -> dict | None:
    """The night's reading, from the strong model: the diff as a dict, or None when it could not be reached."""
    try:
        response = _client().messages.create(
            model=settings.JARVIS_AI_ACT_STRONG_MODEL, max_tokens=3000,
            system=[{"type": "text", "text": prompt}],
            messages=[{"role": "user", "content": body}],
            output_config={"effort": settings.JARVIS_AI_ACT_EFFORT},
        )
    except (anthropic.APIConnectionError, anthropic.APIStatusError, anthropic.RateLimitError) as err:
        log.warning("memory: the model could not be reached for the night's reading (%s)", type(err).__name__)
        return None
    ai_usage.record(user, AiUsage.Kind.COMMAND, response)
    text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text")
    found = re.search(r"\{.*\}", text, re.DOTALL)
    for candidate in (text.strip(), found.group(0) if found else ""):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    log.warning("memory: the night's reading did not come back as a diff")
    return None


def learn_day(row: BrainThread, address: str = "Sir", reader: str = "the speaker") -> dict:
    """One finished day read for lasting facts; the diff applied to what JARVIS learned. Marks the thread read
    (also when there was nothing in it); leaves it unread when the model could not be reached, to try again."""
    try:
        messages = json.loads(row.messages_json or "[]")
    except ValueError:
        messages = []
    text = transcript(messages if isinstance(messages, list) else [])
    out = {"added": 0, "updated": 0, "removed": 0}
    if not text.strip():
        row.learned = True
        row.save(update_fields=["learned"])
        return out
    known = items(row.user)
    handles = {f"m{i + 1}": m for i, m in enumerate(known)}
    facts = "\n".join(f"[{h}] ({m.kind}, {m.source}) {' '.join(m.text.split())}" for h, m in handles.items()) or "(nothing yet)"
    body = (f"<known_facts>\n{facts}\n</known_facts>\n"
            f"<day date=\"{row.day.isoformat()}\">\n{text}\n</day>")
    diff = _ask_model(row.user, LEARN_PROMPT.format(reader=reader, address=address), body)
    if diff is None:
        return out
    for h in diff.get("remove") or []:
        m = handles.get(str(h))
        if m is not None and m.source == Memory.Source.LEARNED:
            m.delete()
            out["removed"] += 1
    for u in diff.get("update") or []:
        if not isinstance(u, dict):
            continue
        m = handles.get(str(u.get("handle") or ""))
        new = " ".join(str(u.get("text") or "").split())[:NOTE_CHARS]
        if m is not None and m.source == Memory.Source.LEARNED and new and new != m.text:
            m.text = new
            m.save(update_fields=["text", "updated_at"])
            out["updated"] += 1
    for a in diff.get("add") or []:
        if not isinstance(a, dict):
            continue
        before = Memory.objects.filter(owner=row.user).count()
        made = note(row.user, str(a.get("text") or ""), kind=str(a.get("kind") or Memory.Kind.PREFERENCE))
        if made is not None and Memory.objects.filter(owner=row.user).count() > before:
            out["added"] += 1
    row.learned = True
    row.save(update_fields=["learned"])
    log.info("memory: %s read for %s: +%d ~%d -%d", row.day, row.user.username, out["added"], out["updated"], out["removed"])
    return out


def learn_pending(now=None) -> int:
    """Every finished day not yet read (the worker was down, or it is the first night): read now. Returns how many
    threads were read."""
    from . import services  # noqa: PLC0415 — services imports the models this module reads

    if not enabled():
        return 0
    today = tu.to_local(now).date() if now else tu.today_local()
    done = 0
    for row in BrainThread.objects.filter(learned=False, day__lt=today, turns__gt=0).select_related("user").order_by("day"):
        try:
            learn_day(row, address=services.address_of(row.user), reader=services.role_label(row.user))
            done += 1
        except Exception:  # noqa: BLE001 — one bad day must not stop the others
            log.exception("memory: the night's reading failed for %s on %s", row.user.username, row.day)
    return done
