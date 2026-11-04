export type Role = 'MD' | 'PA';
export type Priority = 'URGENT' | 'HIGH' | 'NORMAL' | 'LOW';
export type Kind = 'TASK' | 'MEETING' | 'CALL' | 'PAYMENT' | 'FOLLOW_UP';
export type Status = 'OPEN' | 'IN_PROGRESS' | 'BLOCKED' | 'COMPLETED' | 'CANCELLED';

export interface SessionUser {
  username: string;
  role: Role;
  name: string;
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
  assignee: { type: 'MD' | 'PA' | 'STAFF'; staff_id?: number | null; staff_name?: string | null; department?: string | null };
  origin?: string;
  created_by?: string | null;
  description?: string;
  category_key?: string;
  sync_state?: 'NOT_REQUIRED' | 'PENDING' | 'SYNCED' | 'FAILED';
  sync_error?: string;
  remote_task_id?: string; // TaskPanel id, e.g. T-123
  remote_status?: string; // todo | working | waiting | done
  status_label?: string; // Not started | Working | Hold | Completed | Pending confirmation | Not assigned
  hold_reason?: string;
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

export interface Dashboard {
  user: SessionUser;
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
  pa_tasks?: TaskItem[]; // MD view: tasks he gave the PA, with progress
  md_tasks?: TaskItem[]; // PA view: MD tasks that came from her requests
}

export interface ServerAlert {
  id: string;
  kind: 'NEW_REQUEST' | 'REQUEST_DECIDED' | 'STAFF_UPDATE' | 'INFO';
  priority: Priority;
  title: string;
  body: string;
  spoken_text: string;
  ref_type: string;
  ref_id: string;
  created_at: string;
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
  spoken: string;
  ref: { scope: 'private' | 'shared'; id: string; version: number };
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
  | 'pa_task'
  | 'pa_meeting'
  | 'choose_destination'
  | 'accept_request'
  | 'decline_request'
  | 'complete'
  | 'briefing'
  | 'ask'
  | 'remember'
  | 'end'
  | 'answer';

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
  due_label: string;
  duration_minutes: number | null;
  remind_before_minutes: number | null;
  announcement: string;
  staff: StaffOption | null;
  staff_candidates: StaffOption[];
  category_key: string;
  categories: CategoryOption[];
  warnings: string[];
  pa_name: string;
  team: { label: string; members: StaffOption[]; skipped: StaffOption[] } | null;
  target: { id: string; title: string; scope?: string; version?: number } | null;
  target_candidates: { id: string; title: string; scope?: string; version?: number }[];
  needs_clarification: boolean;
  question: string;
  spoken_reply: string;
  ask_text: string;
  memory_text: string;
  reply_language: 'en' | 'hi';
  speak_text: string;
  speak_lang: 'en' | 'hi';
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

export interface AiUsageSummary {
  month: string;
  requests: number;
  web_searches: number;
  questions_today: number;
  daily_question_limit: number;
}
