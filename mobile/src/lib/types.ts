/** MD; the PA; the manager; TM = every other team member (accountant, farm manager, director ...). */
export type Role = 'MD' | 'PA' | 'MG' | 'TM';
export type Priority = 'URGENT' | 'HIGH' | 'NORMAL' | 'LOW';
export type Kind = 'TASK' | 'MEETING' | 'CALL' | 'PAYMENT' | 'FOLLOW_UP';
export type Status = 'OPEN' | 'IN_PROGRESS' | 'BLOCKED' | 'COMPLETED' | 'CANCELLED';

export interface SessionUser {
  username: string;
  role: Role;
  name: string;
  /** The person's job as said aloud ("Accountant", "PA", "Managing Director") and their department. */
  title?: string;
  department?: string;
  /** How JARVIS addresses this person ("Sir", "Ma'am", a name), and the same in Devanagari. Set on the server. */
  address?: string;
  address_hi?: string;
  /** The language JARVIS is speaking to this person right now ('hi' / 'en'), from the server's language rule. */
  language?: 'hi' | 'en';
}

export interface TaskItem {
  id: string;
  scope: 'private' | 'shared';
  title: string;
  notes: string;
  kind: Kind;
  priority: Priority;
  status: Status;
  due_kind: 'NONE' | 'DATE' | 'TIMED';
  due_date: string | null;
  due_at: string | null;
  original_due_at: string | null;
  original_due_date: string | null;
  scheduled_start: string | null;
  scheduled_end: string | null;
  estimated_minutes: number | null;
  due_label: string;
  overdue: boolean;
  version: number;
  /** Who it is for: the MD's own list (MD), a team member (USER: user_id, name in staff_name, job in department),
   *  or a TaskPanel staff member (STAFF). PA / MG are the lists from before the team. */
  assignee: {
    type: 'MD' | 'USER' | 'PA' | 'MG' | 'STAFF';
    staff_id?: number | null;
    user_id?: number | null;
    staff_name?: string | null;
    department?: string | null;
  };
  /** GIVEN (one person gave it to another), SELF (the person's own work), MEET (a meeting the giver set with them),
   *  PA_REQUEST (a request the MD accepted), or a TaskPanel delegation. */
  origin?: string;
  created_by?: string | null;
  created_by_id?: number | null;
  description?: string;
  category_key?: string;
  sync_state?: 'NOT_REQUIRED' | 'PENDING' | 'SYNCED' | 'FAILED';
  sync_error?: string;
  remote_task_id?: string; // TaskPanel id, e.g. T-123
  remote_status?: string; // todo | working | waiting | done
  status_label?: string; // Not started | Working | Hold | Completed | Pending confirmation | Not assigned
  hold_reason?: string;
  /** Ring until seen: when the person it is for saw it, and by when they said they would do it. */
  seen_at?: string | null;
  promise_label?: string;
  promise_label_hi?: string;
  /** The section it is filed in (GST, Accounts…), empty when not filed. */
  section?: string;
  section_id?: number | null;
  section_colour?: string;
}

/** A department: a group of people on the team (never the MD, the manager or the PA). */
export interface DepartmentItem {
  id: number;
  name: string;
  name_hi: string;
  colour: string;
  active: boolean;
  members: TeamMember[];
  open: number;
  late: number;
  section: string;
}

/** A section: a shared folder of work, or one person's own personal folder. */
export interface SectionItem {
  id: number;
  name: string;
  name_hi: string;
  kind: 'WORK' | 'PERSONAL';
  colour: string;
  active: boolean;
  department: string;
  open: number;
  aliases: string[];
}

export interface RequestItem {
  id: string;
  kind: 'CREATE' | 'CHANGE';
  state: 'PENDING' | 'ACCEPTED' | 'DECLINED' | 'WITHDRAWN' | 'CONFLICT';
  title: string;
  priority: Priority;
  due_label: string;
  reason: string;
  target_title: string | null;
  requested_by: string;
  decision_note: string;
  created_at: string;
  payload: Record<string, unknown>;
}

export interface BusyWindow {
  start: string;
  end: string;
  label: string;
}

/** One person on the team (names and jobs only; everyone may see who is on the team). */
export interface TeamMember {
  id: number;
  name: string;
  username: string;
  role: Role;
  title: string;
  department: string;
  gender: '' | 'male' | 'female';
  active: boolean;
  you?: boolean;
  /** The departments this person is in (team members only). */
  departments?: string[];
}

export interface Dashboard {
  user: SessionUser;
  /** The two names as JARVIS says them (set on the server), so no screen carries a typed-in name. */
  pa_name?: string;
  md_name?: string;
  /** How the MD is addressed and referred to ("Sir", "Ma'am", a name), and the same in Devanagari. */
  md_address?: string;
  md_address_hi?: string;
  server_time: string;
  date_label: string;
  overdue: TaskItem[];
  today: TaskItem[];
  upcoming: TaskItem[];
  anytime: TaskItem[];
  completed_today: number;
  requests: RequestItem[];
  delegated: TaskItem[];
  taskpanel: { mode: string; configured: boolean; label: string; linked: boolean };
  md_busy_today?: BusyWindow[];
  pa_tasks?: TaskItem[]; // MD view: the PA's list (kept for older screens)
  mg_tasks?: TaskItem[]; // MD view: the manager's list (kept for older screens)
  given_tasks?: TaskItem[]; // everyone: what they gave others on the team, with progress
  team_tasks?: TaskItem[]; // MD view: everybody else's work (he sees all work)
  manager_name?: string;
  md_tasks?: TaskItem[]; // the MD's official work: all of it (PA, manager) or what came from their own requests
  /** Everyone on the team (names and jobs). */
  team?: TeamMember[];
}

export interface ServerAlert {
  id: string;
  kind: 'NEW_REQUEST' | 'TASK_ASSIGNED' | 'REQUEST_DECIDED' | 'STAFF_UPDATE' | 'INFO';
  priority: Priority;
  title: string;
  body: string;
  /** Already in the recipient's language (the server picks); the English and Hindi twins come along too. */
  spoken_text: string;
  spoken_text_en?: string;
  spoken_text_hi?: string;
  ref_type: string;
  ref_id: string;
  created_at: string;
  /** Spoken or shown once already (by the app or by the phone's background part): never announced again. */
  delivered_at: string | null;
  /** Not told yet and recent: worth announcing. Otherwise it is only for the list. */
  fresh: boolean;
  /** "Later": out of sight until then (the server hides it meanwhile). */
  snoozed_until?: string | null;
  /** The key it is announced under: a snoozed alert that comes back is a new announcement, not a repeat. */
  announce_key?: string;
  question?: AlertQuestion | null;
  /** Ring until seen: it waits for a Seen (the banner shows the Seen button). */
  ring?: boolean;
  /** It rings again every few minutes right now. */
  ringing?: boolean;
  rings?: number;
  seen_at?: string | null;
  /** "Seen" / "देख लिया": the button's word in the person's language. */
  seen_label?: string;
  /** Who it is from: the name ("Mansi") and the word a line uses ("Sir" for the MD). */
  from_name?: string;
  from_word?: string;
}

export interface Reminder {
  key: string;
  label: string;
  fire_at: string;
  fire_at_label: string;
  priority: Priority;
  title: string;
  kind: Kind;
  is_private: boolean;
  /** The line to say, already in the reader's language (the server picks); the two twins come along too. */
  spoken: string;
  spoken_en?: string;
  spoken_hi?: string;
  lang?: 'hi' | 'en';
  ref: { scope: 'private' | 'shared'; id: string; version: number };
  /** Spoken or shown once already: never announced again. */
  told_at: string | null;
  /** The one question JARVIS asks right after it, and what a yes does (a contract from the server). */
  question?: AlertQuestion | null;
  /** Set when this is a snoozed reminder coming back (the key it had before). */
  snoozed_from?: string;
}

/** The one question JARVIS asks aloud after a reminder or an alert, and what a yes does. Written by the server. */
export interface AlertQuestion {
  /** promise / unseen / reassign take free words ("kal shaam tak", "Sunil ko de do"): the server reads the answer
   *  (POST /alerts/<id>/answer/). The others are a yes or a no, carried out by the app. */
  kind: 'accept' | 'done' | 'seen' | 'move' | 'promise' | 'unseen' | 'reassign';
  /** Appended to the spoken line; empty when the line itself already asks (a check-in). */
  en: string;
  hi: string;
  /** move: the item a yes moves, and the day it goes to. */
  target?: { id: string; scope: 'private' | 'shared'; title: string; version?: number };
  to?: string;
  /** unseen / reassign: the work it is about. */
  task?: string;
}

/** The server's reading of free words said after a promise / unseen / reassign question. */
export interface AlertAnswer {
  /** false: the words were about something else; treat them as a new sentence. */
  handled: boolean;
  show?: string;
  say?: string;
  lang?: 'hi' | 'en';
  /** A follow-up still waiting ("By when?", "Who should get it?"), or null when it is settled. */
  ask?: AlertQuestion | null;
}

/** One step in a work item's history. */
export interface HistoryStep {
  kind: string;
  at: string;
  at_label: string;
  at_label_hi: string;
  text: string;
  text_hi: string;
}

export interface StaffOption {
  id: number;
  name: string;
  department: string;
  on_leave?: boolean;
  leave_until?: string | null;
}

export interface CategoryOption {
  key: string;
  name: string;
  department: string;
}

export type DraftAction =
  | 'private_task'
  | 'md_request'
  | 'delegation'
  | 'team_delegation'
  | 'organise'
  | 'pa_task'
  | 'pa_meeting'
  | 'pa_self_task'
  | 'mg_task'
  | 'person_task'
  | 'person_meeting'
  | 'own_task'
  | 'message'
  | 'screen'
  | 'choose_destination'
  | 'accept_request'
  | 'decline_request'
  | 'complete'
  | 'change_task'
  | 'delete_task'
  | 'set_setting'
  | 'briefing'
  | 'ask'
  | 'remember'
  | 'confirm_pending'
  | 'cancel_pending'
  | 'snooze'
  | 'dismiss'
  | 'end'
  | 'answer';

/** One row of a data view (a task, a request, a busy span). */
export interface ViewRow {
  id: string;
  scope: string; // 'private' | 'shared' | 'request' | ''
  title: string;
  kind: string;
  priority: Priority | string;
  status: string;
  when: string;
  day: string;
  who: string;
  progress: number;
  private: boolean;
  overdue: boolean;
  version: number | null;
  hold_reason: string;
  reason?: string;
  target?: string;
  /** Performance rows (one person each): work given, finished, late, and the on-time share of finished work. */
  assigned?: number;
  done?: number;
  late?: number;
  on_time_pct?: number;
  /** People rows (the staff directory): away on leave right now. `assigned` and `late` then count open work. */
  on_leave?: boolean;
}

export interface ViewTimelineItem {
  id: string;
  scope: string;
  title: string;
  kind: string;
  priority: string;
  start: string;
  end: string;
  start_min: number;
  end_min: number;
  private: boolean;
  routine: boolean;
  overdue: boolean;
  version: number | null;
}

/** What the server sends for the screen when JARVIS answers with data. The phone only draws it. */
export interface DataView {
  kind: string;
  layout: 'timeline' | 'bars' | 'stats' | 'table' | 'progress' | 'requests' | 'busy' | 'people';
  title: string;
  subtitle: string;
  columns: { key: string; label: string }[];
  rows: ViewRow[];
  stats: { label: string; value: number | string; tone: string; icon?: string }[];
  timeline: ViewTimelineItem[];
  series: { label: string; date: string; value: number; minutes: number; heavy: boolean; off: boolean; today: boolean }[];
  office?: { start_min: number; end_min: number };
  /** One person's card: who they are, from the staff app. */
  profile?: { name: string; department: string; designation: string; on_leave: boolean; leave: string };
  /** A section's work: the section it shows. */
  section?: { id: number; name: string; colour: string };
  /** The sentence JARVIS spoke for this card (and its Hindi twin), written by the server from the real numbers. */
  summary?: string;
  summary_hi?: string;
  /** The request this card answers (kind, whose, period, filters): sent back with the card so a follow-up
   *  ("sirf meetings", "sab dikhao") starts from the same list. */
  request?: Record<string, unknown>;
  file_name: string;
  text: string;
}

/** What JARVIS found when it checked the diary for a draft: a clash, a free slot, a very full day. */
export interface ScheduleAdvice {
  /** His own meeting over another one: the card cannot be saved until he picks a way out. */
  hard?: boolean;
  for: 'self' | 'person' | 'pa' | 'md';
  asked: number;
  question: string;
  question_hi: string;
  notes: string[];
  clashes: { title: string; start: string; end: string; label: string }[];
  suggested: { due_date: string; due_time: string; label: string } | null;
  load: { count: number; heavy: boolean } | null;
  off_day: boolean;
}

export interface Draft {
  engine: 'ai' | 'rules';
  intent: string;
  transcript: string;
  action: DraftAction;
  title: string;
  notes: string;
  kind: Kind;
  priority: Priority;
  due_date: string;
  due_time: string;
  /** "In 5 minutes": the exact moment, seconds included (empty otherwise). */
  due_at?: string;
  due_in_minutes?: number;
  due_label: string;
  duration_minutes: number | null;
  remind_before_minutes: number | null;
  announcement: string;
  /** snooze: when the reminder or alert JARVIS just spoke comes back. */
  snooze_until?: string;
  /** More than one thing in one breath: every further item (its own kind, time, person, destination). One yes saves all. */
  batch?: Draft[];
  staff: StaffOption | null;
  staff_candidates: StaffOption[];
  category_key: string;
  categories: CategoryOption[];
  warnings: string[];
  /** Who it is for, as the read-back names them ("Sunil", "Sunil and Mansi"). */
  pa_name: string;
  /** mg_task: the manager's name as JARVIS says it. */
  mg_name?: string;
  /** person_task / person_meeting / message: the people on the team it is for (looked up again on the server). */
  assignees?: { id: number; name: string; title: string }[];
  /** A group said by name ("the Accounts team", "everyone"). */
  group_label?: string;
  /** message: its own words, kept whole. */
  message?: string;
  team: { label: string; members: StaffOption[]; skipped: StaffOption[] } | null;
  target: { id: string; title: string; scope?: string; version?: number } | null;
  /** Every item this applies to: "mark both as done", "accept all three". */
  targets: { id: string; title: string; scope?: string; version?: number }[];
  target_candidates: { id: string; title: string; scope?: string; version?: number }[];
  /** change_task: the new values (title / due_date / due_time / priority / kind). */
  changes: Record<string, string>;
  /** People named in it (the server checks it knows each one before anything is saved). */
  people?: string[];
  /** People the speaker just explained; now remembered as contacts. */
  contacts_saved?: string[];
  /** Diary check for timed drafts (clash / free slot / full day). */
  schedule?: ScheduleAdvice | null;
  /** confirm_pending: the day / time to apply to the waiting card before saving ("haan" to the offered slot, or "6 baje"). */
  overrides?: { due_date?: string; due_time?: string; duration_minutes?: number; batch?: Draft[] };
  /** The server already saved it (a clear instruction done at once, a meeting booked, a plain reminder): the lists
   *  refresh now. */
  booked?: boolean;
  /** What was just saved can be taken back for a minute (the Undo button, or "undo" said aloud). */
  undo?: boolean;
  reminder_saved?: boolean;
  /** The server saved what the waiting card held (and more): the card closes. */
  card_done?: boolean;
  /** Data the phone draws for this answer (timeline / chart / table). */
  view?: DataView | null;
  /** Something to do on the screen by voice: export_pdf / export_csv / share / open_item / open_settings / go_home. */
  screen_action?: string;
  /** organise: a department or section change waiting for a yes (removing people, deleting, merging); sent back as is. */
  organise?: Record<string, unknown>;
  setting_key: string;
  setting_value: string;
  setting_patch?: Record<string, string | number | boolean>;
  needs_clarification: boolean;
  question: string;
  spoken_reply: string;
  ask_text: string;
  memory_text: string;
  reply_language: 'en' | 'hi';
  speak_text: string;
  speak_lang: 'en' | 'hi';
  /** What JARVIS says once this is saved (written by the AI in the speaker's own style). */
  done_reply: string;
  done_speak: string;
  done_lang: 'en' | 'hi';
}

export interface AskAnswer {
  text: string;
  speak_text: string;
  speak_lang: 'en' | 'hi';
  sources: { title: string; url: string }[];
  engine: string;
}

export interface MemoryItem {
  id: string;
  text: string;
  created_at: string;
}

/** One thing JARVIS has learned (or been told) about how this person works, in plain words. */
export interface HabitItem {
  key: string;
  label: string;
  value: string;
  learned: string;
  /** Something is stored for it (learned or set); false = still the default. */
  set: boolean;
}

/** A person JARVIS knows for this user who is not on the staff list (learned when they say who someone is). */
export interface ContactItem {
  id: number;
  name: string;
  note: string;
  created_at: string;
}

export interface AiUsageSummary {
  month: string;
  requests: number;
  web_searches: number;
  questions_today: number;
  daily_question_limit: number;
}
