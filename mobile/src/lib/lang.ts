/**
 * Which language JARVIS speaks to this person right now, so every line the APP builds itself (saved lines, the
 * answers to an alert's question, errors) follows the same rule as the server's lines:
 *
 *   1. a pinned setting ("always Hindi" / "always English") wins;
 *   2. otherwise the language of the last turn, as the server decided it (reply_language);
 *   3. before any turn, the language the server says this person has mostly used lately (/me/ "language").
 *
 * The server keeps the rule itself (core/lang.py); this is only the phone's copy of its latest answer.
 */
export type Lang = 'hi' | 'en';

let current: Lang = 'en';
let pinned: 'auto' | Lang = 'auto';

/** The server decided the language of the last turn (or, at start, of this person lately). */
export function setReplyLang(value: string | undefined | null) {
  if (value === 'hi' || value === 'en') current = value;
}

/** The person's own setting: 'auto' = match them, or always 'hi' / 'en'. */
export function setPinnedLang(value: string | undefined | null) {
  pinned = value === 'hi' || value === 'en' ? value : 'auto';
}

export function replyLang(): Lang {
  return pinned === 'auto' ? current : pinned;
}

export function pinnedLang(): 'auto' | Lang {
  return pinned;
}

const DEVANAGARI = /[ऀ-ॿ]/;

/** The voice for a line that is already written: Devanagari is read by the Hindi voice. */
export const langOfText = (text: string): Lang => (DEVANAGARI.test(text) ? 'hi' : 'en');

/**
 * One app-built line in both languages -> what to say now. The English line is always what the screen shows and
 * what the phone falls back to when it has no Hindi voice.
 */
export function twin(en: string, hi: string): { text: string; lang: Lang; fallback: string } {
  return replyLang() === 'hi' && hi ? { text: hi, lang: 'hi', fallback: en } : { text: en, lang: 'en', fallback: en };
}
