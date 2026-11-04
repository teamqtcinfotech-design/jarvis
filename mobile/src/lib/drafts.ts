import { dayLabel } from './format';
import type { Draft, DraftAction } from './types';

export const TASK_DRAFTS: DraftAction[] = ['private_task', 'md_request', 'delegation', 'team_delegation', 'pa_task', 'pa_meeting'];

/** Cards that end with a question ("shall I save it?") and wait for a yes / no / change. */
export const CONFIRMABLE: DraftAction[] = [...TASK_DRAFTS, 'accept_request', 'decline_request', 'complete', 'remember',
  'change_task', 'delete_task'];

const CHANGE_LABEL: Record<string, string> = { title: 'name', due_date: 'day', due_time: 'time', priority: 'priority', kind: 'type' };

export function describeChanges(changes: Record<string, string> | undefined): string {
  if (!changes) return '';
  return Object.entries(changes)
    .map(([k, v]) => `${CHANGE_LABEL[k] ?? k} → ${k === 'due_date' ? dayLabel(v) : v.toLowerCase()}`)
    .join(', ');
}

/** Plain-English heading and "who sees it" line for each kind of draft. */
export function draftHeading(d: Draft): { title: string; detail: string; icon: string } {
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
      return { title: 'Request to Sir', detail: 'Sir will accept or decline it.', icon: 'paper-plane-outline' };
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
    case 'remember':
      return { title: 'Remember this', detail: 'Only you can see your notes. Delete them any time in Settings.', icon: 'bookmark-outline' };
    case 'choose_destination':
      return { title: 'Where should this go?', detail: 'Say: personal, your PA, or a staff name.', icon: 'help-circle-outline' };
    default:
      return { title: 'JARVIS', detail: '', icon: 'sparkles-outline' };
  }
}

/** Short description of a waiting card, so the AI can tell yes / no / a change / something else apart. */
export function describeDraft(d: Draft): string {
  const what = d.action === 'remember' ? d.memory_text
    : d.targets?.length ? d.targets.map((t) => t.title).join(' + ')
    : d.target ? d.target.title : d.title;
  const when = d.due_date ? `, ${dayLabel(d.due_date)}${d.due_time ? ` ${d.due_time}` : ''}` : '';
  const who = d.action === 'delegation' && d.staff ? `, for ${d.staff.name}` : '';
  return `${draftHeading(d).title}: ${what}${when}${who}`.slice(0, 400);
}
