"""JARVIS data model.

Privacy boundary: ``PrivateTask`` is a separate, encrypted table that only the MD
can reach. PA-facing code paths never return its content; the only PA-visible
derivative is anonymous busy time (start/end, no title).
"""
import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Q

from .crypto import EncryptedTextField


class User(AbstractUser):
    class Role(models.TextChoices):
        MD = "MD", "Managing Director"
        PA = "PA", "Personal Assistant"
        MG = "MG", "Manager"
        TM = "TM", "Team member"  # everyone else on the team (accounts, farm manager, director, ...)

    role = models.CharField(max_length=2, choices=Role.choices, default=Role.PA)
    display_name = models.CharField(max_length=80, blank=True)
    # What the person does ("Accountant", "Farm Manager", "Director"): JARVIS finds people by it ("accountant ko
    # bolo") and says it. The department groups people for messages and work to a team ("accounts team ko bolo").
    job_title = models.CharField(max_length=80, blank=True, default="")
    department = models.CharField(max_length=80, blank=True, default="")
    # "male" / "female" / "" (not said): set by the MD on the Team page, never guessed from a name. Only used for
    # "he / she" and the Hindi verb forms when JARVIS speaks ABOUT the person.
    gender = models.CharField(max_length=6, blank=True, default="")

    class Meta:
        constraints = [
            # This is a private, single-executive app: exactly one MD account can exist.
            models.UniqueConstraint(fields=["role"], condition=Q(role="MD"), name="single_md_account"),
        ]

    @property
    def is_md(self) -> bool:
        return self.role == self.Role.MD

    @property
    def is_pa(self) -> bool:
        return self.role == self.Role.PA

    @property
    def is_mg(self) -> bool:
        return self.role == self.Role.MG

    @property
    def is_assistant(self) -> bool:
        """Everyone who is not the MD: the PA, the manager and every other team member. None of them ever sees the
        MD's personal items (or anybody else's)."""
        return self.role != self.Role.MD

    @property
    def sees_md_diary(self) -> bool:
        """The PA and the manager run the MD's diary: they see his OFFICIAL items (never his personal ones) and when
        he is busy. Other team members see only what they gave him or asked him for."""
        return self.role in (self.Role.PA, self.Role.MG)

    @property
    def title_label(self) -> str:
        """The person's job as said aloud: their job title, else the role ("PA", "Manager")."""
        if self.job_title:
            return self.job_title
        return {"MD": "Managing Director", "PA": "PA", "MG": "Manager"}.get(self.role, "Team member")

    @property
    def spoken_name(self) -> str:
        return self.display_name or self.first_name or self.username


class Department(models.Model):
    """A group of people on the team (Accounts, Farm, Social Media…), made and changed by the MD, by voice or on the
    Departments page. Only team members belong to one; the MD, the manager and the PA never do (they work across all
    of them). `User.department` (text) mirrors a person's first department for older screens."""

    name = models.CharField(max_length=80, unique=True)
    name_hi = models.CharField(max_length=80, blank=True, default="")
    aliases = models.JSONField(default=list, blank=True)  # other ways people say it ("accounts team", "hisaab")
    colour = models.CharField(max_length=7, blank=True, default="")
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="departments", blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Section(models.Model):
    """A folder for work (GST, Accounts, Franchise…) shared across the company, or a person's own personal folder
    that nobody else sees. JARVIS files every task into one from the words, and learns each section's words."""

    class Kind(models.TextChoices):
        WORK = "WORK", "Work (shared)"
        PERSONAL = "PERSONAL", "Personal (one person's own)"

    name = models.CharField(max_length=80)
    name_hi = models.CharField(max_length=80, blank=True, default="")
    aliases = models.JSONField(default=list, blank=True)  # words that file a task here, learned and given
    colour = models.CharField(max_length=7, blank=True, default="")
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.WORK)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="personal_sections")
    # The department this section belongs with (Accounts ↔ Accounts): its members' own work files here by default.
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.SET_NULL, related_name="sections")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["name", "kind", "owner"], name="unique_section_name")]

    def __str__(self):
        return self.name


class Priority(models.TextChoices):
    URGENT = "URGENT", "Urgent"
    HIGH = "HIGH", "High"
    NORMAL = "NORMAL", "Normal"
    LOW = "LOW", "Low"


PRIORITY_RANK = {"URGENT": 0, "HIGH": 1, "NORMAL": 2, "LOW": 3}


class Status(models.TextChoices):
    OPEN = "OPEN", "Open"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    BLOCKED = "BLOCKED", "Blocked"
    COMPLETED = "COMPLETED", "Completed"
    CANCELLED = "CANCELLED", "Cancelled"


CLOSED_STATUSES = {Status.COMPLETED, Status.CANCELLED}


class Kind(models.TextChoices):
    TASK = "TASK", "Task"
    MEETING = "MEETING", "Meeting"
    CALL = "CALL", "Call"
    PAYMENT = "PAYMENT", "Payment"
    FOLLOW_UP = "FOLLOW_UP", "Follow-up"


class DueKind(models.TextChoices):
    NONE = "NONE", "No deadline"
    DATE = "DATE", "Date only"
    TIMED = "TIMED", "Date and time"


class TaskFields(models.Model):
    """Fields shared by private and shared tasks. Deadline and planned slot stay separate."""

    priority = models.CharField(max_length=6, choices=Priority.choices, default=Priority.NORMAL)
    status = models.CharField(max_length=11, choices=Status.choices, default=Status.OPEN)
    kind = models.CharField(max_length=9, choices=Kind.choices, default=Kind.TASK)

    due_kind = models.CharField(max_length=5, choices=DueKind.choices, default=DueKind.NONE)
    due_date = models.DateField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    original_due_date = models.DateField(null=True, blank=True)
    original_due_at = models.DateTimeField(null=True, blank=True)

    scheduled_start = models.DateTimeField(null=True, blank=True)
    scheduled_end = models.DateTimeField(null=True, blank=True)
    estimated_minutes = models.PositiveIntegerField(null=True, blank=True)
    remind_before_minutes = models.PositiveIntegerField(null=True, blank=True)

    version = models.PositiveIntegerField(default=1)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    @property
    def is_closed(self) -> bool:
        return self.status in CLOSED_STATUSES


class PrivateTask(TaskFields):
    """MD's personal tasks. Encrypted at rest. MD-only. Never sent to Laravel."""

    class Source(models.TextChoices):
        MANUAL = "MANUAL", "Manual"
        VOICE = "VOICE", "Voice"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="private_tasks")
    title = EncryptedTextField()
    notes = EncryptedTextField(blank=True, default="")
    announcement = EncryptedTextField(blank=True, default="")
    source = models.CharField(max_length=6, choices=Source.choices, default=Source.MANUAL)
    # One of the owner's own personal sections (never a work section): the folder it is filed in, if any.
    section = models.ForeignKey(Section, null=True, blank=True, on_delete=models.SET_NULL, related_name="private_tasks")

    class Meta:
        indexes = [models.Index(fields=["owner", "status"]), models.Index(fields=["due_at"])]


class StaffMember(models.Model):
    """Cached TaskPanel directory entry (minimum allowed fields). external_id is the immutable TaskPanel staff key."""

    external_id = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=120)
    department = models.CharField(max_length=120, blank=True)
    designation = models.CharField(max_length=120, blank=True)
    active = models.BooleanField(default=True)
    on_leave = models.BooleanField(default=False)
    leave_until = models.DateField(null=True, blank=True)
    source = models.CharField(max_length=10, default="taskpanel")
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.department})" if self.department else self.name


class TaskCategory(models.Model):
    """TaskPanel task categories from the directory. TaskPanel enforces the category/department rules."""

    key = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=120)
    department = models.CharField(max_length=120, blank=True)
    active = models.BooleanField(default=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class SharedTask(TaskFields):
    """Work items (never personal ones): the MD's official tasks, work one team member gives another, a team
    member's own work, meetings between two people, and work delegated to TaskPanel staff.

    Who it is for: assignee_user (any JARVIS account) with assignee_type MD (the MD's own official list) or USER
    (anyone else), or a TaskPanel staff member (assignee_type STAFF). Who gave it: created_by. Visibility: the
    giver, the person it is for, and the MD (who sees all work); see services.visible_shared_qs."""

    class Assignee(models.TextChoices):
        MD = "MD", "MD"
        USER = "USER", "Team member"
        STAFF = "STAFF", "Staff"
        # Before the team (Nov 2026): the PA's and the manager's lists. Migration 0018 turned them into USER.
        PA = "PA", "PA"
        MG = "MG", "Manager"

    class Origin(models.TextChoices):
        PA_REQUEST = "PA_REQUEST", "Requested of the MD (accepted)"
        GIVEN = "GIVEN", "Given by one person to another"
        SELF = "SELF", "The person's own work"
        MEET = "MEET", "Meeting between the giver and this person"
        MD_DELEGATION = "MD_DELEGATION", "Delegated by MD"
        PA_DELEGATION = "PA_DELEGATION", "Delegated by PA"
        TASKPANEL_WEB = "TASKPANEL_WEB", "Assigned in TaskPanel"
        # Before the team (Nov 2026); migration 0018 turned these into GIVEN / SELF / MEET.
        MD_TO_PA = "MD_TO_PA", "Assigned by MD to PA"
        MD_MEET_PA = "MD_MEET_PA", "Meeting between MD and PA"
        PA_SELF = "PA_SELF", "PA's own task"
        MG_SELF = "MG_SELF", "Manager's own task"
        MD_TO_MG = "MD_TO_MG", "Assigned by MD to the manager"
        MG_TO_PA = "MG_TO_PA", "Assigned by the manager to the PA"
        PA_TO_MG = "PA_TO_MG", "Assigned by the PA to the manager"

    class Sync(models.TextChoices):
        NOT_REQUIRED = "NOT_REQUIRED", "Not required"
        PENDING = "PENDING", "Pending confirmation"
        SYNCED = "SYNCED", "Confirmed by TaskPanel"
        FAILED = "FAILED", "Not assigned"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=200)
    notes = models.TextField(blank=True, default="")
    description = models.TextField(blank=True, default="")  # full spoken instruction, kept as-is for TaskPanel
    announcement = models.TextField(blank=True, default="")
    assignee_type = models.CharField(max_length=5, choices=Assignee.choices)
    # The JARVIS account this is for (assignee_type MD or USER); empty for TaskPanel staff work.
    assignee_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="assigned_tasks")
    staff = models.ForeignKey(StaffMember, null=True, blank=True, on_delete=models.PROTECT, related_name="tasks")
    category_key = models.CharField(max_length=64, blank=True, default="")
    origin = models.CharField(max_length=13, choices=Origin.choices)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="shared_created")
    # The work section it is filed in (GST, Accounts…), from the words or said outright; empty = not filed yet.
    section = models.ForeignKey(Section, null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks")

    # Projection of the canonical TaskPanel task (TaskPanel is the authority for staff tasks).
    remote_task_uuid = models.CharField(max_length=64, unique=True, null=True, blank=True)
    remote_legacy_id = models.CharField(max_length=32, blank=True, default="")  # e.g. T-123
    remote_status = models.CharField(max_length=20, blank=True, default="")  # todo/working/waiting/done
    remote_version = models.PositiveIntegerField(default=0)
    sync_revision = models.PositiveIntegerField(default=0)
    hold_reason = models.CharField(max_length=300, blank=True, default="")
    sync_state = models.CharField(max_length=12, choices=Sync.choices, default=Sync.NOT_REQUIRED)
    sync_error = models.CharField(max_length=300, blank=True, default="")
    last_synced_at = models.DateTimeField(null=True, blank=True)

    # Ring until seen (Nov 2026): when the person it is for saw it (tapped Seen, opened it, or answered JARVIS about
    # it), and by when they said they will do it ("kal shaam tak"). Both start again when it is given to someone else.
    seen_at = models.DateTimeField(null=True, blank=True)
    promise_date = models.DateField(null=True, blank=True)
    promise_at = models.DateTimeField(null=True, blank=True)  # set when a time was said, else only the day

    class Meta:
        indexes = [models.Index(fields=["assignee_type", "status"]), models.Index(fields=["due_at"]),
                   models.Index(fields=["assignee_user", "status"], name="core_shared_assigne_team_idx")]

    @property
    def promise_kind(self) -> str:
        return DueKind.TIMED if self.promise_at else DueKind.DATE if self.promise_date else DueKind.NONE


class TaskEvent(models.Model):
    """One step in a work item's life, recorded as it happens: who gave it, when it was seen, the date promised,
    when it moved, who it went to instead, when it was done. Read back as the task's history (never edited)."""

    class Kind(models.TextChoices):
        GIVEN = "GIVEN", "Given"
        ADDED = "ADDED", "Added"
        REQUESTED = "REQUESTED", "Requested and accepted"
        DELEGATED = "DELEGATED", "Given to staff"
        SEEN = "SEEN", "Seen"
        PROMISED = "PROMISED", "Promised a time"
        CANNOT = "CANNOT", "Can't do it"
        NOT_SEEN = "NOT_SEEN", "Not seen while it rang"
        RING_AGAIN = "RING_AGAIN", "Rang again"
        REASSIGNED = "REASSIGNED", "Given to someone else"
        MOVED = "MOVED", "Moved"
        STARTED = "STARTED", "Started"
        HOLD = "HOLD", "Put on hold"
        DONE = "DONE", "Done"
        REOPENED = "REOPENED", "Reopened"
        CANCELLED = "CANCELLED", "Cancelled"
        FILED = "FILED", "Filed in a section"

    task = models.ForeignKey(SharedTask, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # What the step needs to be told again: {"to": user id, "from": user id, "label": "tomorrow at 6 PM",
    # "label_hi": ..., "reason": ..., "staff": name}. Work items only: never anything personal.
    data = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["at", "id"]
        indexes = [models.Index(fields=["task", "at"])]


class TaskPanelLink(models.Model):
    """Delegated TaskPanel identity for a JARVIS user. The grant is encrypted at rest and never sent to the phone."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="taskpanel_link")
    actor_key = models.CharField(max_length=64)  # TaskPanel user_key of this person
    actor_name = models.CharField(max_length=120, blank=True)
    grant = EncryptedTextField()
    scopes = models.CharField(max_length=200, default="directory.read tasks.read tasks.create")
    expires_at = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)
    linked_at = models.DateTimeField(auto_now_add=True)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes.split()


class IntegrationCommand(models.Model):
    """Durable command ledger. The id IS the TaskPanel command_id; retries always reuse it."""

    class State(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        SENDING = "SENDING", "Sending"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        REJECTED = "REJECTED", "Rejected"
        OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN", "Outcome unknown"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="integration_commands")
    type = models.CharField(max_length=30)
    task = models.ForeignKey(SharedTask, null=True, blank=True, on_delete=models.SET_NULL, related_name="commands")
    payload = models.JSONField()  # frozen at confirmation; never recomputed on retry
    payload_hash = models.CharField(max_length=64)
    state = models.CharField(max_length=15, choices=State.choices, default=State.QUEUED)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=60, blank=True, default="")
    error_message = models.CharField(max_length=300, blank=True, default="")
    result = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["state", "next_attempt_at"])]


class SyncCursor(models.Model):
    """Per-actor position in TaskPanel's published change feed."""

    link = models.OneToOneField(TaskPanelLink, on_delete=models.CASCADE, related_name="cursor")
    cursor = models.CharField(max_length=200, blank=True, default="")
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=200, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)


class EventInbox(models.Model):
    """Every applied TaskPanel event, so webhook + pull duplicates are applied once."""

    event_id = models.CharField(max_length=64, unique=True)
    event_type = models.CharField(max_length=40)
    task_uuid = models.CharField(max_length=64)
    sync_revision = models.PositiveIntegerField(default=0)
    received_at = models.DateTimeField(auto_now_add=True)


class TaskRequest(models.Model):
    """A PA proposal. Nothing changes for the MD until the MD accepts this exact payload."""

    class Kind(models.TextChoices):
        CREATE = "CREATE", "New task for MD"
        CHANGE = "CHANGE", "Change to MD task"

    class State(models.TextChoices):
        PENDING = "PENDING", "Awaiting MD"
        ACCEPTED = "ACCEPTED", "Accepted"
        DECLINED = "DECLINED", "Declined"
        WITHDRAWN = "WITHDRAWN", "Withdrawn"
        CONFLICT = "CONFLICT", "Conflict"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=6, choices=Kind.choices)
    target = models.ForeignKey(SharedTask, null=True, blank=True, on_delete=models.CASCADE, related_name="change_requests")
    payload = models.JSONField()
    payload_hash = models.CharField(max_length=64)
    base_version = models.PositiveIntegerField(null=True, blank=True)
    reason = models.CharField(max_length=300, blank=True, default="")
    state = models.CharField(max_length=9, choices=State.choices, default=State.PENDING)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="requests_made")
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="requests_decided"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=300, blank=True, default="")
    resulting_task = models.ForeignKey(SharedTask, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class Alert(models.Model):
    """Server-side alert feed (new requests, decisions, staff progress)."""

    class Kind(models.TextChoices):
        NEW_REQUEST = "NEW_REQUEST", "New request"
        TASK_ASSIGNED = "TASK_ASSIGNED", "Task assigned"
        REQUEST_DECIDED = "REQUEST_DECIDED", "Request decided"
        STAFF_UPDATE = "STAFF_UPDATE", "Staff update"
        INFO = "INFO", "Info"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="alerts")
    # Who it is from, when a person sent it (a message, a task given): they are told when it is seen.
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    priority = models.CharField(max_length=6, choices=Priority.choices, default=Priority.NORMAL)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True, default="")          # a briefing can be a full paragraph
    spoken_text = models.TextField(blank=True, default="")
    # The same line in Devanagari, spoken instead when the recipient has been speaking Hindi (empty = English only).
    spoken_text_hi = models.TextField(blank=True, default="")
    ref_type = models.CharField(max_length=20, blank=True, default="")
    ref_id = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    # Told = spoken or shown to the person once (by the app or by the phone's background part); after that it is
    # never announced again by anybody. Acknowledged = seen or acted on; after that it leaves the lists.
    delivered_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    # "Later": hidden until this moment, then announced afresh (kept here so it survives the phone restarting).
    snoozed_until = models.DateTimeField(null=True, blank=True)
    # The one question JARVIS asks after this alert and what a yes does ({"kind": "move", "en": ..., "hi": ...,
    # "target": {...}}); empty = the standard question for its kind, if any (see services.alert_question).
    question = models.JSONField(null=True, blank=True)
    # Ring until seen (Nov 2026): work given and messages sent by a person ring the recipient's phone every few
    # minutes until they tap Seen (or open it, or answer JARVIS about it); after a while the sender is told it was
    # not seen. next_ring_at set = ringing is on (or a stop still has to reach the phones); see core/ringing.py.
    ring = models.BooleanField(default=False)
    seen_at = models.DateTimeField(null=True, blank=True)
    rings = models.PositiveSmallIntegerField(default=0)
    ring_started_at = models.DateTimeField(null=True, blank=True)
    next_ring_at = models.DateTimeField(null=True, blank=True)
    unseen_told_at = models.DateTimeField(null=True, blank=True)  # the sender was told "not seen yet"

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["recipient", "acknowledged_at"]),
                   models.Index(fields=["next_ring_at"], name="core_alert_ring_idx")]


class PushDevice(models.Model):
    """A phone that receives live push (Firebase Cloud Messaging) for one user. The token moves with the login."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="push_devices")
    token = models.CharField(max_length=512, unique=True)
    platform = models.CharField(max_length=16, default="android")
    failures = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class ReminderAck(models.Model):
    """A reminder the person has seen or acted on: it leaves the lists for good."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    key = models.CharField(max_length=200)
    acknowledged_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "key"], name="unique_reminder_ack")]


class ReminderTold(models.Model):
    """A reminder that was spoken or shown once (by the app, or by the phone's background part while the app was
    closed). Whoever handles it next sees this and does not say it again; it still sits in the list until seen."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    key = models.CharField(max_length=200)
    told_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "key"], name="unique_reminder_told")]


class PhoneLog(models.Model):
    """What went wrong on a phone (errors, failed calls, crashes), sent by the app so a problem can be read here
    instead of guessed at. Short lines only; never task titles."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    level = models.CharField(max_length=8, default="error")
    where = models.CharField(max_length=80, blank=True, default="")
    message = models.CharField(max_length=500)
    at = models.DateTimeField()               # when it happened on the phone
    app_version = models.CharField(max_length=20, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-at"]


class ReminderSnooze(models.Model):
    """"Later" for one reminder: it comes back at this moment as a fresh announcement. Kept on the server, so a phone
    restart does not lose it and the app and the phone's background part honour the same time."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    key = models.CharField(max_length=200)
    until = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "key"], name="unique_reminder_snooze")]


class CommandReceipt(models.Model):
    """Idempotency: a retried request with the same key returns the original result."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    key = models.CharField(max_length=100)
    endpoint = models.CharField(max_length=100)
    request_hash = models.CharField(max_length=64)
    status_code = models.PositiveSmallIntegerField()
    response = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "key"], name="unique_command_key")]


class AuditLog(models.Model):
    """Who did what. Never stores private task titles or secrets."""

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=60)
    target_type = models.CharField(max_length=30)
    target_id = models.CharField(max_length=64)
    detail = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class Memory(models.Model):
    """Something the user explicitly asked JARVIS to remember ("I go to the gym at 7").

    Owner-only and encrypted: the MD's memories never reach the PA, and the PA's never reach the MD.
    Only explicit, user-confirmed facts are stored here — JARVIS never saves its own guesses.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memories")
    text = EncryptedTextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class Contact(models.Model):
    """A person JARVIS knows for this user who is not on the staff list (a client, a vendor, the CA...).

    Learned when the user says who someone is; owner-only and encrypted, like memories. It is what lets JARVIS
    say "I don't know any Aman" instead of quietly booking a meeting with nobody.
    """

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="contacts")
    name = EncryptedTextField()
    note = EncryptedTextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class UserPrefs(models.Model):
    """Settings shared by the app and the background listener. JARVIS can change them by voice."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="prefs")
    data = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)


class ConversationState(models.Model):
    """Short memory of the voice conversation for the phone's background listener: the card waiting for a
    yes/no, the options after "which one?", the missed reminders JARVIS offered to read.

    Encrypted at rest (it can hold a private task title) and forgotten after a few minutes of silence.
    """

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="conversation_state")
    state_json = EncryptedTextField(blank=True, default="{}")
    updated_at = models.DateTimeField(auto_now=True)


class AiUsage(models.Model):
    """Daily AI counters per user, so spending stays visible and capped."""

    class Kind(models.TextChoices):
        COMMAND = "COMMAND", "Understanding a command"
        ANSWER = "ANSWER", "Answering a question"
        BRIEFING = "BRIEFING", "Briefing"
        VOICE = "VOICE", "Spoken reply (Grok voice); input_tokens holds characters"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ai_usage")
    day = models.DateField()
    kind = models.CharField(max_length=10, choices=Kind.choices)
    requests = models.PositiveIntegerField(default=0)
    input_tokens = models.PositiveBigIntegerField(default=0)
    output_tokens = models.PositiveBigIntegerField(default=0)
    web_searches = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "day", "kind"], name="unique_ai_usage_day")]


class TalkSession(models.Model):
    """One walkie-talkie talk: who started it, who was in it, how many turns and how long. Never the sound itself
    (nothing is recorded, by design); this is what the Recent list and Sir's team view show."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    started_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="talks_started")
    members = models.JSONField(default=list)  # user ids, the starter included
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    turns = models.PositiveIntegerField(default=0)   # presses of the button, all speakers together
    seconds = models.PositiveIntegerField(default=0)  # time somebody was talking
    last_speaker = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-started_at"]


class WalkiePresence(models.Model):
    """Whether a person's phone holds its walkie-talkie line open right now, and until when they asked for quiet.
    Written by the relay (manage.py walkie_server); read by the API for Sir's team view."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="walkie")
    connected = models.BooleanField(default=False)
    last_seen = models.DateTimeField(null=True, blank=True)
    quiet_until = models.DateTimeField(null=True, blank=True)


class Channel(models.TextChoices):
    """Who gets a new app version: the test phones first, everyone after "promote"."""
    PREVIEW = "preview", "Test phones only"
    PRODUCTION = "production", "Everyone"


class AppUpdate(models.Model):
    """One published version of the app's code (over the air, no APK). The files live in the update store,
    named by their content hash; this row is the manifest the phones receive (signed when they ask)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)  # the manifest id
    runtime_version = models.CharField(max_length=40)  # the native build it runs on (the APK's version)
    platform = models.CharField(max_length=10, default="android")
    channel = models.CharField(max_length=12, choices=Channel.choices, default=Channel.PREVIEW)
    created_at = models.DateTimeField()  # the manifest's createdAt: phones take only newer ones
    launch_asset = models.JSONField()  # {file, hash, key, contentType}
    assets = models.JSONField(default=list)  # [{file, hash, key, contentType, fileExtension}]
    expo_client = models.JSONField(default=dict)  # the app's public config (Constants.expoConfig on the phone)
    fingerprint = models.CharField(max_length=64)  # of the code and files: an identical publish is skipped
    commit = models.CharField(max_length=40, blank=True, default="")
    message = models.CharField(max_length=200, blank=True, default="")
    withdrawn = models.BooleanField(default=False)  # rolled back: never served again
    republished_from = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    promoted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["runtime_version", "platform", "withdrawn", "-created_at"], name="core_appupd_serve_idx")]


class AppApk(models.Model):
    """A new APK (needed only when native parts change). Phones on an older version are offered it: one tap."""

    version = models.CharField(max_length=40)
    file = models.CharField(max_length=120)  # its name in the update store's apk folder
    size = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64)
    channel = models.CharField(max_length=12, choices=Channel.choices, default=Channel.PREVIEW)
    notes = models.CharField(max_length=200, blank=True, default="")
    withdrawn = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class AppDevice(models.Model):
    """A phone that asks for app updates. The id is random and made by the app; a test phone gets new versions
    before everyone else. Linked to whoever is signed in on it, so the list says whose phone is whose."""

    device_id = models.CharField(max_length=64, unique=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="app_devices")
    model = models.CharField(max_length=80, blank=True, default="")
    runtime_version = models.CharField(max_length=40, blank=True, default="")
    update_id = models.CharField(max_length=40, blank=True, default="")  # the app code it runs now ("" = the APK's own)
    app_version = models.CharField(max_length=40, blank=True, default="")
    tester = models.BooleanField(default=False)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_seen"]
