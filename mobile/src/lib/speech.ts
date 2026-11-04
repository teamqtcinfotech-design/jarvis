import * as Speech from 'expo-speech';

// One voice at a time. Speaking stops instantly when the mic opens.
// onDone fires when an utterance finishes naturally or fails (so a conversation never gets stuck),
// but not when it is interrupted on purpose.
export type SpeakLang = 'hi' | 'en';

interface Utterance {
  text: string;
  lang: SpeakLang;
  onDone?: () => void;
}

let queue: Utterance[] = [];
let speaking = false;
// Voice used for English sentences. 'hi-IN' = Hindi accent (default), 'en-IN' = Indian English.
let accent = 'hi-IN';
const listeners = new Set<(speaking: boolean) => void>();

export function setVoiceAccent(value: string) {
  accent = value;
}

/** Subscribe to speaking on/off (for the "Speaking…" status). Returns an unsubscribe function. */
export function onSpeakingChange(fn: (speaking: boolean) => void) {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

function setSpeaking(value: boolean) {
  if (speaking === value) return;
  speaking = value;
  listeners.forEach((fn) => fn(value));
}

function pump() {
  if (speaking || queue.length === 0) return;
  const item = queue.shift()!;
  setSpeaking(true);
  Speech.speak(item.text, {
    language: item.lang === 'hi' ? 'hi-IN' : accent,
    rate: 0.95,
    pitch: 1.0,
    onDone: () => {
      setSpeaking(false);
      item.onDone?.();
      pump();
    },
    onStopped: () => {
      setSpeaking(false);
      pump();
    },
    onError: () => {
      setSpeaking(false);
      item.onDone?.();
      pump();
    },
  });
}

export function say(text: string, opts: { interrupt?: boolean; onDone?: () => void; lang?: SpeakLang } = {}) {
  if (!text?.trim()) {
    opts.onDone?.();
    return;
  }
  if (opts.interrupt) {
    queue = [];
    Speech.stop();
    setSpeaking(false);
  }
  queue.push({ text, lang: opts.lang ?? 'en', onDone: opts.onDone });
  pump();
}

export function stopSpeaking() {
  queue = [];
  Speech.stop();
  setSpeaking(false);
}

export function isSpeaking() {
  return speaking;
}
