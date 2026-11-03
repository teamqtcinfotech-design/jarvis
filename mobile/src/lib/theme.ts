// Classic executive palette: ivory, deep navy, muted gold. Calm, not flashy.
export const colors = {
  bg: '#F6F3EC',
  surface: '#FFFFFF',
  surfaceAlt: '#EFEAE0',
  ink: '#13233A',
  inkSoft: '#2C3E57',
  text: '#1E2633',
  muted: '#6B7280',
  faint: '#9AA1AC',
  line: '#E3DDD0',
  gold: '#A8834A',
  goldSoft: '#F3EADB',
  success: '#2F6B4F',
  successSoft: '#E4EFE8',
  danger: '#9B2C2C',
  dangerSoft: '#F6E5E2',
  warn: '#A8691A',
  warnSoft: '#F8EEDC',
  info: '#2F5D8A',
  infoSoft: '#E5EDF5',
  overlay: 'rgba(19,35,58,0.48)',
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
  HIGH: 'High',
  NORMAL: 'Normal',
  LOW: 'Low',
};

export const kindLabel: Record<string, string> = {
  TASK: 'Task',
  MEETING: 'Meeting',
  CALL: 'Call',
  PAYMENT: 'Payment',
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
  shadowColor: '#13233A',
  shadowOpacity: 0.08,
  shadowRadius: 12,
  shadowOffset: { width: 0, height: 4 },
  elevation: 3,
};
