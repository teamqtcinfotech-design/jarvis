"""Ask the real command model a bench of questions and check which template and filters it picks.

    python manage.py brain_bench            # every question, MD and PA
    python manage.py brain_bench --role pa  # Mansi's questions only
    python manage.py brain_bench --only meeting

This is the evidence for tuning the prompt: the server side is covered by the test suite (which fakes the
model), this shows what the model itself chooses for real sentences, English and Hinglish, including the
follow-ups that narrow a list ("sirf meetings"). It costs a few paise per question (Haiku). Nothing is saved.
"""
from django.core.management.base import BaseCommand

from core import ai_brain, lang, services
from core import timeutils as tu
from core.models import User

T = "{today}"
W = "{week_end}"
Y = "{yesterday}"
M = "{tomorrow}"

# (role, text, expected fields). Expected values are checked as given; a list is checked as a set.
# "after" = the list JARVIS answered with just before (goes to the model as last_list), to test follow-ups.
BENCH = [
    # --- today means today
    ("md", "aaj kya hai", {"intent": "query", "view_kind": "today"}),
    ("md", "what do I have today", {"intent": "query", "view_kind": "today"}),
    ("md", "am I free today?", {"intent": "query", "view_kind": ["today", "busy"]}),
    ("md", "aaj ka schedule batao", {"intent": "query", "view_kind": "today"}),
    ("pa", "do I have anything important to do today", {"intent": "query", "view_kind": ["today", "tasks", "pending"], "view_priority": "important"}),
    ("pa", "aaj mujhe kya karna hai", {"intent": "query", "view_kind": ["today", "tasks"]}),
    ("md", "kya kuch late hai", {"intent": "query", "view_kind": "late"}),
    ("md", "kya reh gaya hai", {"intent": "query", "view_kind": ["late", "pending"]}),
    # --- narrowing: type, from whom, priority, words
    ("md", "aaj ki meetings", {"intent": "query", "view_kind": ["today", "tasks"], "view_types": ["MEETING"]}),
    ("pa", "today's meeting that Sir gave me", {"intent": "query", "view_kind": ["today", "tasks"], "view_types": ["MEETING"], "view_from_whom": "sir"}),
    ("pa", "aaj Sir ne jo meeting di hai wo kab hai", {"intent": "query", "view_kind": ["today", "tasks"], "view_types": ["MEETING"], "view_from_whom": "sir"}),
    ("pa", "Sir ne aaj mujhe kya diya", {"intent": "query", "view_kind": ["today", "tasks"], "view_from_whom": "sir"}),
    ("pa", "mere apne tasks", {"intent": "query", "view_kind": ["tasks", "pending", "pa"], "view_from_whom": "me"}),
    ("md", "koi urgent kaam hai?", {"intent": "query", "view_priority": "urgent"}),
    ("md", "anything important this week", {"intent": "query", "view_kind": ["week", "tasks"], "view_priority": "important"}),
    ("md", "auditor wala call kab hai", {"intent": "query", "view_kind": "tasks", "view_types": ["CALL"], "view_words": "auditor"}),
    ("md", "Pune vendor ka task kab tak hai", {"intent": "query", "view_kind": "tasks", "view_words": "Pune vendor"}),
    ("md", "kal ki calls", {"intent": "query", "view_kind": ["tasks", "upcoming"], "view_types": ["CALL"], "view_from": M, "view_to": M}),
    ("md", "is hafte ke payments", {"intent": "query", "view_kind": ["week", "tasks"], "view_types": ["PAYMENT"], "view_from": T, "view_to": W}),
    ("md", "Mansi ne kya bheja hai", {"intent": "query", "view_kind": ["requests", "tasks", "pending"]}),
    ("md", "Mansi ke paas kya hai", {"intent": "query", "view_kind": ["pa", "tasks"]}),
    ("md", "staff ko kya diya hai", {"intent": "query", "view_kind": ["staff", "tasks"]}),
    # --- follow-ups that narrow or widen the last list
    ("pa", "sirf meetings", {"intent": "query", "view_types": ["MEETING"], "view_from": T, "view_to": T},
     {"kind": "today", "from": T, "to": T}),
    ("pa", "sirf Sir ke diye hue", {"intent": "query", "view_from_whom": "sir", "view_from": T, "view_to": T},
     {"kind": "today", "from": T, "to": T}),
    ("pa", "aur urgent wale?", {"intent": "query", "view_priority": "urgent", "view_types": ["MEETING"]},
     {"kind": "tasks", "from": T, "to": T, "types": ["MEETING"]}),
    ("pa", "sab dikhao", {"intent": "query", "view_types": [], "view_from_whom": "", "view_from": T, "view_to": T},
     {"kind": "tasks", "from": T, "to": T, "types": ["MEETING"], "from_whom": "sir"}),
    ("md", "only the late ones", {"intent": "query", "view_filter": "late"},
     {"kind": "pending"}),
    # --- the usual lists still work
    ("md", "Ramesh Kumar ke tasks dikhao", {"intent": "query", "view_kind": "tasks", "view_who": "Ramesh Kumar"}),
    ("md", "meri staff ki list", {"intent": "query", "view_kind": "people"}),
    ("md", "Neha ka performance", {"intent": "query", "view_kind": "performance", "view_who": ["Neha", "Neha Gupta"]}),
    ("md", "kal kya kya complete hua", {"intent": "query", "view_kind": ["tasks", "done"], "view_by": "done"}),
    ("md", "kitna pending hai", {"intent": "query", "view_kind": ["pending", "stats"]}),
    ("md", "mere personal tasks", {"intent": "query", "view_kind": ["private", "tasks"]}),
    ("md", "official kaam kya baaki hai", {"intent": "query", "view_kind": ["official", "pending", "tasks"]}),
    ("pa", "Sir kab busy hain aaj", {"intent": "query", "view_kind": ["busy", "today"]}),
    # --- not lists
    ("md", "kal 4 baje Sharma ji ke saath meeting", {"intent": "create_task", "kind": "MEETING"}),
    ("md", "{pa} ko bolo kal tak hotel book kare", {"intent": "assign_to_pa"}),
    ("pa", "Sir ke liye kal 11 baje bank wali meeting", {"intent": "request_for_md"}),
    ("md", "Indore mein aaj mausam kaisa hai", {"intent": "general_question"}),
    ("md", "bas thank you", {"intent": "end_conversation"}),
    # --- version 2: exact "in N minutes", snooze, questions, history, cancelled, names and habits by voice
    ("md", "5 minute baad yaad dilana files lene hain", {"intent": "create_task", "due_in_minutes": 5}),
    ("md", "remind me in 2 hours to call Mehta ji", {"intent": "create_task", "due_in_minutes": 120, "kind": "CALL"}),
    ("md", "kal 11 baje Sharma ji ke saath meeting", {"intent": "create_task", "kind": "MEETING", "duration_minutes": 0}),
    ("md", "aadhe ghante ki meeting kal 3 baje Mehta ji ke saath", {"intent": "create_task", "kind": "MEETING", "duration_minutes": 30}),
    ("md", "10 minute baad yaad dilana", {"intent": "snooze", "snooze_minutes": 10},
     {"awaiting": 'JARVIS asked: "Done, or remind you later?" about "Call the bank"'}),
    ("md", "baad mein", {"intent": "snooze"}, {"awaiting": 'JARVIS asked: "Done, or remind you later?" about "Call the bank"'}),
    ("md", "ho gaya", {"intent": "confirm_pending"}, {"awaiting": 'JARVIS asked: "Done, or remind you later?" about "Call the bank"'}),
    ("md", "haan accept karo", {"intent": "confirm_pending"}, {"awaiting": 'JARVIS asked: "Accept it?" about "Sign the vendor agreement"'}),
    ("md", "nahi rehne do", {"intent": "cancel_pending"}, {"awaiting": 'JARVIS asked: "Shall I move Pay the vendor to tomorrow?" about "Good morning, here is your day"'}),
    ("md", "Sharma ji se pichhli baar kab mila tha", {"intent": "query", "view_kind": "history", "view_words": "Sharma"}),
    ("md", "when did I last meet Sharma ji", {"intent": "query", "view_kind": "history", "view_words": "Sharma"}),
    ("md", "cancel kiye hue tasks dikhao", {"intent": "query", "view_kind": "cancelled"}),
    ("md", "meri PA ka naam Suraj hai", {"intent": "set_setting", "setting_key": "paName", "setting_value": "Suraj"}),
    ("md", "mujhe Ma'am bolo", {"intent": "set_setting", "setting_key": "addressAs"}),
    ("pa", "mujhe Mansi bulao", {"intent": "set_setting", "setting_key": "myName", "setting_value": "Mansi"}),
    ("md", "meri meetings aam taur par 30 minute ki hoti hain", {"intent": "set_setting", "setting_key": "meetingMinutes", "setting_value": "30"}),
]


def _fill(value, dates):
    if isinstance(value, str):
        return value.format(**dates)
    if isinstance(value, list):
        return [_fill(v, dates) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, dates) for k, v in value.items()}
    return value


def _ok(got, want) -> bool:
    if isinstance(want, list) and not isinstance(got, list):
        return got in want            # any of these kinds is right
    if isinstance(want, list):
        return set(got or []) == set(want)
    if isinstance(want, int) and not isinstance(want, bool):
        return int(got or 0) == want  # a wanted 0 ("no length said") is a real answer, not "empty"
    return (got or "") == want


class Command(BaseCommand):
    help = "Ask the command model the bench questions and report which template and filters it chose."

    def add_arguments(self, parser):
        parser.add_argument("--role", choices=["md", "pa", "all"], default="all")
        parser.add_argument("--only", default="", help="only questions containing this text")

    def handle(self, *args, **opts):
        if not ai_brain.ai_available():
            self.stderr.write("AI is off (JARVIS_AI_ENABLED / ANTHROPIC_API_KEY): nothing to bench.")
            return
        today = tu.today_local()
        from datetime import timedelta
        dates = {"today": today.isoformat(), "tomorrow": (today + timedelta(days=1)).isoformat(),
                 "yesterday": (today - timedelta(days=1)).isoformat(), "week_end": (today + timedelta(days=6)).isoformat()}
        users = {"md": User.objects.filter(role=User.Role.MD, is_active=True).first(),
                 "pa": User.objects.filter(role=User.Role.PA, is_active=True).first()}
        dates["pa"] = users["pa"].spoken_name if users["pa"] else "PA"  # the PA's real name in the sentences
        contexts = {}
        passed = failed = 0
        for entry in BENCH:
            role, text, want = entry[0], entry[1].format(**dates), _fill(entry[2], dates)
            after = _fill(entry[3], dates) if len(entry) > 3 else None
            if opts["role"] != "all" and role != opts["role"]:
                continue
            if opts["only"] and opts["only"].lower() not in text.lower():
                continue
            user = users.get(role)
            if user is None:
                self.stderr.write(f"no {role.upper()} user in this database; skipping: {text}")
                continue
            if role not in contexts:
                contexts[role] = services.ai_context(user)
            ctx = dict(contexts[role])
            ctx["conversation"] = []
            ctx["reply_language"] = lang.lang_for(user, text)  # as build_draft tells the model
            if after and "awaiting" in after:
                # JARVIS just asked a question (about a reminder, an alert, the briefing): the text is the answer.
                ctx["awaiting_confirmation"] = {"draft": after["awaiting"], "original_sentence": ""}
                ctx["conversation"] = [{"role": "jarvis", "text": after["awaiting"]}]
            elif after:
                ctx["last_list"] = {"filters": after, "jarvis_said": "…"}
                ctx["conversation"] = [{"role": "user", "text": "aaj kya hai"}, {"role": "jarvis", "text": "Here is the list."}]
            staff_names = [s["name"] for s in ctx["staff"]]
            cmd = ai_brain.interpret(text, ctx, staff_names, has_pending_requests=bool(ctx.get("pending_requests")), user=user)
            misses = {k: cmd.get(k) for k, v in want.items() if not _ok(cmd.get(k), v)}
            chosen = ", ".join(f"{k}={cmd.get(k)!r}" for k in ("intent", "view_kind", "view_who", "view_from", "view_to", "view_by",
                                                              "view_filter", "view_types", "view_from_whom", "view_priority", "view_words",
                                                              "due_in_minutes", "snooze_minutes", "duration_minutes", "setting_key", "setting_value")
                               if cmd.get(k))
            if misses:
                failed += 1
                self.stdout.write(self.style.ERROR(f"FAIL [{role}] {text}\n      got  {chosen}\n      miss {misses}\n      want {want}"))
            else:
                passed += 1
                self.stdout.write(self.style.SUCCESS(f"ok   [{role}] {text}\n      {chosen}"))
        self.stdout.write(f"\n{passed} passed, {failed} failed")
