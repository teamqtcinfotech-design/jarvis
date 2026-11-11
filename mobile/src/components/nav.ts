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
    ...(isMD
      ? [
          { key: 'private', label: 'Private reminders', icon: 'lock-closed-outline', count: privateCount, detail: 'Only you can see these' },
          { key: 'requests', label: 'Need your OK', icon: 'hand-left-outline', count: pending, tone: colors.gold },
          { key: 'pa', label: 'Given to your PA', icon: 'person-outline', count: open(data?.pa_tasks) },
        ]
      : [
          { key: 'requests', label: `Waiting for ${md}`, icon: 'hand-left-outline', count: pending, tone: colors.gold },
          { key: 'md_tasks', label: `${md}’s tasks from you`, icon: 'person-outline', count: open(data?.md_tasks) },
          { key: 'busy', label: `When ${md} is busy`, icon: 'time-outline' },
        ]),
    { key: 'staff', label: 'Given to staff', icon: 'briefcase-outline', count: open(data?.delegated) },
    { key: 'held', label: 'Held alerts', icon: 'notifications-outline', count: engine.held.length, tone: colors.danger, detail: 'Kept quiet while you were busy' },
    { key: 'briefing', label: 'Briefing', icon: 'sunny-outline' },
    { key: 'settings', label: 'Settings', icon: 'settings-outline' },
  ];
}

/** Which server data view a list key maps to (for the tablet workspace). */
export const VIEW_FOR_KEY: Record<string, string> = {
  today: 'today', late: 'late', upcoming: 'upcoming', nodate: 'nodate', private: 'private', requests: 'requests',
  pa: 'pa', md_tasks: 'pa', busy: 'busy', staff: 'staff',
};
