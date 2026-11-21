/**
 * The wake words the phone can listen for, one at a time. Each is a small on-device listener bundled in the app
 * (modules/jarvis-wake/android/src/main/assets), and brings the assistant's own name with it: "Hey Billy" makes
 * the assistant Billy. The app itself stays JARVIS. The server keeps the same table (core.services.WAKE_WORDS).
 */
export interface WakeWord {
  key: string;
  phrase: string;
  name: string;
  model: string;
}

export const WAKE_WORDS: WakeWord[] = [
  { key: 'hey_jarvis', phrase: 'Hey JARVIS', name: 'JARVIS', model: 'hey_jarvis_v0.1.onnx' },
  { key: 'hey_billy', phrase: 'Hey Billy', name: 'Billy', model: 'hey_billy.onnx' },
  { key: 'hey_honey', phrase: 'Hey Honey', name: 'Honey', model: 'hey_honey.onnx' },
  { key: 'ok_boss', phrase: 'OK Boss', name: 'Boss', model: 'ok_boss.onnx' },
  { key: 'alfred', phrase: 'Alfred', name: 'Alfred', model: 'alfred.onnx' },
  { key: 'computer', phrase: 'Computer', name: 'Computer', model: 'computer.onnx' },
  { key: 'hey_luna', phrase: 'Hey Luna', name: 'Luna', model: 'hey_luna.onnx' },
];

export function wakeWordOf(key: string | undefined): WakeWord {
  return WAKE_WORDS.find((w) => w.key === key) ?? WAKE_WORDS[0];
}

/** The wake phrase as the phone's speech recogniser tends to write it, at the start of what was heard, for every
 *  word the phone can listen for (so "Hey Billy, kal 5 baje…" keeps only the instruction). */
export const WAKE_PHRASE = new RegExp(
  '^\\s*(?:(?:hey|hi|hello|ok|okay|he|hay|hai|a|oh|हे|हाय|हेलो|ओके)[\\s,]+)?'
  + '(?:jarvis|jarwis|jarvish|jervis|jarves|javis|jarbis|जार्विस|जारविस|billy|billie|bili|बिली|honey|hunny|हनी|boss|बॉस|alfred|अल्फ्रेड|computer|कंप्यूटर|luna|लूना)'
  + '[\\s,.!?]*',
  'i',
);
