"""Short professional messages JARVIS writes on behalf of one person to the other.

When Sir gives Mansi a task or sets a meeting, she hears a composed note in his name; when she sends
Sir a request, he hears a courteous note in hers. Always brief, always formal; a plain template is used
if the AI is unavailable or slow, so nothing ever waits on it for more than a few seconds.

Written by Grok's quick text model (Nov 2026): a note takes well under a second, and the live voice line's
"given to Mansi" no longer waits on a slow note.
"""
import json
import logging

from django.conf import settings

from . import ai_usage, xai
from .models import AiUsage

log = logging.getLogger("jarvis.compose")

SYSTEM_PROMPT = """You write the short message JARVIS speaks and shows to the other person when a task, meeting or request is passed on inside a company. You speak FOR the sender, as a capable executive assistant would.

Rules:
- One sentence, at most 20 words, the way a good assistant relays a message out loud. Formal, warm, plain; never casual; no emojis, no lists.
- Start with the recipient's name. Say what, and when if a date or time is given. Use only the facts given: never invent a day ("today"), a time, a reason or a place.
- Mention urgency only when the priority is URGENT or HIGH ("urgent", "high priority"). Never mention a normal or low priority.
- No filler: no "kindly requests", "at your earliest convenience", "please be informed", "as it is marked".
- The sender and the recipient can be anyone on the team (the MD, the PA, the manager, the accountant, a director ...). The MD speaks to others politely but briefly ("please"); everyone addresses the MD respectfully by the exact word given as md_address in the facts ("Sir, ..." / "Ma'am, ..."); colleagues speak to each other by name, courteously. In the examples below "Sir" stands for md_address and "Mansi" for a team member's real name (recipient / sender).
- Do not sign off, do not add greetings like "hope you are well", do not mention JARVIS.
- Write the message twice: "text" in the sender's own style in Roman letters (English, or Hinglish if the instruction was Hinglish; never Devanagari), and "text_hi" the same message in natural spoken Hindi in Devanagari (names stay as they are; numbers and times the way they are said).

Examples (text only; text_hi is the Devanagari twin of each):
- Sir gives Mansi a task "Book the Pune hotel" due 12 Nov 5 PM, normal: "Mansi, please book the Pune hotel by Thursday, 12 November, 5 PM."
- Mansi asks Sir to "Sign the vendor agreement", high: "Sir, Mansi requests your signature on the vendor agreement; it is high priority."
- Sir sets a meeting "Review the Pune deck" tomorrow 11 AM: "Mansi, Sir would like to review the Pune deck with you tomorrow at 11 AM.\""""

NOTE_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}, "text_hi": {"type": "string"}},
    "required": ["text", "text_hi"],
    "additionalProperties": False,
}
TIMEOUT_SECONDS = 4.0   # a note is a nicety: past this the plain template goes out instead
_DEVANAGARI = __import__("re").compile(r"[ऀ-ॿ]")


def compose(kind: str, sender: str, recipient: str, facts: dict, fallback: str, fallback_hi: str = "",
            user=None) -> tuple[str, str]:
    """kind: task_for_pa | meeting_with_pa | request_to_md | change_request.
    Returns (message, message in Devanagari), or the fallbacks when the AI is unavailable."""
    if not (settings.JARVIS_AI_ENABLED and xai.configured()):
        return fallback, fallback_hi
    from . import services  # local import: services imports compose inside its functions

    payload = {"kind": kind, "sender": sender, "recipient": recipient, "md_address": services.md_address(),
               **{k: v for k, v in facts.items() if v}}
    try:
        data = xai.responses(
            model=settings.XAI_COMMAND_MODEL, reasoning_effort="none", instructions=SYSTEM_PROMPT,
            input=f"<facts>\n{json.dumps(payload, ensure_ascii=False, default=str)}\n</facts>",
            json_schema=NOTE_SCHEMA, schema_name="note", max_output_tokens=300, timeout=TIMEOUT_SECONDS,
        )
        _count(user, data)
        note = json.loads(xai.output_text(data) or "{}")
        text = " ".join(str(note.get("text") or "").split())[:300]
        text_hi = " ".join(str(note.get("text_hi") or "").split())[:300]
        if not text:
            return fallback, fallback_hi
        return text, (text_hi if _DEVANAGARI.search(text_hi) else fallback_hi)
    except (xai.XaiError, ValueError, OSError) as exc:
        log.warning("compose failed (%s); template used", type(exc).__name__)
        return fallback, fallback_hi


def _count(user, data: dict) -> None:
    """One more quick model call on the day's counter (tokens from the response's usage block)."""
    if user is None or not getattr(user, "pk", None):
        return
    usage = data.get("usage") or {}

    class _Usage:
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)

    class _Response:
        usage = _Usage()

    ai_usage.record(user, AiUsage.Kind.COMMAND, _Response())
