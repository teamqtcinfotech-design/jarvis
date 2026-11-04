// Light, calm and premium: whitish background, near-black text, one warm accent used sparingly.
export const colors = {
  bg: '#FAFAF8',
  surface: '#FFFFFF',
  surfaceAlt: '#F2F2EF',
  ink: '#1C2333', // primary: buttons, mic, headings
  inkSoft: '#3D4556',
  text: '#1C1F26',
  muted: '#6B6F78',
  faint: '#A0A3AA',
  line: '#E6E5E0',
  gold: '#B5854B', // the one accent colour
  goldSoft: '#F6EEE2',
  success: '#2E7D5B',
  successSoft: '#E6F2EC',
  danger: '#B4372F',
  dangerSoft: '#FBEAE8',
  warn: '#B7791F',
  warnSoft: '#FBF1DF',
  info: '#2F6FB0',
  infoSoft: '#E8F0F9',
  overlay: 'rgba(20,24,33,0.35)',
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
  serif: 'serif',
};

export const radius = { sm: 8, md: 12, lg: 18, xl: 28 };

export const shadow = {
  shadowColor: '#1C2333',
  shadowOpacity: 0.06,
  shadowRadius: 14,
  shadowOffset: { width: 0, height: 4 },
  elevation: 2,
};
