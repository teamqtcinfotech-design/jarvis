"""Departments (groups of people) and sections (folders for work), Phase 3 of the team plan (17 Nov 2026).

Departments: made, renamed and deleted by the MD, by voice or on the Departments page; members added and removed the
same way. Only team members belong to one: the MD, the manager and the PA work across all of them and are refused.
Two exist by default (Accounts, Social Media); everything else is built by voice.

Sections: shared folders for work (GST, Accounts, Franchise…) plus each person's own personal folders. Anyone makes a
work section by naming it; renaming, merging and deleting are the MD's. Every task is filed into one from its words
(each section's name and aliases), else the department of the person it is for, else the giver's; a section said
aloud always wins and is made if new. Moving a task by voice teaches the section the task's words.
"""
import difflib
import json
import logging
import re

from django.db import transaction
from django.utils import timezone

from . import services
from .models import Department, PrivateTask, Section, SharedTask, Status, TaskEvent, User
from .services import ServiceError

log = logging.getLogger("jarvis.sections")

DEFAULT_DEPARTMENTS = (("Accounts", "अकाउंट्स", ["accounts", "account", "hisaab", "hisab", "khata", "finance", "accounting"]),
                       ("Social Media", "सोशल मीडिया", ["social media", "social", "instagram", "insta", "reels", "reel",
                                                        "facebook", "youtube", "post", "posts", "content"]))
COLOURS = ["#4F46E5", "#0EA5E9", "#10B981", "#F59E0B", "#EF4444", "#8B5CF6", "#EC4899", "#14B8A6", "#F97316", "#64748B"]
MAX_ALIASES = 40

_SUFFIXES = (" department", " dept", " team", " section", " folder", " walon", " waalon", " wale", " waale", " log",
             " ke", " ka", " ki", " mein", " me", " wala", " waala", " ko")
_STOP = {
    "the", "a", "an", "to", "for", "of", "in", "on", "at", "with", "and", "or", "by", "is", "it", "this", "that", "my", "me",
    "our", "your", "his", "her", "their", "from", "about", "please", "sir", "maam", "madam", "ji", "ko", "ka", "ki", "ke",
    "se", "mein", "me", "par", "pe", "aur", "ya", "hai", "hain", "ho", "kar", "karo", "karna", "karni", "karne", "kardo",
    "kar do", "de", "do", "dena", "dedo", "bhej", "bhejo", "bhejna", "wala", "wali", "wale", "liye", "tak", "kal", "aaj",
    "parso", "subah", "shaam", "sham", "raat", "baje", "ek", "do", "teen", "task", "tasks", "kaam", "work", "meeting",
    "call", "bolo", "bol", "kaho", "batao", "bata", "ye", "yeh", "wo", "woh", "isko", "usko", "unko", "inko", "new", "naya",
    "nayi", "file", "send", "make", "banao", "bana", "give", "get", "lo", "le", "lena", "rakho", "rakh", "daal", "daalo",
    "urgent", "turant", "jaldi", "important", "zaroori", "zaruri", "today", "tomorrow", "friday", "monday", "tuesday",
    "wednesday", "thursday", "saturday", "sunday", "week", "month", "hafte", "mahine", "din", "minute", "ghante", "pm", "am",
}
_TOKEN = re.compile(r"[^\W\d_]+", re.UNICODE)


# --------------------------------------------------------------------------- words
def norm(text: str) -> str:
    t = " ".join(str(text or "").lower().replace("’", "'").replace("'", "").split())
    t = re.sub(r"[^\w\sऀ-ॿ]", " ", t)
    return " ".join(t.split())


def strip_suffixes(text: str) -> str:
    t = norm(text)
    changed = True
    while changed:
        changed = False
        for suf in _SUFFIXES:
            if t.endswith(suf) and len(t) > len(suf):
                t = t[: -len(suf)].strip()
                changed = True
    return t


def words_of(text: str) -> list[str]:
    return [w for w in _TOKEN.findall(norm(text)) if len(w) >= 3 and w not in _STOP]


def _keys(name: str, name_hi: str, aliases: list) -> set[str]:
    keys = {norm(name), strip_suffixes(name), norm(name_hi)} | {norm(a) for a in (aliases or []) if a}
    return {k for k in keys if k}


def _display(name: str) -> str:
    name = " ".join(str(name or "").split())[:80]
    return " ".join(w if w.isupper() and len(w) <= 4 else w[:1].upper() + w[1:] for w in name.split())


def _colour(n: int) -> str:
    return COLOURS[n % len(COLOURS)]


def _find(name: str, rows: list, key_of) -> dict:
    """{"status": known | close | ambiguous | unknown, "item", "options"} for a name said against rows of things."""
    key = strip_suffixes(name)
    out = {"status": "unknown", "item": None, "options": []}
    if not key:
        return out
    exact = [r for r in rows if key in key_of(r)]
    if len(exact) == 1:
        return {"status": "known", "item": exact[0], "options": []}
    if len(exact) > 1:
        return {"status": "ambiguous", "item": None, "options": [r.name for r in exact[:4]]}
    if len(key) >= 4:
        scored = sorted(((max((difflib.SequenceMatcher(None, key, k).ratio() for k in key_of(r)), default=0), r) for r in rows),
                        key=lambda p: p[0], reverse=True)
        if scored and scored[0][0] >= 0.78 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.08):
            return {"status": "close", "item": scored[0][1], "options": []}
        near = [r.name for s, r in scored[:2] if s >= 0.6]
        if near:
            out["options"] = near
    return out


# --------------------------------------------------------------------------- departments
def departments(active: bool = True) -> list[Department]:
    qs = Department.objects.prefetch_related("members").order_by("name")
    return list(qs.filter(active=True) if active else qs)


def dept_keys(d: Department) -> set[str]:
    return _keys(d.name, d.name_hi, d.aliases)


def find_department(name: str, active: bool = True) -> dict:
    found = _find(name, departments(active), dept_keys)
    return {"status": found["status"], "department": found["item"], "options": found["options"]}


def management() -> list[User]:
    """The manager and the PA: never inside a department, shown on their own line (the manager first)."""
    people = [u for u in services.team() if u.is_pa or u.is_mg]
    return sorted(people, key=lambda u: (0 if u.is_mg else 1, u.spoken_name.lower()))


def eligible(person: User) -> None:
    if person.is_md or person.is_pa or person.is_mg:
        role = "the Managing Director" if person.is_md else "the manager" if person.is_mg else "the PA"
        raise ServiceError("not_eligible", f"{person.spoken_name} is {role}; they work across all departments.", 422)


def _manager_only(user: User) -> None:
    if not user.is_md:
        raise ServiceError("forbidden", f"Only {services.md_address()} can change departments.", 403)


def _mirror(person: User) -> None:
    """`User.department` (text) = the person's first department, for the older screens and the AI context."""
    first = person.departments.filter(active=True).order_by("name").first()
    text = first.name if first else ""
    if person.department != text:
        person.department = text
        person.save(update_fields=["department"])


def _tell(person: User, by: User, en: str, hi: str, title: str) -> None:
    from .models import Alert, Priority
    if person.id == by.id or not person.is_active:
        return
    Alert.objects.create(recipient=person, sender=by, kind=Alert.Kind.INFO, priority=Priority.LOW, title=title[:200],
                         body=en[:400], spoken_text=en[:400], spoken_text_hi=hi[:400], ref_type="department", ref_id=str(person.id))


@transaction.atomic
def create_department(md: User, name: str, people: list[User] | None = None) -> tuple[Department, bool]:
    """Returns (department, created). A switched-off department of that name comes back; an active one is reused."""
    _manager_only(md)
    label = _display(name)
    if not label:
        raise ServiceError("invalid", "say the department's name.", 422)
    found = find_department(label, active=False)
    dept = found["department"] if found["status"] in ("known",) else None
    created = dept is None
    if dept is None:
        dept = Department.objects.create(name=label, created_by=md, colour=_colour(Department.objects.count()))
        services.audit(md, "department.create", "department", dept.id, label)
    elif not dept.active:
        dept.active = True
        dept.save(update_fields=["active"])
        created = True
        services.audit(md, "department.restore", "department", dept.id, dept.name)
    department_section(dept, by=md)
    if people:
        add_members(md, dept, people)
    return dept, created


@transaction.atomic
def rename_department(md: User, dept: Department, new_name: str) -> str:
    _manager_only(md)
    label = _display(new_name)
    if not label:
        raise ServiceError("invalid", "say the new name.", 422)
    if Department.objects.filter(name__iexact=label).exclude(id=dept.id).exists():
        raise ServiceError("taken", f"there is already a department called {label}.", 409)
    old = dept.name
    dept.name = label
    dept.save(update_fields=["name"])
    section = dept.sections.filter(active=True, kind=Section.Kind.WORK).first()
    if section and norm(section.name) == norm(old):
        section.name = label
        section.save(update_fields=["name"])
    for u in dept.members.all():
        _mirror(u)
    services.audit(md, "department.rename", "department", dept.id, f"{old} -> {label}")
    return old


@transaction.atomic
def delete_department(md: User, dept: Department) -> list[User]:
    """Switched off (it can be made again by name): its people are no longer in it; its section stays as a folder."""
    _manager_only(md)
    people = list(dept.members.all())
    dept.members.clear()
    dept.active = False
    dept.save(update_fields=["active"])
    for u in people:
        _mirror(u)
    services.audit(md, "department.delete", "department", dept.id, dept.name)
    return people


@transaction.atomic
def add_members(md: User, dept: Department, people: list[User]) -> list[User]:
    _manager_only(md)
    added = []
    for person in people:
        eligible(person)
        if not person.is_active:
            raise ServiceError("no_person", f"{person.spoken_name}'s account is switched off.", 422)
        if dept.members.filter(id=person.id).exists():
            continue
        dept.members.add(person)
        _mirror(person)
        added.append(person)
        who, who_hi = services.giver_words(md)
        _tell(person, md, f"{person.spoken_name}, {who} has put you in the {dept.name} department.",
              f"{person.spoken_name}, {who_hi} ने आपको {dept.name} डिपार्टमेंट में रखा है।", f"You are in {dept.name}")
    if added:
        services.audit(md, "department.add", "department", dept.id, ", ".join(u.username for u in added))
    return added


@transaction.atomic
def remove_members(md: User, dept: Department, people: list[User]) -> list[User]:
    _manager_only(md)
    removed = []
    for person in people:
        if not dept.members.filter(id=person.id).exists():
            continue
        dept.members.remove(person)
        _mirror(person)
        removed.append(person)
        who, who_hi = services.giver_words(md)
        _tell(person, md, f"{person.spoken_name}, {who} has taken you out of the {dept.name} department.",
              f"{person.spoken_name}, {who_hi} ने आपको {dept.name} डिपार्टमेंट से हटा दिया है।", f"Out of {dept.name}")
    if removed:
        services.audit(md, "department.remove", "department", dept.id, ", ".join(u.username for u in removed))
    return removed


def member_of(person: User) -> list[Department]:
    return list(person.departments.filter(active=True).order_by("name"))


def department_section(dept: Department, by: User | None = None) -> Section:
    """The work section that goes with a department (the same name), made if missing."""
    section = dept.sections.filter(active=True, kind=Section.Kind.WORK).first()
    if section:
        return section
    section = Section.objects.filter(kind=Section.Kind.WORK, owner=None, name__iexact=dept.name).first()
    if section is None:
        section = Section.objects.create(name=dept.name, name_hi=dept.name_hi, aliases=list(dept.aliases or []), kind=Section.Kind.WORK,
                                         colour=dept.colour or _colour(Section.objects.count()), created_by=by)
    section.department = dept
    section.active = True
    section.save(update_fields=["department", "active"])
    return section


def department_item(d: Department, user: User | None = None) -> dict:
    members = [u for u in d.members.all() if u.is_active]
    open_qs = SharedTask.objects.filter(assignee_user__in=members, assignee_type=SharedTask.Assignee.USER) \
        .exclude(status__in=[Status.COMPLETED, Status.CANCELLED])
    if user is not None and not user.is_md:
        open_qs = open_qs.filter(created_by=user)
    open_items = list(open_qs)
    section = d.sections.filter(active=True).first()
    return {
        "id": d.id, "name": d.name, "name_hi": d.name_hi, "colour": d.colour, "active": d.active,
        "members": [services.person_item(u, user) for u in sorted(members, key=lambda u: u.spoken_name.lower())],
        "open": len(open_items), "late": sum(1 for t in open_items if services.is_overdue(t)),
        "section": section.name if section else "",
    }


# --------------------------------------------------------------------------- sections
def sections(user: User | None = None, kind: str | None = None, active: bool = True) -> list[Section]:
    qs = Section.objects.select_related("department", "owner").order_by("name")
    if active:
        qs = qs.filter(active=True)
    if kind == Section.Kind.WORK:
        return list(qs.filter(kind=Section.Kind.WORK))
    if kind == Section.Kind.PERSONAL:
        return list(qs.filter(kind=Section.Kind.PERSONAL, owner=user)) if user else []
    work = list(qs.filter(kind=Section.Kind.WORK))
    own = list(qs.filter(kind=Section.Kind.PERSONAL, owner=user)) if user else []
    return work + own


def section_keys(s: Section) -> set[str]:
    return _keys(s.name, s.name_hi, s.aliases)


def find_section(name: str, user: User | None = None, kind: str | None = None, active: bool = True) -> dict:
    found = _find(name, sections(user, kind, active), section_keys)
    return {"status": found["status"], "section": found["item"], "options": found["options"]}


def _md_only_sections(user: User) -> None:
    if not user.is_md:
        raise ServiceError("forbidden", f"Only {services.md_address()} can rename, merge or delete sections.", 403)


@transaction.atomic
def create_section(user: User, name: str, kind: str = Section.Kind.WORK) -> tuple[Section, bool]:
    """Anyone makes a work section by naming it; a personal one belongs to its maker. Returns (section, created)."""
    label = _display(name)
    if not label:
        raise ServiceError("invalid", "say the section's name.", 422)
    owner = user if kind == Section.Kind.PERSONAL else None
    found = find_section(label, user if owner else None, kind, active=False)
    section = found["section"] if found["status"] == "known" else None
    if section is not None and section.kind == kind and section.owner_id == (owner.id if owner else None):
        created = False
        if not section.active:
            section.active = True
            section.save(update_fields=["active"])
            created = True
        return section, created
    section = Section.objects.create(name=label, kind=kind, owner=owner, created_by=user, colour=_colour(Section.objects.count()))
    services.audit(user, "section.create", "section", section.id, f"{kind} {label}")
    return section, True


@transaction.atomic
def rename_section(user: User, section: Section, new_name: str) -> str:
    if section.kind == Section.Kind.WORK:
        _md_only_sections(user)
    elif section.owner_id != user.id:
        raise ServiceError("forbidden", "that is not your section.", 403)
    label = _display(new_name)
    if not label:
        raise ServiceError("invalid", "say the new name.", 422)
    clash = Section.objects.filter(name__iexact=label, kind=section.kind, owner=section.owner).exclude(id=section.id).exists()
    if clash:
        raise ServiceError("taken", f"there is already a section called {label}.", 409)
    old = section.name
    section.aliases = sorted(set(section.aliases or []) | {norm(old)})[:MAX_ALIASES]
    section.name = label
    section.save(update_fields=["name", "aliases"])
    services.audit(user, "section.rename", "section", section.id, f"{old} -> {label}")
    return old


@transaction.atomic
def merge_sections(user: User, src: Section, dst: Section) -> int:
    """Everything in src moves to dst; src is switched off and its words become dst's. Returns how many moved."""
    if src.kind == Section.Kind.WORK or dst.kind == Section.Kind.WORK:
        _md_only_sections(user)
    if src.id == dst.id:
        raise ServiceError("same", "those are the same section.", 422)
    if src.kind != dst.kind or src.owner_id != dst.owner_id:
        raise ServiceError("invalid", "a personal section cannot be merged with a work section.", 422)
    n = SharedTask.objects.filter(section=src).update(section=dst) + PrivateTask.objects.filter(section=src).update(section=dst)
    dst.aliases = sorted(set(dst.aliases or []) | set(src.aliases or []) | {norm(src.name)})[:MAX_ALIASES]
    dst.save(update_fields=["aliases"])
    src.active = False
    src.save(update_fields=["active"])
    services.audit(user, "section.merge", "section", src.id, f"-> {dst.id} ({n})")
    return n


@transaction.atomic
def delete_section(user: User, section: Section) -> int:
    """Switched off; its tasks stay, without a section. Returns how many were in it."""
    if section.kind == Section.Kind.WORK:
        _md_only_sections(user)
    elif section.owner_id != user.id:
        raise ServiceError("forbidden", "that is not your section.", 403)
    n = SharedTask.objects.filter(section=section).update(section=None) + PrivateTask.objects.filter(section=section).update(section=None)
    section.active = False
    section.save(update_fields=["active"])
    services.audit(user, "section.delete", "section", section.id, f"{section.name} ({n})")
    return n


def section_item(s: Section, user: User | None = None) -> dict:
    open_n = SharedTask.objects.filter(section=s).exclude(status__in=[Status.COMPLETED, Status.CANCELLED])
    if user is not None and s.kind == Section.Kind.WORK:
        open_n = services.visible_shared_qs(user).filter(section=s).exclude(status__in=[Status.COMPLETED, Status.CANCELLED])
    if s.kind == Section.Kind.PERSONAL:
        count = PrivateTask.objects.filter(section=s, owner=user).exclude(status__in=[Status.COMPLETED, Status.CANCELLED]).count() if user else 0
    else:
        count = open_n.count()
    return {"id": s.id, "name": s.name, "name_hi": s.name_hi, "kind": s.kind, "colour": s.colour, "active": s.active,
            "department": s.department.name if s.department_id and s.department.active else "",
            "open": count, "aliases": list(s.aliases or [])[:12]}


# --------------------------------------------------------------------------- filing
def _score(section: Section, tokens: list[str], text: str) -> int:
    hits = 0
    for key in section_keys(section):
        if " " in key:
            if key in text:
                hits += 2
        elif key in tokens:
            hits += 1
    return hits


def match_section(text: str, *, person: User | None = None, giver: User | None = None, user: User | None = None,
                  kind: str = Section.Kind.WORK) -> Section | None:
    """The section a task's words point at (the most hits on a section's name and words), else the department of the
    person it is for, else the giver's. Personal items are matched against the owner's own sections only."""
    text_n = norm(text)
    tokens = _TOKEN.findall(text_n)
    rows = sections(user, kind)
    best, best_hits = None, 0
    for s in rows:
        hits = _score(s, tokens, text_n)
        if hits > best_hits:
            best, best_hits = s, hits
    if best is not None:
        return best
    if kind != Section.Kind.WORK:
        return None
    for who in (person, giver):
        if who is None or who.is_md or who.is_pa or who.is_mg:
            continue
        dept = member_of(who)
        if len(dept) == 1:
            return department_section(dept[0])
    return None


def file_task(task: SharedTask, text: str, *, giver: User | None = None, person: User | None = None,
              said: str = "", by: User | None = None) -> Section | None:
    """File a work item. `said` = a section named outright (made if new, by `by` or the giver)."""
    section = None
    if said:
        found = find_section(said, kind=Section.Kind.WORK)
        section = found["section"] if found["status"] in ("known", "close") else None
        if section is None:
            section, _made = create_section(by or giver or task.created_by, said)
    if section is None:
        section = match_section(text or task.title, person=person or task.assignee_user, giver=giver or task.created_by)
    if section is not None and task.section_id != section.id:
        task.section = section
        SharedTask.objects.filter(id=task.id).update(section=section)
        # Filing from the words is part of the "given" step (its line says the section); only a section named
        # outright is its own step in the history.
        if said:
            services.log_event(task, TaskEvent.Kind.FILED, by or giver or task.created_by, section=section.name)
    return section


def section_data(task: SharedTask) -> dict:
    """For the task's first history step: the section it was filed in, if any."""
    return {"section": task.section.name} if task.section_id else {}


def file_private(task: PrivateTask, text: str, owner: User, said: str = "") -> Section | None:
    section = None
    if said:
        found = find_section(said, owner, Section.Kind.PERSONAL)
        section = found["section"] if found["status"] in ("known", "close") else None
        if section is None:
            section, _made = create_section(owner, said, Section.Kind.PERSONAL)
    if section is None:
        section = match_section(text or task.title, user=owner, kind=Section.Kind.PERSONAL)
    if section is not None and task.section_id != section.id:
        task.section = section
        PrivateTask.objects.filter(id=task.id).update(section=section)
    return section


def learn(section: Section, title: str) -> None:
    """A task moved here by hand teaches the section its words (the plain ones, never names)."""
    names = {norm(u.spoken_name) for u in services.team()} | {norm(n) for u in services.team() for n in norm(u.spoken_name).split()}
    new = [w for w in words_of(title) if w not in names and w not in section_keys(section)]
    if not new:
        return
    section.aliases = (list(section.aliases or []) + new)[-MAX_ALIASES:]
    section.save(update_fields=["aliases"])


@transaction.atomic
def move_task(user: User, task_id, section_name: str) -> tuple[SharedTask, Section]:
    """"Put this in Partners": whoever gave it, whoever it is for, or the MD. A section named that does not exist is
    made. The section learns the task's words."""
    task = services.visible_shared_qs(user).select_for_update(of=("self",)).filter(id=task_id).first()
    if task is None:
        raise SharedTask.DoesNotExist("not visible")
    if not (user.is_md or task.created_by_id == user.id or task.assignee_user_id == user.id):
        raise ServiceError("forbidden", "only whoever gave it or does it can move it to another section.", 403)
    found = find_section(section_name, kind=Section.Kind.WORK)
    section = found["section"] if found["status"] in ("known", "close") else None
    if section is None:
        if found["status"] == "ambiguous":
            raise ServiceError("ambiguous", f"which section: {' or '.join(found['options'])}?", 422)
        section, _made = create_section(user, section_name)
    old = task.section
    task.section = section
    task.version += 1
    task.save(update_fields=["section", "version", "updated_at"])
    services.log_event(task, TaskEvent.Kind.FILED, user, section=section.name, **({"from": old.name} if old else {}))
    learn(section, task.title)
    services.audit(user, "task.section", "shared_task", task.id, section.name)
    return task, section


# --------------------------------------------------------------------------- the small AI reader for unsure filing
READ_SCHEMA = {
    "type": "object",
    "properties": {"section": {"type": "string", "description": "one of the given section names, exactly, or empty"}},
    "required": ["section"], "additionalProperties": False,
}


def read_section_ai(text: str, user=None) -> Section | None:
    """Only when the words matched no section and the sentence is long: one small question to the model, with the
    section names. Never raises; None when unsure or unavailable."""
    from django.conf import settings
    from . import ai_brain, ai_usage
    from .models import AiUsage
    import anthropic

    rows = sections(None, Section.Kind.WORK)
    if not rows or not ai_brain.ai_available() or len(words_of(text)) < 6:
        return None
    names = [s.name for s in rows]
    try:
        response = ai_brain._get_client().messages.create(
            model=settings.JARVIS_AI_COMMAND_MODEL, max_tokens=60,
            system="You file one office task into one of the given sections (folders of work) by its meaning. Answer with the "
                   "section name exactly as given, or empty when none clearly fits. Never invent a section.",
            messages=[{"role": "user", "content": f"<sections>{json.dumps(names, ensure_ascii=False)}</sections>\n<task>{text[:400]}</task>"}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": READ_SCHEMA}},
        )
        ai_usage.record(user, AiUsage.Kind.COMMAND, response)
        if response.stop_reason in ("refusal", "max_tokens"):
            return None
        name = json.loads(ai_brain._first_text(response)).get("section") or ""
    except (anthropic.APIError, json.JSONDecodeError, StopIteration) as exc:
        log.warning("section reader unavailable: %s", type(exc).__name__)
        return None
    return next((s for s in rows if s.name == name), None)


# --------------------------------------------------------------------------- answers: cards
def management_line(hindi: bool = False) -> str:
    people = management()
    if not people:
        return ""
    parts = [f"{u.spoken_name} ({'मैनेजर' if hindi and u.is_mg else 'PA' if u.is_pa else 'Manager'})" for u in people]
    return ("मैनेजमेंट: " if hindi else "Management: ") + ", ".join(parts)


def _open_work(user: User, members: list[User]) -> dict[int, list[SharedTask]]:
    qs = SharedTask.objects.filter(assignee_user__in=members, assignee_type=SharedTask.Assignee.USER) \
        .exclude(status__in=[Status.COMPLETED, Status.CANCELLED]).select_related("assignee_user", "section")
    if not user.is_md:
        qs = qs.filter(created_by=user)
    out: dict[int, list[SharedTask]] = {u.id: [] for u in members}
    for t in qs:
        out.setdefault(t.assignee_user_id, []).append(t)
    return out


def department_card(user: User, dept: Department) -> dict:
    """"Accounts department dikhao": who is in it, what each has open and late, its sections."""
    from . import datacards
    members = sorted([u for u in dept.members.all() if u.is_active], key=lambda u: u.spoken_name.lower())
    work = _open_work(user, members)
    rows = []
    for u in members:
        mine = work.get(u.id, [])
        late = sum(1 for t in mine if services.is_overdue(t))
        rows.append({"id": f"user-{u.id}", "scope": "", "title": u.spoken_name, "who": u.title_label, "kind": u.title_label,
                     "status": f"{len(mine)} open" + (f", {late} late" if late else "") if mine else "Free", "when": "", "day": "",
                     "priority": "NORMAL", "progress": 0, "private": False, "overdue": late > 0, "version": None,
                     "hold_reason": "", "assigned": len(mine), "late": late, "on_leave": False})
    open_n = sum(len(v) for v in work.values())
    late_n = sum(1 for v in work.values() for t in v if services.is_overdue(t))
    secs = [s.name for s in dept.sections.filter(active=True)]
    view = datacards._table("department", f"{dept.name} department", rows,
                            [("title", "Name"), ("kind", "Role"), ("assigned", "Open"), ("late", "Late")],
                            subtitle=_join_words(f"{len(members)} {'person' if len(members) == 1 else 'people'}",
                                                 f"· section: {', '.join(secs)}" if secs else ""), layout="people")
    view["stats"] = [
        {"label": "People", "value": len(members), "tone": "accent", "icon": "people-outline"},
        {"label": "Open", "value": open_n, "tone": "info", "icon": "briefcase-outline"},
        {"label": "Late", "value": late_n, "tone": "danger" if late_n else "muted", "icon": "alert-circle-outline"},
        {"label": "Free", "value": sum(1 for u in members if not work.get(u.id)), "tone": "success", "icon": "checkmark-circle-outline"},
    ]
    view["series"] = [{"label": r["title"], "date": r["title"], "value": r["assigned"], "minutes": 0, "heavy": r["late"] > 0,
                       "off": False, "today": False} for r in rows]
    view["profile"] = {"name": dept.name, "department": "Department", "designation": f"{len(members)} people", "on_leave": False, "leave": ""}
    view["request"] = {"kind": "department", "group": dept.name}
    address, address_hi = services.address_of(user), services.address_hi_of(user)
    names = ", ".join(u.spoken_name for u in members[:4]) + (f" and {len(members) - 4} more" if len(members) > 4 else "")
    if not members:
        en, hi = f"{dept.name} has nobody in it yet.", f"{dept.name} में अभी कोई नहीं है।"
    else:
        en = f"{dept.name} has {len(members)} {'person' if len(members) == 1 else 'people'}: {names}; {open_n} open" + (f", {late_n} late." if late_n else ".")
        hi = f"{dept.name} में {len(members)} {'व्यक्ति' if len(members) == 1 else 'लोग'}: {names}; {open_n} काम बाकी" + (f", {late_n} लेट।" if late_n else "।")
    view["summary"], view["summary_hi"] = f"{address}, {en}", f"{address_hi}, {hi}"
    return view


def departments_grid(user: User) -> dict:
    """"Sab departments dikhao": every department with its size and open work; the management line on top."""
    from . import datacards
    rows = []
    total_people = 0
    for d in departments():
        members = [u for u in d.members.all() if u.is_active]
        work = _open_work(user, members)
        open_n = sum(len(v) for v in work.values())
        late_n = sum(1 for v in work.values() for t in v if services.is_overdue(t))
        total_people += len(members)
        rows.append({"id": f"dept-{d.id}", "scope": "", "title": d.name, "who": ", ".join(u.spoken_name for u in members) or "nobody yet",
                     "kind": f"{len(members)} {'person' if len(members) == 1 else 'people'}", "status": f"{open_n} open" + (f", {late_n} late" if late_n else ""),
                     "when": "", "day": "", "priority": "NORMAL", "progress": 0, "private": False, "overdue": late_n > 0,
                     "version": None, "hold_reason": "", "assigned": open_n, "late": late_n, "on_leave": False, "colour": d.colour})
    view = datacards._table("departments", "Departments", rows, [("title", "Department"), ("kind", "People"), ("who", "Members"), ("assigned", "Open"), ("late", "Late")],
                            subtitle=management_line() or "No manager or PA yet", layout="people")
    view["stats"] = [
        {"label": "Departments", "value": len(rows), "tone": "accent", "icon": "git-network-outline"},
        {"label": "People", "value": total_people, "tone": "info", "icon": "people-outline"},
        {"label": "Open", "value": sum(r["assigned"] for r in rows), "tone": "success", "icon": "briefcase-outline"},
        {"label": "Late", "value": sum(r["late"] for r in rows), "tone": "danger" if any(r["late"] for r in rows) else "muted", "icon": "alert-circle-outline"},
    ]
    view["series"] = [{"label": r["title"], "date": r["title"], "value": r["assigned"], "minutes": 0, "heavy": r["late"] > 0, "off": False, "today": False} for r in rows]
    view["request"] = {"kind": "departments"}
    address, address_hi = services.address_of(user), services.address_hi_of(user)
    if not rows:
        en, hi = "there are no departments yet. Say, for example, 'Farm department banao aur Kiran ko add kar do'.", "अभी कोई डिपार्टमेंट नहीं है। बोलिए, जैसे 'Farm department banao aur Kiran ko add kar do'।"
    else:
        names = ", ".join(f"{r['title']} ({r['kind']})" for r in rows)
        en = f"{len(rows)} {'department' if len(rows) == 1 else 'departments'}: {names}." + (f" {management_line()}." if management_line() else "")
        hi = f"{len(rows)} डिपार्टमेंट: {names}।" + (f" {management_line(True)}।" if management_line() else "")
    view["summary"], view["summary_hi"] = f"{address}, {en}", f"{address_hi}, {hi}"
    return view


def _join_words(*parts: str) -> str:
    return " ".join(p for p in parts if p)


def ai_lists(user: User) -> dict:
    """What the model is told: the departments with their members, and the section names."""
    return {
        "departments": [{"name": d.name, "members": [u.spoken_name for u in d.members.all() if u.is_active]} for d in departments()],
        "management": [f"{u.spoken_name} ({'Manager' if u.is_mg else 'PA'})" for u in management()],
        "sections": [s.name for s in sections(None, Section.Kind.WORK)],
        "my_personal_sections": [s.name for s in sections(user, Section.Kind.PERSONAL)],
    }
