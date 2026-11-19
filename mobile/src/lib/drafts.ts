import { dayLabel } from './format';
import type { Draft, DraftAction } from './types';

export const TASK_DRAFTS: DraftAction[] = ['private_task', 'md_request', 'delegation', 'team_delegation', 'pa_task', 'pa_meeting',
  'pa_self_task', 'mg_task', 'person_task', 'person_meeting', 'own_task'];

/** Cards of the team's kind, saved on the server by the same path the voice uses (/assistant/execute/). Work for the
 *  MD (md_request) is one of them: it goes straight onto the MD's list there, like work for anyone. */
export const SERVER_SAVED: DraftAction[] = ['person_task', 'person_meeting', 'own_task', 'message', 'pa_task', 'pa_meeting',
  'pa_self_task', 'mg_task', 'md_request', 'organise'];

/** Cards that end with a question ("shall I save it?") and wait for a yes / no / change. */
export const CONFIRMABLE: DraftAction[] = [...TASK_DRAFTS, 'message', 'accept_request', 'decline_request', 'complete', 'remember',
  'change_task', 'delete_task', 'organise'];

const CHANGE_LABEL: Record<string, string> = {
  title: 'name', due_date: 'day', due_time: 'time', priority: 'priority', kind: 'type', duration_minutes: 'length',
};

/** "time → 15:30, length → 15 min". Values may be text or numbers (a meeting's length), never assumed to be text. */
export function describeChanges(changes: Record<string, string | number> | undefined): string {
  if (!changes || typeof changes !== 'object') return '';
  return Object.entries(changes)
    .map(([k, v]) => {
      const text = String(v ?? '');
      const shown = k === 'due_date' ? dayLabel(text) : k === 'duration_minutes' ? `${text} min` : text.toLowerCase();
      return `${CHANGE_LABEL[k] ?? k} → ${shown}`;
    })
    .join(', ');
}

/** Plain-English heading and "who sees it" line for each kind of draft. `md` = how the MD is referred to. */
export function draftHeading(d: Draft, md = 'Sir', him = 'him'): { title: string; detail: string; icon: string } {
  const pa = d.pa_name || 'your PA';
  switch (d.action) {
    case 'private_task':
      return { title: 'Your personal task', detail: 'Only you can see this.', icon: 'lock-closed-outline' };
    case 'pa_task':
      return { title: `Task for ${pa}`, detail: `${pa} does it. You both get reminders.`, icon: 'person-outline' };
    case 'pa_meeting':
      return { title: `Meeting with ${pa}`, detail: `${pa} is told. You both get reminders.`, icon: 'people-outline' };
    case 'delegation':
      return d.staff
        ? { title: `Task for ${d.staff.name}`, detail: `${d.staff.department} · goes to the staff task app`, icon: 'briefcase-outline' }
        : { title: 'Task for a staff member', detail: 'Choose who should do it.', icon: 'briefcase-outline' };
    case 'team_delegation':
      return {
        title: `Task for ${d.team?.label ?? 'the team'}`,
        detail: `${d.team?.members.length ?? 0} people · each gets their own task`,
        icon: 'people-circle-outline',
      };
    case 'md_request':
      return { title: `Task for ${md}`, detail: `Goes straight onto ${md}’s list and rings until seen. ${md} can decline it.`, icon: 'person-outline' };
    case 'mg_task':
      return { title: `Task for ${d.mg_name || 'the manager'}`, detail: `${d.mg_name || 'The manager'} is told at once.`, icon: 'person-outline' };
    case 'pa_self_task':
      return { title: 'Your own task', detail: `On your list. ${md} can see it; it is not sent to ${him}.`, icon: 'person-outline' };
    case 'person_task':
      return {
        title: `Task for ${pa}`,
        detail: (d.assignees?.length ?? 0) > 1 ? `${d.assignees?.length} people · each gets it and is told at once` : `${pa} is told at once. You hear when it is done.`,
        icon: (d.assignees?.length ?? 0) > 1 ? 'people-outline' : 'person-outline',
      };
    case 'person_meeting':
      return { title: `Meeting with ${pa}`, detail: `${pa} is told. You both get reminders.`, icon: 'people-outline' };
    case 'own_task':
      return { title: 'Your own work', detail: `On your list. ${md} can see your work; your personal items only you can see.`, icon: 'person-outline' };
    case 'message':
      return { title: `Message to ${pa}`, detail: 'A message, not a task: nothing to tick off.', icon: 'chatbubble-ellipses-outline' };
    case 'accept_request':
      return { title: 'Accept request', detail: 'It will be added to your list.', icon: 'checkmark-circle-outline' };
    case 'decline_request':
      return { title: 'Decline request', detail: 'The sender will be told.', icon: 'close-circle-outline' };
    case 'complete':
      return { title: 'Mark as done', detail: '', icon: 'checkmark-done-outline' };
    case 'change_task':
      return { title: 'Change task', detail: describeChanges(d.changes), icon: 'create-outline' };
    case 'delete_task':
      return { title: 'Delete task', detail: 'It will be cancelled, not erased.', icon: 'trash-outline' };
    case 'set_setting':
      return { title: 'Setting', detail: '', icon: 'settings-outline' };
    case 'organise':
      return { title: d.title || 'Change the team', detail: 'Departments and sections. A yes does it; "undo" takes it back for a minute.', icon: 'git-network-outline' };
    case 'remember':
      return { title: 'Remember this', detail: 'Only you can see your notes. Delete them any time in Settings.', icon: 'bookmark-outline' };
    case 'choose_destination':
      return {
        title: /don't know any/i.test(d.question ?? '') ? 'Who is this?' : 'Whose is this?',
        detail: 'Just say it: JARVIS asks before it saves anything.',
        icon: 'help-circle-outline',
      };
    default:
      return { title: 'JARVIS', detail: '', icon: 'sparkles-outline' };
  }
}

/** Short description of a waiting card, so the AI can tell yes / no / a change / something else apart. */
export function describeDraft(d: Draft, md = 'Sir'): string {
  const one = (x: Draft) => {
    const what = x.action === 'remember' ? x.memory_text
      : x.targets?.length ? x.targets.map((t) => t.title).join(' + ')
      : x.target ? x.target.title : x.title;
    const when = x.due_date ? `, ${dayLabel(x.due_date)}${x.due_time ? ` ${x.due_time}` : ''}` : '';
    const who = x.action === 'delegation' && x.staff ? `, for ${x.staff.name}`
      : x.assignees?.length ? `, for ${x.assignees.map((a) => a.name).join(' and ')}` : '';
    return `${draftHeading(x, md).title}: ${what}${when}${who}`;
  };
  // Several things in one breath: the whole list, so "second one 9 baje" or "sab save karo" are read right.
  const all = [d, ...(d.batch ?? [])].map(one);
  return (all.length > 1 ? `${all.length} items: ${all.join(' | ')}` : all[0]).slice(0, 400);
}
