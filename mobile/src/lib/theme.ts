// Clean, quiet, assistant-like: white surfaces, near-black text, neutral greys, one soft indigo accent.
// System sans-serif everywhere (Roboto on Android), sized like a modern chat assistant.
export const colors = {
  bg: '#FAFAFC',
  surface: '#FFFFFF',
  surfaceAlt: '#F3F3F7',
  ink: '#141427', // primary: buttons, headings
  inkSoft: '#3F3F55',
  text: '#1A1A2E',
  muted: '#6B6B80',
  faint: '#A1A1B0',
  line: '#E6E6EF',
  // The accent (kept under its old name so every screen picks it up): soft indigo, like the references.
  gold: '#5B5BD6',
  goldSoft: '#ECECFB',
  accent: '#5B5BD6',
  accentSoft: '#ECECFB',
  accentDeep: '#3A3AA8',
  accentGlow: '#8E8EF2',
  success: '#1F7A4D',
  successSoft: '#E6F4EC',
  danger: '#C0392B',
  dangerSoft: '#FBEAE8',
  warn: '#B7791F',
  warnSoft: '#FBF1DF',
  info: '#2F6FB0',
  infoSoft: '#E8F0F9',
  overlay: 'rgba(20,20,39,0.38)',
  white: '#FFFFFF',
};

/** Gradients used by the orb and the hero cards. */
export const gradients = {
  orb: ['#9B9BFF', '#5B5BD6', '#2B2B86'] as const,
  orbListening: ['#B6A6FF', '#6E5BE0', '#3B2F9E'] as const,
  orbSpeaking: ['#8FD3FF', '#5B7BE0', '#2E3A9E'] as const,
  hero: ['#F2F1FF', '#FAFAFC'] as const,
  sheen: ['rgba(255,255,255,0)', 'rgba(255,255,255,0.45)', 'rgba(255,255,255,0)'] as const,
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

/** Tone names used by stat tiles and charts coming from the server. */
export const tone: Record<string, { fg: string; bg: string }> = {
  accent: { fg: colors.accent, bg: colors.accentSoft },
  info: { fg: colors.info, bg: colors.infoSoft },
  success: { fg: colors.success, bg: colors.successSoft },
  danger: { fg: colors.danger, bg: colors.dangerSoft },
  warn: { fg: colors.warn, bg: colors.warnSoft },
  muted: { fg: colors.muted, bg: colors.surfaceAlt },
};

// Poppins everywhere (loaded in app/_layout.tsx). Android needs the family per weight, so fontFor() maps a
// fontWeight to the right file; Txt applies it automatically.
export const fonts = {
  regular: 'Poppins_400Regular',
  medium: 'Poppins_500Medium',
  semibold: 'Poppins_600SemiBold',
  bold: 'Poppins_700Bold',
  /** Headings (kept under the old name so nothing else changes). */
  serif: 'Poppins_600SemiBold',
};

export function fontFor(weight?: string | number): string {
  const w = String(weight ?? '400');
  if (w === '700' || w === '800' || w === '900' || w === 'bold') return fonts.bold;
  if (w === '600') return fonts.semibold;
  if (w === '500') return fonts.medium;
  return fonts.regular;
}

export const radius = { sm: 10, md: 14, lg: 20, xl: 28 };

export const shadow = {
  shadowColor: '#141427',
  shadowOpacity: 0.06,
  shadowRadius: 14,
  shadowOffset: { width: 0, height: 4 },
  elevation: 2,
};
