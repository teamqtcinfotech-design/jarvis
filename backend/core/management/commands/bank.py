"""The bank of corrections (26 Nov 2026): every "galat" / "nahi, wo wali" the speaker said after a reply is kept
with the turn it corrected (core/memory.py, BankCase). This command replays them against the REAL model, without
touching any data: the model gets the corrected turn exactly as it saw it and the correction, and its first move
(the tools it reaches for, with their arguments) is compared with what the correction turn did when it was live.

    python manage.py bank                 # list the cases
    python manage.py bank run             # replay every active case (a few paise each)
    python manage.py bank run --only 12   # one case by id
    python manage.py bank export bank.json
    python manage.py bank retire 12       # keep it, but leave it out of runs

Run it before a change to the brain's prompt or tools goes out; a case that fails is a correction coming back.
"""
import json

import anthropic
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core import act, ai_brain, memory, services
from core.models import BankCase

KEY_ARGS = ("id", "ids", "date", "time", "title", "kind", "priority", "to", "what", "text", "minutes", "force")


def _parse_block(b) -> dict:
    return b.model_dump(mode="json", exclude_none=True) if hasattr(b, "model_dump") else dict(b)


def _norm(v) -> str:
    return " ".join(str(v if v is not None else "").split()).lower()


def _matches(want: dict, got: dict) -> bool:
    """A tool call the model made matches an expected one: same name, and every key argument the expected call
    had is there with the same value (words may be contained either way)."""
    if want.get("name") != got.get("name"):
        return False
    wi, gi = want.get("input") or {}, got.get("input") or {}
    for k in KEY_ARGS:
        if k not in wi:
            continue
        if k in ("title", "what", "text", "to"):
            a, b = _norm(wi[k]), _norm(gi.get(k))
            if not (a and b and (a in b or b in a)):
                return False
        elif k == "ids":
            if sorted(map(str, wi[k] or [])) != sorted(map(str, gi.get(k) or [])):
                return False
        elif _norm(wi[k]) != _norm(gi.get(k)):
            return False
    return True


def check_case(case: BankCase, model: str, client=None) -> dict:
    """Replay one case: {"ok": bool, "expected": [...], "got": [...], "said": "..."}."""
    before = json.loads(case.before_json or "[]")
    turn = json.loads(case.turn_json or "{}")
    expected = json.loads(case.expected_json or "{}")
    user = case.user
    address, address_hi = services.address_of(user), services.address_hi_of(user)
    system = [{"type": "text", "text": act.ACT_SYSTEM_PROMPT.format(reader=services.role_label(user), address=address, address_hi=address_hi)}]
    client = client or act._client()  # noqa: SLF001 — the brain's own client
    response = client.messages.create(model=model, max_tokens=2500, system=system, messages=before + [turn], tools=act.TOOLS,
                                      output_config={"effort": settings.JARVIS_AI_ACT_EFFORT})
    got_tools, said = [], ""
    for b in (_parse_block(x) for x in response.content):
        if b.get("type") == "tool_use":
            got_tools.append({"name": b.get("name"), "input": b.get("input") or {}})
        elif b.get("type") == "text":
            data = act._parse(str(b.get("text") or ""))  # noqa: SLF001
            said = str((data or {}).get("say_en") or b.get("text") or "")
    want_tools = expected.get("tools") or []
    if want_tools:
        left = list(got_tools)
        ok = True
        for w in want_tools:
            hit = next((g for g in left if _matches(w, g)), None)
            if hit is None:
                ok = False
                break
            left.remove(hit)
    else:
        ok = not got_tools and bool(said.strip())  # the correction needed no tool: a reply, and nothing touched
    return {"ok": ok, "expected": want_tools, "got": got_tools, "said": said, "expected_said": expected.get("say_en") or ""}


class Command(BaseCommand):
    help = "List, replay (against the real model), export or retire the bank of corrections."

    def add_arguments(self, parser):
        parser.add_argument("action", nargs="?", default="list", choices=["list", "run", "export", "retire"])
        parser.add_argument("target", nargs="?", default="", help="a file for export; a case id for retire")
        parser.add_argument("--only", default="", help="run: one case id")
        parser.add_argument("--model", default="", help="run: the model (default: the act path's strong model)")

    def handle(self, *args, **opts):
        action = opts["action"]
        if action == "list":
            rows = BankCase.objects.select_related("user").order_by("id")
            if not rows:
                self.stdout.write("The bank is empty: no correction has been recorded yet.")
                return
            for c in rows:
                exp = json.loads(c.expected_json or "{}")
                tools = ", ".join(t.get("name") or "?" for t in exp.get("tools") or []) or "(reply only)"
                flag = "" if c.active else "  [retired]"
                self.stdout.write(f"#{c.id}  {c.day}  {c.user.username}{flag}\n    said: {c.said[:120]}\n    did:  {tools}"
                                  + (f"\n    rule: {c.learned[:120]}" if c.learned else ""))
            return
        if action == "export":
            if not opts["target"]:
                raise CommandError("bank export <file.json>")
            rows = [{"id": c.id, "day": c.day.isoformat(), "user": c.user.username, "said": c.said, "before": json.loads(c.before_json or "[]"),
                     "turn": json.loads(c.turn_json or "{}"), "expected": json.loads(c.expected_json or "{}"), "learned": c.learned,
                     "active": c.active} for c in BankCase.objects.select_related("user").order_by("id")]
            with open(opts["target"], "w", encoding="utf-8") as fh:
                json.dump(rows, fh, ensure_ascii=False, indent=1)
            self.stdout.write(self.style.SUCCESS(f"{len(rows)} case(s) written to {opts['target']}"))
            return
        if action == "retire":
            try:
                c = BankCase.objects.get(id=int(opts["target"] or opts["only"] or 0))
            except (BankCase.DoesNotExist, ValueError):
                raise CommandError("bank retire <case id>") from None
            c.active = False
            c.save(update_fields=["active"])
            self.stdout.write(self.style.SUCCESS(f"#{c.id} retired"))
            return
        # run
        if not ai_brain.ai_available():
            self.stderr.write("AI is off (JARVIS_AI_ENABLED / ANTHROPIC_API_KEY): nothing to replay.")
            return
        model = opts["model"] or settings.JARVIS_AI_ACT_STRONG_MODEL
        rows = BankCase.objects.filter(active=True).select_related("user").order_by("id")
        if opts["only"]:
            rows = rows.filter(id=int(opts["only"]))
        passed = failed = 0
        for c in rows:
            try:
                out = check_case(c, model)
            except (anthropic.APIConnectionError, anthropic.APIStatusError) as err:
                self.stdout.write(self.style.ERROR(f"#{c.id}  the model could not be reached ({type(err).__name__})"))
                failed += 1
                continue
            if out["ok"]:
                passed += 1
                self.stdout.write(self.style.SUCCESS(f"ok   #{c.id}  {c.said[:90]}\n      -> {out['said'][:120]}"))
            else:
                failed += 1
                self.stdout.write(self.style.ERROR(
                    f"FAIL #{c.id}  {c.said[:90]}\n      want {json.dumps(out['expected'], ensure_ascii=False)[:300]}"
                    f"\n      got  {json.dumps(out['got'], ensure_ascii=False)[:300]}\n      said {out['said'][:160]}"))
        self.stdout.write(f"\n{passed} passed, {failed} failed")
        memory.log.info("bank run: %d passed, %d failed", passed, failed)
