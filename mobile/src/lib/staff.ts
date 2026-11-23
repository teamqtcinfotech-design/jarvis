/** Small helpers for the staff screens (the staff panel's people, Nov 2026). */
import { colors } from './theme';

/** One colour per department, so a person's department is seen before it is read. */
export const DEPT_TONE: Record<string, string> = {
  Accounts: '#2F6FED', 'Customer Care': '#0E9F6E', Admin: '#5B5BD6', Management: '#B45309', CA: '#7C3AED', 'Technical Team': '#DB2777', Reception: '#0891B2',
};

export const deptTone = (dept: string) => DEPT_TONE[dept] ?? colors.muted;

/** "Mr. Bijender ( Admin)" -> "MB", "NIKKI" -> "N". */
export const initials = (name: string) =>
  name.replace(/\(.*?\)/g, '').trim().split(/\s+/).slice(0, 2).map((w) => w[0]?.toUpperCase() ?? '').join('') || '?';

export const leaveLabel = (s: { on_leave?: boolean; leave_until?: string | null }) =>
  s.on_leave
    ? (s.leave_until ? `On leave until ${new Date(s.leave_until).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })}` : 'On leave')
    : '';
