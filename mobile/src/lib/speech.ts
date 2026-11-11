import * as Speech from 'expo-speech';

import { onTts, ttsAvailable, ttsSpeak, ttsStop } from './wake';

// One voice at a time. Speaking stops instantly when the mic opens.
// onDone fires when an utterance finishes naturally, fails, or times out (so a conversation never gets stuck),
// but not when it is interrupted on purpose.
export type SpeakLang = 'hi' | 'en';

interface Utterance {
  id: string;
  text: string;
  lang: SpeakLang;
  /** Plain-letter version of a Devanagari sentence, used if the phone has no Hindi voice. */
  fallback?: string;
  onDone?: () => void;
}

let queue: Utterance[] = [];
let current: { item: Utterance; watchdog: ReturnType<typeof setTimeout>; startedAt: number } | null = null;
// The sentence that was cut short last (he talked over it, or tapped): what it was and how long it had been playing,
// so "continue" can pick it up where it stopped.
let cutShort: { text: string; lang: SpeakLang; fallback?: string; playedMs: number; at: number } | null = null;
const noteCut = () => {
  if (!current) return;
  cutShort = { text: current.item.text, lang: current.item.lang, fallback: current.item.fallback,
    playedMs: Date.now() - current.startedAt, at: Date.now() };
};

/** The rest of the sentence that was cut short in the last few seconds (from the start of the sentence it was in). */
export function restOfCutShort(withinMs = 4000): { text: string; lang: SpeakLang } | null {
  const c = cutShort;
  if (!c || Date.now() - c.at > withinMs) return null;
  // About 13 letters a second, after the half second the voice takes to start.
  const spoken = Math.max(0, ((c.playedMs - 600) / 1000) * 13);
  const parts = c.text.match(/[^।.!?]+[।.!?]*\s*/g) ?? [c.text];
  let upTo = 0;
  let from = 0;
  for (let i = 0; i < parts.length; i += 1) {
    upTo += parts[i].length;
    if (upTo <= spoken) from = i + 1; // this sentence was said in full
  }
  const rest = parts.slice(Math.min(from, parts.length - 1)).join('').trim();
  return rest ? { text: rest, lang: c.lang } : null;
}
// Voice used for English sentences. 'hi-IN' = Hindi accent (default), 'en-IN' = Indian English.
let accent = 'hi-IN';
let seq = 0;
const listeners = new Set<(speaking: boolean) => void>();

export function setVoiceAccent(value: string) {
  accent = value;
}

/** Subscribe to speaking on/off (for the orb). Returns an unsubscribe function. */
export function onSpeakingChange(fn: (speaking: boolean) => void) {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

function notify(speaking: boolean) {
  listeners.forEach((fn) => fn(speaking));
}

/** Called when the native voice (or expo-speech, or the watchdog) says the current sentence has ended. */
function finish(id: string, event: 'done' | 'error' | 'stopped' | 'timeout') {
  if (!current || current.item.id !== id) return;
  clearTimeout(current.watchdog);
  if (event === 'stopped') noteCut();
  const { item } = current;
  current = null;
  notify(false);
  if (event !== 'stopped') item.onDone?.(); // an error or a hang still moves the conversation on
  pump();
}

if (ttsAvailable) onTts((e) => finish(e.id, e.event));

function pump() {
  if (current || queue.length === 0) return;
  const item = queue.shift()!;
  const language = item.lang === 'hi' ? 'hi-IN' : accent;
  // If the engine never answers (lost connection, missing voice), give up after a generous reading time.
  const watchdog = setTimeout(() => finish(item.id, 'timeout'), 5000 + item.text.length * 90);
  current = { item, watchdog, startedAt: Date.now() };
  notify(true);
  if (ttsAvailable) {
    if (!ttsSpeak(item.id, item.text, language, item.fallback ?? null, 0.95)) finish(item.id, 'error');
    return;
  }
  Speech.speak(item.text, {
    language,
    rate: 0.95,
    pitch: 1.0,
    onDone: () => finish(item.id, 'done'),
    onStopped: () => finish(item.id, 'stopped'),
    onError: () => finish(item.id, 'error'),
  });
}

/** "PA" is read by every voice as a word ("paa"); as "P.A." it is said letter by letter. The screen keeps "PA". */
export const forSpeech = (text: string) => text.replace(/\bPA\b/g, 'P.A.');

export function say(text: string, opts: { interrupt?: boolean; onDone?: () => void; lang?: SpeakLang; fallback?: string } = {}) {
  if (!text?.trim()) {
    opts.onDone?.();
    return;
  }
  if (opts.interrupt) stopSpeaking();
  queue.push({
    id: `u${Date.now()}-${seq++}`, text: forSpeech(text), lang: opts.lang ?? 'en',
    fallback: opts.fallback ? forSpeech(opts.fallback) : opts.fallback, onDone: opts.onDone,
  });
  pump();
}

export function stopSpeaking() {
  queue = [];
  if (ttsAvailable) ttsStop();
  else Speech.stop();
  if (current) {
    noteCut();
    clearTimeout(current.watchdog);
    current = null;
    notify(false);
  }
}

export function isSpeaking() {
  return current !== null;
}
