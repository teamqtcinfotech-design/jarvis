"""Short professional messages JARVIS writes on behalf of one person to the other.

When Sir gives Mansi a task or sets a meeting, she hears a composed note in his name; when she sends
Sir a request, he hears a courteous note in hers. Always brief, always formal; a plain template is used
if the AI is unavailable, so nothing ever waits on it.
"""
import json
import logging

import anthropic
from django.conf import settings

from . import ai_brain, ai_usage
from .models import AiUsage

log = logging.getLogger("jarvis.compose")

SYSTEM_PROMPT = """You write the short message JARVIS speaks and shows to the other person when a task, meeting or request is passed on inside a company. You speak FOR the sender, as a capable executive assistant would.

Rules:
- One sentence, at most 20 words, the way a good assistant relays a message out loud. Formal, warm, plain; never casual; no emojis, no lists.
- Start with the recipient's name. Say what, and when if a date or time is given. Use only the facts given: never invent a day ("today"), a time, a reason or a place.
- Mention urgency only when the priority is URGENT or HIGH ("urgent", "high priority"). Never mention a normal or low priority.
- No filler: no "kindly requests", "at your earliest convenience", "please be informed", "as it is marked".
- Same language style as the sender's own words: English, or Hinglish in Roman letters. No Devanagari.
- The MD speaks to the assistant politely but briefly ("please"); the assistant addresses the MD respectfully by the exact word given as md_address in the facts ("Sir, ..." / "Ma'am, ..."). In the examples below "Sir" stands for md_address and "Mansi" for the assistant's real name (recipient / sender).
- Do not sign off, do not add greetings like "hope you are well", do not mention JARVIS.
- Output only the message text.

Examples:
- Sir gives Mansi a task "Book the Pune hotel" due 12 Nov 5 PM, normal: "Mansi, please book the Pune hotel by Thursday, 12 November, 5 PM."
- Mansi asks Sir to "Sign the vendor agreement", high: "Sir, Mansi requests your signature on the vendor agreement; it is high priority."
- Sir sets a meeting "Review the Pune deck" tomorrow 11 AM: "Mansi, Sir would like to review the Pune deck with you tomorrow at 11 AM.\""""


def compose(kind: str, sender: str, recipient: str, facts: dict, fallback: str, user=None) -> str:
    """kind: task_for_pa | meeting_with_pa | request_to_md | change_request. Returns the message (or the fallback)."""
    if not ai_brain.ai_available():
        return fallback
    from . import services  # local import: services imports compose inside its functions

    payload = {"kind": kind, "sender": sender, "recipient": recipient, "md_address": services.md_address(),
               **{k: v for k, v in facts.items() if v}}
    try:
        response = ai_brain._get_client().messages.create(  # noqa: SLF001 — same client, same timeouts
            model=settings.JARVIS_AI_COMMAND_MODEL,
            max_tokens=200,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": f"<facts>\n{json.dumps(payload, ensure_ascii=False, default=str)}\n</facts>"}],
            output_config={"effort": "low"},
        )
        ai_usage.record(user, AiUsage.Kind.COMMAND, response)
        if response.stop_reason == "refusal":
            return fallback
        text = " ".join(ai_brain._first_text(response).split())  # noqa: SLF001
        return text[:300] if text else fallback
    except (anthropic.APIError, ValueError) as exc:
        log.warning("compose failed (%s); template used", type(exc).__name__)
        return fallback
