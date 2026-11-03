import * as Speech from 'expo-speech';

// One voice at a time. Speaking stops instantly when the mic opens.
// onDone fires only when an utterance finishes naturally (not when it is interrupted).
interface Utterance {
  text: string;
  onDone?: () => void;
}

let queue: Utterance[] = [];
let speaking = false;
let language = 'en-IN';

export function setVoiceLanguage(lang: string) {
  language = lang;
}

function next() {
  speaking = false;
  pump();
}

function pump() {
  if (speaking || queue.length === 0) return;
  const item = queue.shift()!;
  speaking = true;
  Speech.speak(item.text, {
    language,
    rate: 0.97,
    pitch: 1.0,
    onDone: () => {
      item.onDone?.();
      next();
    },
    onStopped: next,
    onError: next,
  });
}

export function say(text: string, opts: { interrupt?: boolean; onDone?: () => void } = {}) {
  if (!text?.trim()) {
    opts.onDone?.();
    return;
  }
  if (opts.interrupt) {
    queue = [];
    Speech.stop();
    speaking = false;
  }
  queue.push({ text, onDone: opts.onDone });
  pump();
}

export function stopSpeaking() {
  queue = [];
  Speech.stop();
  speaking = false;
}
