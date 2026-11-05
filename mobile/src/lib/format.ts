const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

export function timeOf(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  const h = d.getHours() % 12 || 12;
  const m = d.getMinutes();
  return `${h}${m ? `:${String(m).padStart(2, '0')}` : ''} ${d.getHours() < 12 ? 'AM' : 'PM'}`;
}

/** '17:00' -> '5 PM', '09:30' -> '9:30 AM'. */
export function timeLabel(hhmm: string | null | undefined): string {
  if (!hhmm) return '';
  const [h, m] = hhmm.split(':').map(Number);
  if (Number.isNaN(h)) return hhmm;
  const hour = h % 12 || 12;
  return `${hour}${m ? `:${String(m).padStart(2, '0')}` : ''} ${h < 12 ? 'AM' : 'PM'}`;
}

export function isoDate(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

export function addDays(base: Date, n: number): Date {
  const d = new Date(base);
  d.setDate(d.getDate() + n);
  return d;
}

export function dayLabel(dateStr: string): string {
  if (!dateStr) return 'No date';
  const [y, m, d] = dateStr.split('-').map(Number);
  const date = new Date(y, m - 1, d);
  const today = new Date();
  const t = isoDate(today);
  if (dateStr === t) return 'Today';
  if (dateStr === isoDate(addDays(today, 1))) return 'Tomorrow';
  if (dateStr === isoDate(addDays(today, -1))) return 'Yesterday';
  return `${DAYS[date.getDay()]} ${date.getDate()} ${MONTHS[date.getMonth()]}`;
}

export function greeting(): string {
  const h = new Date().getHours();
  if (h < 12) return 'Good morning';
  if (h < 17) return 'Good afternoon';
  return 'Good evening';
}

export function relativeAgo(ts: number | null): string {
  if (!ts) return 'not yet';
  const s = Math.round((Date.now() - ts) / 1000);
  if (s < 60) return 'just now';
  const m = Math.round(s / 60);
  return m < 60 ? `${m} min ago` : `${Math.round(m / 60)} h ago`;
}
