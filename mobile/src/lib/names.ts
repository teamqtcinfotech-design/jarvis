/**
 * How people are addressed. The server decides ("Sir", "Ma'am", "Madam" or the MD's own name, from the MD's
 * setting; the PA by first name) and sends the word with the login and on the dashboard. Nothing on the phone
 * spells "Sir" on its own; the fallbacks below are only for a session saved before the server sent the word.
 */
import type { Dashboard, SessionUser } from './types';

/** How JARVIS addresses the signed-in person. */
export const addressOf = (u: SessionUser | null | undefined): string => u?.address || (u?.role === 'MD' ? 'Sir' : u?.name ?? '');

/** The same in Devanagari, for the Hindi voice. */
export const addressHiOf = (u: SessionUser | null | undefined): string => u?.address_hi || (u?.role === 'MD' ? 'सर' : u?.name ?? '');

/** How the MD is referred to on the PA's screens and in her JARVIS's lines ("Waiting for Ma'am"). */
export const mdWord = (d: Dashboard | null | undefined): string => d?.md_address || 'Sir';

export const mdWordHi = (d: Dashboard | null | undefined): string => d?.md_address_hi || 'सर';

const FEMALE = /^(ma'?am|madam|madame|miss|mrs|ms)$/i;

/** "he" / "she" for the MD, from the form of address (a bare name says nothing, so it stays "he"). */
export const mdHe = (d: Dashboard | null | undefined): string => (FEMALE.test(mdWord(d)) ? 'she' : 'he');

export const mdHim = (d: Dashboard | null | undefined): string => (FEMALE.test(mdWord(d)) ? 'her' : 'him');
