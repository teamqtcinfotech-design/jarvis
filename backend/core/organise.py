"""Organising by voice: departments and their people, sections, and moving work into a section.

  "Ek naya department banao, Farm, aur usme Kiran ko add kar do"   -> department made, Kiran in it, Kiran told
  "Nikki ko Social Media se hata do"                               -> asks first, then removes
  "Farm ka naam badal ke Farms and Dairy kar do"                   -> renamed
  "Marketing department delete kar do"                             -> asks first
  "GST section banao" / "ise Partners mein daal do"                -> section made / the task on screen moved
  "Franchise ko Partners mein merge kar do"                        -> asks first

The main brain only says "this is organising" (intent organise); the sentence is read here by a small reader of its
own (the AI with the department, section and team lists, or rules when it is off). Adding and making happen at once
and can be undone for a minute; removing, deleting and merging ask first. Departments are the MD's; anyone can make a
work section by naming it. A plan that comes back from the phone is untrusted: everything is looked up again.
"""
import json
import logging
import re

import anthropic
from django.conf import settings

from . import ai_brain, ai_usage, sections, services
from . import lang as lang_mod
from .models import AiUsage, Department, Section, SharedTask, User
from .services import ServiceError

log = logging.getLogger("jarvis.organise")

WHATS = ("department", "section", "task", "none")
DOS = ("create", "rename", "delete", "add", "remove", "move", "merge", "show", "none")

READ_SCHEMA = {
    "type": "object",
    "properties": {
        "what": {"type": "string", "enum": list(WHATS)},
        "do": {"type": "string", "enum": list(DOS)},
        "name": {"type": "string", "description": "the department or section named (for task: the section to move INTO)"},
        "new_name": {"type": "string", "description": "rename: the new name; merge: the section everything goes INTO; else empty"},
        "people": {"type": "array", "items": {"type": "string"}, "description": "people named, as said (names or jobs)"},
        "task_words": {"type": "string", "description": "task/move: words that pick out the task ('Pune wala', 'GST file'), or 'this' for the one on screen"},
        "personal": {"type": "boolean", "description": "section: the speaker's own personal folder, not a shared work section"},
    },
    "required": ["what", "do", "name", "new_name", "people", "task_words", "personal"],
    "additionalProperties": False,
}

READ_PROMPT = """You read one spoken instruction (English, Hindi or Hinglish as a phone's speech recogniser wrote it) about organising an Indian company's team in JARVIS, and say what it asks.

Departments are groups of PEOPLE (Accounts, Farm, Social Media). Sections are FOLDERS OF WORK (GST, Franchise, Partners). A task can be moved into a section. The context lists the departments (with their members), the sections, the team's names and the card on screen (if any).

what + do:
- department create: "naya department banao Farm", "make a Marketing department (with Seema)" -> name = the department; people = anyone to put in it.
- department add: "Kiran ko Farm mein daalo / add kar do", "put Sunil in Accounts" -> name = the department, people = who.
- department remove: "Nikki ko Social Media se hata do / nikaal do", "take Sunil out of Accounts".
- department rename: "Farm ka naam badal ke Farms and Dairy kar do", "rename Accounts to Finance" -> name = old, new_name = new.
- department delete: "Marketing department delete / hata / khatam kar do".
- department show: "Accounts department dikhao", "Farm mein kaun hai" -> name.
- section create: "GST section banao", "ek section banao Franchise ke liye" -> name. personal = true for "mera personal section", "apna folder".
- section rename / delete / merge: "GST ko Tax naam do" (name GST, new_name Tax); "Expo section hata do"; "Franchise ko Partners mein merge kar do" (name = Franchise, new_name = Partners: everything goes INTO Partners).
- task move: "ise Partners mein daal do", "ye GST mein rakho", "Pune wala task Franchise section mein daalo", "move the bank task to Accounts" -> name = the section, task_words = the words that pick the task ("this" / "ye" / "ise" = the one on screen).
- Something else entirely (a task to give, a question, a message) -> what none, do none.
Names: copy them as said (JARVIS matches them; a slightly misheard name is fine). A "team" said with a department's name means that department ("accounts team" = Accounts). Fill every field; "" / [] / false for what does not apply."""

_DEPT_WORD = r"(?:department|dept|departmnt|section|folder|team|vibhag|विभाग|डिपार्टमेंट|सेक्शन)"
_CREATE = re.compile(r"\b(banao|bana do|banana|create|make|naya|nayi|new|add a)\b|बनाओ|नया|नई", re.IGNORECASE)
_DELETE = re.compile(r"\b(delete|hata do|hatao|khatam|remove the|band kar)\b|हटा|डिलीट|ख़त्म|खतम", re.IGNORECASE)
_RENAME = re.compile(r"\b(rename|naam badal|naam badlo|naam do|naam rakh|call it)\b|नाम बदल|नाम रख", re.IGNORECASE)
_MERGE = re.compile(r"\b(merge|mila do|jod do|combine)\b|मर्ज|मिला दो|जोड़ दो", re.IGNORECASE)
_REMOVE_PERSON = re.compile(r"\b([a-z]+(?:\s+[a-z]+)?)\s+ko\s+([a-z][a-z ]*?)\s+(?:se|mein se)\s+(?:hata|nikaal|nikal|bahar)", re.IGNORECASE)
_ADD_PERSON = re.compile(r"\b([a-z]+(?:\s+[a-z]+)?)\s+ko\s+([a-z][a-z ]*?)\s+(?:mein|me)\s+(?:daal|dal|add|rakh|le lo|shamil)", re.IGNORECASE)
_MOVE_TASK = re.compile(r"\b(?:ise|isko|ye|yeh|this|is task|is kaam|it)\b.*?\b([a-z][a-z ]*?)\s+(?:section\s+)?(?:mein|me|into|to)\s+(?:daal|dal|rakh|move|put|bhej)", re.IGNORECASE)
_NAME_AFTER = re.compile(rf"(?:{_DEPT_WORD})\s+(?:banao|bana do|create|make|delete|hata do|hatao)?\s*([A-Za-z][A-Za-z ]{{1,40}}?)(?:\s+(?:aur|and|ke liye|banao|bana do|kar do|delete|hata)|[,.]|$)", re.IGNORECASE)
_NAME_BEFORE = re.compile(rf"([A-Za-z][A-Za-z ]{{1,40}}?)\s+(?:{_DEPT_WORD})\b", re.IGNORECASE)
_SECTION_WORD = re.compile(r"\b(section|folder)\b|सेक्शन|फ़ोल्डर", re.IGNORECASE)
_PERSONAL = re.compile(r"\b(personal|mera|meri|apna|apni|private|niji)\b|पर्सनल|मेरा|अपना|निजी", re.IGNORECASE)
_THIS = re.compile(r"\b(ise|isko|ye|yeh|this|it|is task|is kaam)\b|इसे|इसको|ये|यह", re.IGNORECASE)


_VERBISH = {"ek", "naya", "nayi", "new", "a", "an", "the", "create", "make", "banao", "bana", "banana", "delete", "hata", "hatao",
            "rename", "merge", "ko", "ka", "ki", "ke", "mein", "me", "is", "ye", "yeh", "this", "that", "us", "wo", "woh",
            "add", "kar", "karo", "do", "naam", "badal", "badlo", "please", "jarvis", "hey"}


def _name_in(text: str) -> str:
    """The department or section named: the words before "department" / "section" ("Marketing department"), else
    the words after ("department banao Farm"), with the verbs and fillers around them dropped."""
    for pattern in (_NAME_BEFORE, _NAME_AFTER):
        m = pattern.search(text)
        if not m:
            continue
        words = [w for w in m.group(1).split() if w.lower() not in _VERBISH]
        if words:
            return sections.strip_suffixes(" ".join(words))
    return ""


def _empty() -> dict:
    return {"what": "none", "do": "none", "name": "", "new_name": "", "people": [], "task_words": "", "personal": False}


def _read_ai(text: str, user: User, on_screen: dict | None) -> dict | None:
    if not ai_brain.ai_available():
        return None
    ctx = sections.ai_lists(user)
    ctx["team"] = [u.spoken_name for u in services.team()]
    if on_screen:
        ctx["on_screen"] = {"title": on_screen.get("title", ""), "items": [i["title"] for i in on_screen.get("items", [])[:8]]}
    try:
        response = ai_brain._get_client().messages.create(
            model=settings.JARVIS_AI_COMMAND_MODEL, max_tokens=300, system=READ_PROMPT,
            messages=[{"role": "user", "content": f"<context>{json.dumps(ctx, ensure_ascii=False)}</context>\n<instruction>{text[:600]}</instruction>"}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": READ_SCHEMA}},
        )
        ai_usage.record(user, AiUsage.Kind.COMMAND, response)
        if response.stop_reason in ("refusal", "max_tokens"):
            return None
        data = json.loads(ai_brain._first_text(response))
    except anthropic.APIStatusError as exc:
        log.error("AI rejected the organise reader (%s): %s", exc.status_code, str(getattr(exc, "message", ""))[:300])
        return None
    except (anthropic.APIConnectionError, json.JSONDecodeError, StopIteration) as exc:
        log.warning("organise reader unavailable: %s", type(exc).__name__)
        return None
    out = _empty()
    out.update({k: data.get(k, out[k]) for k in out})
    if out["what"] not in WHATS or out["do"] not in DOS:
        return _empty()
    out["people"] = [str(p) for p in (out["people"] or []) if str(p).strip()][:12]
    return out


def _read_rules(text: str) -> dict:
    """Offline reading of the common shapes."""
    out = _empty()
    t = " ".join(text.split())
    low = t.lower()
    what = "section" if _SECTION_WORD.search(low) else "department" if re.search(_DEPT_WORD, low, re.IGNORECASE) else "none"
    m = _REMOVE_PERSON.search(low)
    if m:
        out.update(what="department", do="remove", name=sections.strip_suffixes(m.group(2)), people=[m.group(1).strip()])
        return out
    m = _MOVE_TASK.search(low)
    if m and (_THIS.search(low) or "task" in low or "kaam" in low):
        out.update(what="task", do="move", name=sections.strip_suffixes(m.group(1)), task_words="this")
        return out
    m = _ADD_PERSON.search(low)
    if m and not _CREATE.search(low):
        out.update(what="department", do="add", name=sections.strip_suffixes(m.group(2)), people=[m.group(1).strip()])
        return out
    name = _name_in(t)
    if _MERGE.search(low):
        parts = re.split(r"\b(?:ko|into|with|mein|me)\b", low, maxsplit=1)
        src = sections.strip_suffixes(re.sub(r"\b(merge|mila do|jod do|combine|kar do|karo)\b", " ", parts[0]))
        dst = sections.strip_suffixes(re.sub(r"\b(merge|mila do|jod do|combine|kar do|karo|mein|me)\b", " ", parts[1])) if len(parts) > 1 else ""
        out.update(what="section", do="merge", name=src, new_name=dst)
        return out
    if _RENAME.search(low):
        m2 = re.search(r"(?:badal ke|rename\s+\S+\s+to|to|ko)\s+([A-Za-z][A-Za-z ]{1,40}?)\s*(?:kar do|karo|naam|rakho|$)", t, re.IGNORECASE)
        out.update(what=what if what != "none" else "department", do="rename", name=name, new_name=sections.strip_suffixes(m2.group(1)) if m2 else "")
        return out
    if _DELETE.search(low) and what != "none":
        out.update(what=what, do="delete", name=name)
        return out
    if _CREATE.search(low) and what != "none":
        people = []
        m3 = re.search(r"(?:usme|usmein|is mein|ismein|with|including)\s+([A-Za-z][A-Za-z ,]+?)\s+(?:ko\s+)?(?:add|daal|dal|rakh|shamil)", t, re.IGNORECASE)
        if m3:
            people = [p.strip() for p in re.split(r",|\baur\b|\band\b", m3.group(1)) if p.strip()]
        out.update(what=what, do="create", name=name, people=people, personal=bool(what == "section" and _PERSONAL.search(low)))
        return out
    if what != "none" and name and re.search(r"\b(dikhao|show|kaun|who|batao|details?)\b|दिखाओ|कौन", low):
        out.update(what=what, do="show", name=name)
    return out


def read(text: str, user: User, on_screen: dict | None = None) -> dict:
    return _read_ai(text, user, on_screen) or _read_rules(text)


# --------------------------------------------------------------------------- planning
def _people_of(user: User, names: list[str]) -> tuple[list[User], str]:
    """The team members named; ("", question) when one is unknown or fits two."""
    people = []
    for raw in names:
        found = services.find_people(raw)
        if found["status"] in ("known", "close"):
            people.append(found["people"][0])
        elif found["status"] == "ambiguous":
            return [], f"which {raw}: {' or '.join(found['options'])}?"
        else:
            hint = f" Did you mean {found['options'][0]}?" if found.get("options") else ""
            return [], f"I don't know anyone called {raw} on the team.{hint}"
    return people, ""


def _dept(name: str, active: bool = True) -> tuple[Department | None, str]:
    found = sections.find_department(name, active)
    if found["status"] in ("known", "close"):
        return found["department"], ""
    if found["status"] == "ambiguous":
        return None, f"which department: {' or '.join(found['options'])}?"
    hint = f" Did you mean {found['options'][0]}?" if found.get("options") else ""
    return None, f"there is no department called {name}.{hint}"


def _section(name: str, user: User, personal: bool = False) -> tuple[Section | None, str]:
    kind = Section.Kind.PERSONAL if personal else Section.Kind.WORK
    found = sections.find_section(name, user if personal else None, kind)
    if found["status"] in ("known", "close"):
        return found["section"], ""
    if found["status"] == "ambiguous":
        return None, f"which section: {' or '.join(found['options'])}?"
    hint = f" Did you mean {found['options'][0]}?" if found.get("options") else ""
    return None, f"there is no section called {name}.{hint}"


def _task_of(user: User, words: str, on_screen: dict | None) -> tuple[SharedTask | None, str]:
    """The work item meant: the one on screen for "this", else by its words among what the speaker can see."""
    visible = services.visible_shared_qs(user).exclude(status__in=["COMPLETED", "CANCELLED"])
    if (not words or words.strip().lower() in ("this", "ye", "yeh", "ise", "isko", "it")) and on_screen:
        items = on_screen.get("items") or []
        if len(items) == 1 or (items and on_screen.get("kind") in ("task", "item")):
            task = visible.filter(id=items[0]["id"]).first()
            if task:
                return task, ""
    tokens = [w for w in sections.words_of(words or "") if w]
    if not tokens:
        if on_screen and (on_screen.get("items") or []):
            task = visible.filter(id=on_screen["items"][0]["id"]).first()
            if task:
                return task, ""
        return None, "which task? Say a few words from its name."
    hits = [t for t in visible.order_by("-created_at")[:300] if any(w in sections.norm(t.title) for w in tokens)]
    if len(hits) == 1:
        return hits[0], ""
    if not hits:
        return None, f"I found no open task with the words {words}."
    return None, f"which one: {' or '.join(t.title for t in hits[:3])}?"


def plan(user: User, text: str, on_screen: dict | None = None, read_result: dict | None = None) -> dict:
    """{"done", "en", "hi", "undo", "view"} when it was done at once; {"confirm", "question", "question_hi", "plan"}
    when it must be confirmed; {"ask", "ask_hi"} for a question back; {"nothing": True} when it is not organising."""
    r = read_result or read(text, user, on_screen)
    what, do = r.get("what"), r.get("do")
    if what in ("none", None) or do in ("none", None):
        return {"nothing": True}
    address, address_hi = services.address_of(user), services.address_hi_of(user)

    def ask(en, hi=""):
        return {"ask": f"{address}, {en}", "ask_hi": f"{address_hi}, {hi or en}"}

    if what == "department":
        if do == "show":
            dept, err = _dept(r["name"])
            if dept is None:
                return ask(err)
            return {"done": True, "view": sections.department_card(user, dept)}
        if not user.is_md:
            return ask(f"only {services.md_address()} can change departments.", f"सिर्फ़ {services.md_address_hi()} डिपार्टमेंट बदल सकते हैं।")
        if do == "create":
            if not r["name"]:
                return ask("what should the department be called?", "डिपार्टमेंट का नाम क्या रखूँ?")
            people, err = _people_of(user, r["people"])
            if err:
                return ask(err)
            for p in people:
                try:
                    sections.eligible(p)
                except ServiceError as e:
                    return ask(e.message)
            return apply(user, {"what": "department", "do": "create", "name": r["name"], "people": [p.id for p in people]})
        if do in ("add", "remove"):
            dept, err = _dept(r["name"])
            if dept is None:
                return ask(err)
            people, err = _people_of(user, r["people"])
            if err:
                return ask(err)
            if not people:
                return ask("who should I add?" if do == "add" else "who should I take out?", "किसे जोड़ूँ?" if do == "add" else "किसे हटाऊँ?")
            if do == "add":
                for p in people:
                    try:
                        sections.eligible(p)
                    except ServiceError as e:
                        return ask(e.message)
                return apply(user, {"what": "department", "do": "add", "department": dept.id, "people": [p.id for p in people]})
            names = services.names_phrase(people)
            return {"confirm": True, "question": f"{address}, take {names} out of {dept.name}?",
                    "question_hi": f"{address_hi}, {services.names_phrase(people, hindi=True)} को {dept.name} से हटा दूँ?",
                    "plan": {"what": "department", "do": "remove", "department": dept.id, "people": [p.id for p in people]}}
        if do == "rename":
            dept, err = _dept(r["name"])
            if dept is None:
                return ask(err)
            if not r["new_name"]:
                return ask(f"what should {dept.name} be called now?", f"{dept.name} को अब क्या नाम दूँ?")
            return apply(user, {"what": "department", "do": "rename", "department": dept.id, "new_name": r["new_name"]})
        if do == "delete":
            dept, err = _dept(r["name"])
            if dept is None:
                return ask(err)
            n = dept.members.filter(is_active=True).count()
            tail = f" Its {n} {'person stays' if n == 1 else 'people stay'} on the team, just not in it." if n else ""
            tail_hi = f" इसके {n} लोग टीम में रहेंगे, बस इस डिपार्टमेंट में नहीं।" if n else ""
            return {"confirm": True, "question": f"{address}, delete the {dept.name} department?{tail}",
                    "question_hi": f"{address_hi}, {dept.name} डिपार्टमेंट हटा दूँ?{tail_hi}",
                    "plan": {"what": "department", "do": "delete", "department": dept.id}}
        return {"nothing": True}

    if what == "section":
        personal = bool(r.get("personal"))
        if do == "show":
            sec, err = _section(r["name"], user, personal)
            if sec is None:
                return ask(err)
            from . import datacards
            view = datacards.build(user, "tasks", params={"who": "all" if user.is_md else "me", "group": sec.name, "filter": "open"})
            return {"done": True, "view": view}
        if do == "create":
            if not r["name"]:
                return ask("what should the section be called?", "सेक्शन का नाम क्या रखूँ?")
            return apply(user, {"what": "section", "do": "create", "name": r["name"], "personal": personal})
        sec, err = _section(r["name"], user, personal)
        if sec is None:
            return ask(err)
        if sec.kind == Section.Kind.WORK and not user.is_md:
            return ask(f"only {services.md_address()} can rename, merge or delete a section.", f"सिर्फ़ {services.md_address_hi()} सेक्शन बदल या हटा सकते हैं।")
        if do == "rename":
            if not r["new_name"]:
                return ask(f"what should {sec.name} be called now?", f"{sec.name} को अब क्या नाम दूँ?")
            return apply(user, {"what": "section", "do": "rename", "section": sec.id, "new_name": r["new_name"]})
        if do == "merge":
            dst, err = _section(r["new_name"], user, personal) if r["new_name"] else (None, "merge it into which section?")
            if dst is None:
                return ask(err)
            n = SharedTask.objects.filter(section=sec).count()
            return {"confirm": True, "question": f"{address}, merge {sec.name} into {dst.name}? Its {n} tasks move there; this cannot be undone.",
                    "question_hi": f"{address_hi}, {sec.name} को {dst.name} में मिला दूँ? इसके {n} काम वहाँ जाएँगे; ये वापस नहीं होगा।",
                    "plan": {"what": "section", "do": "merge", "section": sec.id, "into": dst.id}}
        if do == "delete":
            n = SharedTask.objects.filter(section=sec).exclude(status__in=["COMPLETED", "CANCELLED"]).count()
            tail = f" Its {n} open {'task keeps' if n == 1 else 'tasks keep'} going, without a section." if n else ""
            tail_hi = f" इसके {n} काम चलते रहेंगे, बिना सेक्शन के।" if n else ""
            return {"confirm": True, "question": f"{address}, delete the {sec.name} section?{tail}",
                    "question_hi": f"{address_hi}, {sec.name} सेक्शन हटा दूँ?{tail_hi}",
                    "plan": {"what": "section", "do": "delete", "section": sec.id}}
        return {"nothing": True}

    if what == "task" and do == "move":
        if not r["name"]:
            return ask("into which section?", "किस सेक्शन में?")
        task, err = _task_of(user, r.get("task_words") or "", on_screen)
        if task is None:
            return ask(err)
        return apply(user, {"what": "task", "do": "move", "task": str(task.id), "name": r["name"]})
    return {"nothing": True}


# --------------------------------------------------------------------------- doing it
def apply(user: User, p: dict) -> dict:
    """Do a plan (fresh from `plan`, or back from the phone after a yes: untrusted, everything is looked up again).
    Returns {"done": True, "en", "hi", "undo": [refs], "view"?}; raises ServiceError."""
    address, address_hi = services.address_of(user), services.address_hi_of(user)
    what, do = p.get("what"), p.get("do")
    people = list(User.objects.filter(id__in=[int(i) for i in (p.get("people") or []) if str(i).isdigit()], is_active=True))

    if what == "department":
        if do == "create":
            dept, created = sections.create_department(user, p.get("name") or "", people)
            names = services.names_phrase(people) if people else ""
            names_hi = services.names_phrase(people, hindi=True) if people else ""
            made_en = f"{dept.name} department made" if created else f"{dept.name} already exists"
            made_hi = f"{dept.name} डिपार्टमेंट बन गया" if created else f"{dept.name} पहले से है"
            en = f"Done, {address}. {made_en}" + (f"; {names} {'is' if len(people) == 1 else 'are'} in it." if people else ".")
            hi = f"हो गया, {address_hi}। {made_hi}" + (f"; {names_hi} इसमें {'है' if len(people) == 1 else 'हैं'}।" if people else "।")
            return {"done": True, "en": en, "hi": hi, "view": sections.department_card(user, dept),
                    "undo": [{"type": "organise", "inverse": {"what": "department", "do": "delete" if created else "remove",
                                                               "department": dept.id, "people": [u.id for u in people]}}]}
        dept = Department.objects.filter(id=p.get("department")).first()
        if dept is None:
            raise ServiceError("gone", "that department is gone.", 404)
        if do == "add":
            added = sections.add_members(user, dept, people)
            names = services.names_phrase(added or people)
            names_hi = services.names_phrase(added or people, hindi=True)
            if not added:
                return {"done": True, "en": f"{address}, {names} {'is' if len(people) == 1 else 'are'} already in {dept.name}.",
                        "hi": f"{address_hi}, {names_hi} पहले से {dept.name} में {'है' if len(people) == 1 else 'हैं'}।", "undo": []}
            return {"done": True, "en": f"Done, {address}: {names} {'is' if len(added) == 1 else 'are'} in {dept.name} now. I've told {'them' if len(added) > 1 else names}.",
                    "hi": f"हो गया, {address_hi}: {names_hi} अब {dept.name} में {'है' if len(added) == 1 else 'हैं'}। उन्हें बता दिया।",
                    "view": sections.department_card(user, dept),
                    "undo": [{"type": "organise", "inverse": {"what": "department", "do": "remove", "department": dept.id, "people": [u.id for u in added]}}]}
        if do == "remove":
            removed = sections.remove_members(user, dept, people)
            names = services.names_phrase(removed or people)
            names_hi = services.names_phrase(removed or people, hindi=True)
            if not removed:
                return {"done": True, "en": f"{address}, {names} {'was' if len(people) == 1 else 'were'} not in {dept.name}.",
                        "hi": f"{address_hi}, {names_hi} {dept.name} में {'था' if len(people) == 1 else 'थे'} ही नहीं।", "undo": []}
            return {"done": True, "en": f"Done, {address}: {names} {'is' if len(removed) == 1 else 'are'} out of {dept.name}. I've told {'them' if len(removed) > 1 else names}.",
                    "hi": f"हो गया, {address_hi}: {names_hi} अब {dept.name} में नहीं {'है' if len(removed) == 1 else 'हैं'}। उन्हें बता दिया।",
                    "view": sections.department_card(user, dept),
                    "undo": [{"type": "organise", "inverse": {"what": "department", "do": "add", "department": dept.id, "people": [u.id for u in removed]}}]}
        if do == "rename":
            old = sections.rename_department(user, dept, p.get("new_name") or "")
            return {"done": True, "en": f"Done, {address}: {old} is now called {dept.name}.", "hi": f"हो गया, {address_hi}: {old} अब {dept.name} कहलाएगा।",
                    "undo": [{"type": "organise", "inverse": {"what": "department", "do": "rename", "department": dept.id, "new_name": old}}]}
        if do == "delete":
            people_before = sections.delete_department(user, dept)
            return {"done": True, "en": f"Done, {address}: the {dept.name} department is gone. Its people are still on the team.",
                    "hi": f"हो गया, {address_hi}: {dept.name} डिपार्टमेंट हट गया। इसके लोग टीम में हैं।",
                    "undo": [{"type": "organise", "inverse": {"what": "department", "do": "restore", "department": dept.id, "people": [u.id for u in people_before]}}]}
        if do == "restore":
            dept.active = True
            dept.save(update_fields=["active"])
            sections.add_members(user, dept, people)
            return {"done": True, "en": f"{dept.name} is back.", "hi": f"{dept.name} वापस आ गया।", "undo": []}

    if what == "section":
        if do == "create":
            kind = Section.Kind.PERSONAL if p.get("personal") else Section.Kind.WORK
            sec, created = sections.create_section(user, p.get("name") or "", kind)
            which = "personal section" if kind == Section.Kind.PERSONAL else "section"
            which_hi = "पर्सनल सेक्शन" if kind == Section.Kind.PERSONAL else "सेक्शन"
            if not created:
                return {"done": True, "en": f"{address}, the {sec.name} {which} already exists.", "hi": f"{address_hi}, {sec.name} {which_hi} पहले से है।", "undo": []}
            return {"done": True, "en": f"Done, {address}: {sec.name} {which} made. Say '{sec.name} mein daalo' to file work there.",
                    "hi": f"हो गया, {address_hi}: {sec.name} {which_hi} बन गया। काम वहाँ रखने के लिए '{sec.name} mein daalo' बोलिए।",
                    "undo": [{"type": "organise", "inverse": {"what": "section", "do": "delete", "section": sec.id}}]}
        sec = Section.objects.filter(id=p.get("section")).first()
        if sec is None:
            raise ServiceError("gone", "that section is gone.", 404)
        if do == "rename":
            old = sections.rename_section(user, sec, p.get("new_name") or "")
            return {"done": True, "en": f"Done, {address}: {old} is now called {sec.name}.", "hi": f"हो गया, {address_hi}: {old} अब {sec.name} कहलाएगा।",
                    "undo": [{"type": "organise", "inverse": {"what": "section", "do": "rename", "section": sec.id, "new_name": old}}]}
        if do == "merge":
            dst = Section.objects.filter(id=p.get("into")).first()
            if dst is None:
                raise ServiceError("gone", "that section is gone.", 404)
            n = sections.merge_sections(user, sec, dst)
            return {"done": True, "en": f"Done, {address}: {sec.name} is merged into {dst.name}, {n} {'task' if n == 1 else 'tasks'} moved.",
                    "hi": f"हो गया, {address_hi}: {sec.name} को {dst.name} में मिला दिया, {n} काम वहाँ गए।", "undo": []}
        if do == "delete":
            ids = [str(i) for i in SharedTask.objects.filter(section=sec).values_list("id", flat=True)]
            n = sections.delete_section(user, sec)
            return {"done": True, "en": f"Done, {address}: the {sec.name} section is gone" + (f"; its {n} {'task keeps' if n == 1 else 'tasks keep'} going without one." if n else "."),
                    "hi": f"हो गया, {address_hi}: {sec.name} सेक्शन हट गया" + (f"; इसके {n} काम बिना सेक्शन के चलेंगे।" if n else "।"),
                    "undo": [{"type": "organise", "inverse": {"what": "section", "do": "restore", "section": sec.id, "tasks": ids[:200]}}]}
        if do == "restore":
            sec.active = True
            sec.save(update_fields=["active"])
            SharedTask.objects.filter(id__in=p.get("tasks") or []).update(section=sec)
            return {"done": True, "en": f"{sec.name} is back.", "hi": f"{sec.name} वापस आ गया।", "undo": []}

    if what == "task" and do == "move":
        task_before = SharedTask.objects.filter(id=p.get("task")).select_related("section").first()
        old = task_before.section if task_before else None
        task, sec = sections.move_task(user, p.get("task"), p.get("name") or "")
        return {"done": True, "en": f"Done, {address}: {task.title} is in {sec.name} now.", "hi": f"हो गया, {address_hi}: {task.title} अब {sec.name} में है।",
                "undo": [{"type": "organise", "inverse": {"what": "task", "do": "move_back", "task": str(task.id), "section": old.id if old else None}}]}
    if what == "task" and do == "move_back":
        task = SharedTask.objects.filter(id=p.get("task")).first()
        if task is not None:
            sec = Section.objects.filter(id=p.get("section")).first() if p.get("section") else None
            task.section = sec
            task.version += 1
            task.save(update_fields=["section", "version", "updated_at"])
        return {"done": True, "en": "Moved back.", "hi": "वापस रख दिया।", "undo": []}
    raise ServiceError("invalid", "I can't do that here.", 422)


def undo(user: User, inverse: dict) -> int:
    """Take an organising step back (see apply's undo refs). Returns 1 when something changed."""
    try:
        out = apply(user, inverse)
    except ServiceError:
        return 0
    return 1 if out.get("done") else 0


def describe(p: dict) -> str:
    """One line for the waiting card: what a yes will do."""
    what, do = p.get("what"), p.get("do")
    name = ""
    if what == "department" and p.get("department"):
        d = Department.objects.filter(id=p["department"]).first()
        name = d.name if d else ""
    elif what == "section" and p.get("section"):
        s = Section.objects.filter(id=p["section"]).first()
        name = s.name if s else ""
    verb = {"remove": "Take people out of", "delete": "Delete", "merge": "Merge"}.get(do, do.title() if do else "")
    return f"{verb} {name}".strip()


def language_of(user: User, text: str) -> str:
    return lang_mod.lang_for(user, text)
