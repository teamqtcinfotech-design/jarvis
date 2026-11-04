// Clean, quiet, assistant-like: white surfaces, near-black text, neutral greys, one warm accent used sparingly.
// System sans-serif everywhere (Roboto on Android), sized like a modern chat assistant.
export const colors = {
  bg: '#FFFFFF',
  surface: '#FFFFFF',
  surfaceAlt: '#F4F4F5',
  ink: '#0D0D0D', // primary: buttons, orb, headings
  inkSoft: '#3F3F46',
  text: '#1A1A1A',
  muted: '#6B6B76',
  faint: '#A1A1AA',
  line: '#E6E6EA',
  gold: '#B8864F', // the one accent colour
  goldSoft: '#F7EFE4',
  success: '#1F7A4D',
  successSoft: '#E6F4EC',
  danger: '#C0392B',
  dangerSoft: '#FBEAE8',
  warn: '#B7791F',
  warnSoft: '#FBF1DF',
  info: '#2F6FB0',
  infoSoft: '#E8F0F9',
  overlay: 'rgba(13,13,13,0.35)',
  white: '#FFFFFF',
};

export const priorityColor: Record<string, string> = {
  URGENT: colors.danger,
  HIGH: colors.warn,
  NORMAL: colors.info,
  LOW: '#7A8594',
};

export const prioritySoft: Record<string, string> = {
  URGENT: colors.dangerSoft,
  HIGH: colors.warnSoft,
  NORMAL: colors.infoSoft,
  LOW: colors.surfaceAlt,
};

export const priorityLabel: Record<string, string> = {
  URGENT: 'Urgent',
  HIGH: 'Important',
  NORMAL: 'Normal',
  LOW: 'Low',
};

export const kindLabel: Record<string, string> = {
  TASK: 'Task',
  MEETING: 'Meeting',
  CALL: 'Call',
  PAYMENT: 'Payment reminder',
  FOLLOW_UP: 'Follow-up',
};

export const kindIcon: Record<string, string> = {
  TASK: 'checkbox-outline',
  MEETING: 'people-outline',
  CALL: 'call-outline',
  PAYMENT: 'card-outline',
  FOLLOW_UP: 'return-up-forward-outline',
};

export const fonts = {
  // Kept as a name so headings can switch later; the app now uses the system sans-serif everywhere.
  serif: 'sans-serif',
};

export const radius = { sm: 10, md: 14, lg: 20, xl: 28 };

export const shadow = {
  shadowColor: '#0D0D0D',
  shadowOpacity: 0.05,
  shadowRadius: 14,
  shadowOffset: { width: 0, height: 4 },
  elevation: 2,
};
