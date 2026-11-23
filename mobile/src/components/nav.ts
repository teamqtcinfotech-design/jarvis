/** The task lists, as menu items (phone drawer) or rail icons (tablet). One place, so counts never differ. */
import type { DrawerItem } from './Drawer';
import type { EngineValueLite } from './navTypes';
import { mdWord } from '@/lib/names';
import { colors } from '@/lib/theme';
import type { Dashboard, SessionUser } from '@/lib/types';

const open = (items: { status: string }[] | undefined) => (items ?? []).filter((t) => t.status !== 'COMPLETED' && t.status !== 'CANCELLED').length;

export function navItems(user: SessionUser, data: Dashboard | null, engine: EngineValueLite): DrawerItem[] {
  const isMD = user.role === 'MD';
  const md = mdWord(data);
  const pending = data?.requests.filter((r) => r.state === 'PENDING').length ?? 0;
  const privateCount = data ? [...data.overdue, ...data.today, ...data.upcoming, ...data.anytime].filter((t) => t.scope === 'private').length : 0;
  return [
    { key: 'today', label: 'Today', icon: 'today-outline', count: data?.today.length },
    { key: 'late', label: 'Late', icon: 'alert-circle-outline', count: data?.overdue.length, tone: colors.danger },
    { key: 'upcoming', label: 'Coming up', icon: 'calendar-outline', count: data?.upcoming.length },
    { key: 'nodate', label: 'No date yet', icon: 'infinite-outline', count: data?.anytime.length },
    // Everyone has personal items now (theirs alone), and everyone can give work to anyone on the team.
    { key: 'private', label: 'Personal', icon: 'lock-closed-outline', count: privateCount, detail: 'Only you can see these' },
    { key: 'given', label: 'Given by you', icon: 'people-outline', count: open(data?.given_tasks), detail: 'Work you gave people, with progress' },
    // Work for the MD goes straight onto his list now; a request from before that still waits is listed until it is settled.
    ...(pending
      ? [{ key: 'requests', label: isMD ? 'Need your OK' : `Waiting for ${md}`, icon: 'hand-left-outline', count: pending, tone: colors.gold }]
      : []),
    ...(isMD
      ? [
          { key: 'team', label: 'Team’s work', icon: 'git-network-outline', count: open(data?.team_tasks), detail: 'Everyone’s work, their own and given to each other' },
        ]
      : [
          { key: 'md_tasks', label: `${md}’s tasks from you`, icon: 'person-outline', count: open(data?.md_tasks) },
          { key: 'busy', label: `When ${md} is busy`, icon: 'time-outline' },
        ]),
    { key: 'staff', label: 'Given to staff', icon: 'briefcase-outline', count: open(data?.delegated) },
    // The company's staff panel and its partner / franchise records (TaskPanel, Nov 2026).
    { key: 'staff_panel', label: 'Staff', icon: 'id-card-outline', detail: 'Everyone in the staff panel, their work and progress' },
    ...(isMD ? [{ key: 'company_panel', label: 'Partners & franchise', icon: 'business-outline', detail: 'The company records, view only' }] : []),
    { key: 'messages', label: 'Messages', icon: 'chatbubbles-outline', detail: 'Received and sent, kept for good' },
    { key: 'held', label: 'Held alerts', icon: 'notifications-outline', count: engine.held.length, tone: colors.danger, detail: 'Kept quiet while you were busy' },
    { key: 'updates', label: 'Updates', icon: 'newspaper-outline', count: engine.news?.length, detail: 'Small news, decisions and nudges' },
    { key: 'cancelled', label: 'Cancelled', icon: 'close-circle-outline', detail: 'Last 30 days · restore from here' },
    { key: 'briefing', label: 'Briefing', icon: 'sunny-outline' },
    { key: 'settings', label: 'Settings', icon: 'settings-outline' },
  ];
}

/** Which server data view a list key maps to (for the tablet workspace). */
export const VIEW_FOR_KEY: Record<string, string> = {
  today: 'today', late: 'late', upcoming: 'upcoming', nodate: 'nodate', private: 'private', requests: 'requests',
  pa: 'pa', mg: 'pa', given: 'pa', md_tasks: 'pa', team: 'pa', busy: 'busy', staff: 'staff', cancelled: 'cancelled',
  messages: 'messages',
};
