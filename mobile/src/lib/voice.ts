import { ExpoSpeechRecognitionModule, useSpeechRecognitionEvent } from 'expo-speech-recognition';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Animated, Platform } from 'react-native';

import { stopSpeaking } from './speech';

export type CaptureState = 'idle' | 'listening' | 'done' | 'error';

const ERROR_TEXT: Record<string, string> = {
  'not-allowed': 'Microphone permission is needed. Allow it in Settings.',
  'no-speech': "I didn't hear anything. Tap the mic and speak.",
  'speech-timeout': "I didn't hear anything. Tap the mic and speak.",
  network: 'Speech service needs internet. You can type instead.',
  'service-not-allowed': 'Google speech service is not available on this phone. You can type instead.',
  'language-not-supported': 'This language is not available on the phone. Try English (India).',
  busy: 'The microphone is busy. Try again in a moment.',
  'audio-capture': 'Could not use the microphone.',
};

/** Silence (nothing was said) is not a failure: the conversation asks "are you there?" instead. */
export const SILENCE_CODES = new Set(['no-speech', 'speech-timeout']);

// A long message (a few minutes, said with pauses): every round's words are kept and the recogniser starts again at
// once, until he says "bas" (or "that's all", "send it"), stays quiet for a whole round, or five minutes pass.
const LONG_END = /(?:^|[\s,.!?।])(bas|bas itna|bas itna hi|itna hi|bas itna hi tha|that's all|thats all|that is all|send it|send kar do|bhej do|khatam|ho gaya|done|बस|बस इतना|इतना ही|भेज दो|हो गया)\s*[.!?।]*\s*$/i;
// He stopped on a joining word ("Sunil ko bolo ki …", "… aur"): the sentence is not over, so JARVIS keeps listening.
const JOINING_END = /(?:^|\s)(aur|and|phir|then|ki|ke|ko|ka|matlab|also|plus|tatha|lekin|but|या|और|फिर|कि|को|के|का)\s*[,.]?\s*$/i;
const LONG_MAX_MS = 5 * 60_000;

interface LongState {
  /** Listening across pauses right now. explicit: he asked for it ("lamba message hai"); else a joining word did. */
  active: boolean;
  explicit: boolean;
  buffer: string;
  startedAt: number;
}
const noLong = (): LongState => ({ active: false, explicit: false, buffer: '', startedAt: 0 });
const joinText = (a: string, b: string) => [a, b].map((x) => x.trim()).filter(Boolean).join(' ');

interface Handlers {
  onFinal?: (text: string) => void;
  onError?: (message: string, code: string) => void;
  /** Words the recogniser should expect (JARVIS, Mansi, staff names) so they are not misheard. */
  hints?: string[];
}

interface Probe {
  resolve: (voices: boolean) => void;
  timer: ReturnType<typeof setTimeout>;
}

interface StartOpts {
  quick?: boolean;
  retry?: boolean;
  /** A long message: keep listening across pauses until "bas", a quiet round, or five minutes. */
  long?: boolean;
  /** Internal: the next round of a message already being heard (its words so far are kept). */
  more?: boolean;
}

/**
 * Android's built-in recogniser. lang: 'auto' = Hindi + English (Indian English, which understands Hinglish; on
 * Android 14+ the phone switches to Hindi by itself), or a fixed 'hi-IN' / 'en-IN'. Audio never reaches our server.
 */
export function useVoiceCapture(lang: string, handlers: Handlers = {}) {
  const [state, setState] = useState<CaptureState>('idle');
  const [partial, setPartial] = useState('');
  const latest = useRef('');
  const finalText = useRef('');
  const errored = useRef(false);
  const stateRef = useRef<CaptureState>('idle');
  const probing = useRef<Probe | null>(null);
  // The phone's speech service sometimes drops a round the moment it starts (it had not finished closing the last
  // one): that is not his silence and not an error to show. The round is started again, quietly, up to twice.
  const retries = useRef(0);
  const stoppedOnPurpose = useRef(false);
  const lastQuick = useRef(false);
  const startRef = useRef<(opts?: StartOpts) => void>(() => undefined);
  const longRef = useRef<LongState>(noLong());
  const [longMode, setLongMode] = useState(false);
  // How loud he is speaking right now (0..1), for the orb. Driven by the recogniser's volume events.
  const [level] = useState(() => new Animated.Value(0));
  const setLevel = useCallback(
    (v: number) => Animated.timing(level, { toValue: Math.max(0, Math.min(1, v)), duration: 110, useNativeDriver: true }).start(),
    [level],
  );
  const handlersRef = useRef(handlers);
  useEffect(() => {
    handlersRef.current = handlers;
  });
  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  const watchdog = useRef<ReturnType<typeof setTimeout> | null>(null);
  const clearWatchdog = () => {
    if (watchdog.current) clearTimeout(watchdog.current);
    watchdog.current = null;
  };

  const finishProbe = (voices: boolean) => {
    const p = probing.current;
    if (!p) return;
    probing.current = null;
    clearTimeout(p.timer);
    try {
      stoppedOnPurpose.current = true;
      ExpoSpeechRecognitionModule.abort();
    } catch {
      // nothing to abort
    }
    p.resolve(voices);
  };

  useSpeechRecognitionEvent('start', () => {
    if (!probing.current) setState('listening');
  });
  useSpeechRecognitionEvent('speechstart', () => {
    // A sound alone is not a conversation (a door, a cough); the probe waits for actual words.
  });
  useSpeechRecognitionEvent('result', (event) => {
    const text = event.results[0]?.transcript ?? '';
    if (probing.current) {
      if (text.trim().split(/\s+/).length >= 2) finishProbe(true); // someone really is talking in the room
      return;
    }
    latest.current = text;
    if (event.isFinal) finalText.current = text;
    setPartial(joinText(longRef.current.buffer, text)); // a long message shows everything heard so far
  });
  useSpeechRecognitionEvent('volumechange', (event) => {
    if (probing.current) return;
    setLevel((event.value + 2) / 12); // the recogniser reports -2 (silence) .. 10 (loud)
  });
  /** The long message is over: everything heard is one instruction ("bas" itself is left out). */
  const finishLong = () => {
    const all = longRef.current.buffer.replace(LONG_END, '').trim();
    longRef.current = noLong();
    setLongMode(false);
    errored.current = true; // the round's own "end" must not report again
    setState('done');
    if (all) handlersRef.current.onFinal?.(all);
    else handlersRef.current.onError?.(ERROR_TEXT['no-speech'], 'no-speech');
  };
  /** Listen to the next part of the same message, the words so far kept. */
  const nextRound = () => {
    errored.current = true;
    setTimeout(() => {
      if (!stoppedOnPurpose.current && longRef.current.active) startRef.current({ more: true });
    }, 150);
  };

  useSpeechRecognitionEvent('end', () => {
    setLevel(0);
    if (probing.current) {
      finishProbe(false);
      return;
    }
    clearWatchdog();
    if (errored.current) return;
    const text = (finalText.current || latest.current).trim();
    const long = longRef.current;
    if (long.active) {
      long.buffer = joinText(long.buffer, text);
      const over = !text || LONG_END.test(long.buffer) || Date.now() - long.startedAt > LONG_MAX_MS
        || (!long.explicit && !JOINING_END.test(text));
      if (over || stoppedOnPurpose.current) finishLong();
      else nextRound();
      return;
    }
    if (text && JOINING_END.test(text) && !lastQuick.current) {
      // "Sunil ko bolo ki …" and a breath: the sentence is not finished. Keep listening and join the parts.
      longRef.current = { active: true, explicit: false, buffer: text, startedAt: Date.now() };
      nextRound();
      return;
    }
    setState('done');
    if (text) handlersRef.current.onFinal?.(text);
    else handlersRef.current.onError?.(ERROR_TEXT['no-speech'], 'no-speech');
  });
  useSpeechRecognitionEvent('error', (event) => {
    if (probing.current) {
      finishProbe(false);
      return;
    }
    clearWatchdog();
    if (longRef.current.active && SILENCE_CODES.has(event.error)) {
      finishLong(); // quiet for a whole round: the message is complete
      return;
    }
    const dropped = (event.error === 'aborted' && !stoppedOnPurpose.current) || event.error === 'client' || event.error === 'busy';
    if (dropped && retries.current < 2 && stateRef.current === 'listening') {
      retries.current += 1;
      errored.current = true; // the "end" of the dropped round is not his silence
      setTimeout(() => {
        if (stateRef.current === 'listening' && !stoppedOnPurpose.current) startRef.current({ quick: lastQuick.current, retry: true });
      }, 500);
      return;
    }
    if (event.error === 'aborted') {
      setState('idle');
      return;
    }
    errored.current = true;
    setState('error');
    handlersRef.current.onError?.(ERROR_TEXT[event.error] ?? event.message ?? 'Speech recognition failed.', event.error);
  });

  /**
   * quick: JARVIS has just asked something with a short answer (a card's "save?", "which one?"): the round may end
   * soon after a word or two. Otherwise he gets a long moment to start speaking before it counts as silence.
   */
  const start = useCallback(async (opts: StartOpts = {}) => {
    const quick = opts?.quick === true && !opts?.long;
    if (opts?.retry !== true && opts?.more !== true) retries.current = 0;
    if (opts?.more !== true && opts?.retry !== true) {
      // A fresh start: a long message only when he asked for one.
      longRef.current = opts?.long ? { active: true, explicit: true, buffer: '', startedAt: Date.now() } : noLong();
    }
    setLongMode(longRef.current.active && longRef.current.explicit);
    lastQuick.current = quick;
    stoppedOnPurpose.current = false;
    if (probing.current) finishProbe(false); // the user comes first
    stoppedOnPurpose.current = false;
    stopSpeaking();
    setPartial(longRef.current.buffer);
    latest.current = '';
    finalText.current = '';
    errored.current = false;
    // Ask only when the microphone is not already allowed: the request itself opens a system screen,
    // which would push JARVIS to the background and cut the conversation.
    let perm = await ExpoSpeechRecognitionModule.getMicrophonePermissionsAsync();
    if (!perm.granted) perm = await ExpoSpeechRecognitionModule.requestMicrophonePermissionsAsync();
    if (!perm.granted) {
      errored.current = true;
      setState('error');
      handlersRef.current.onError?.(ERROR_TEXT['not-allowed'], 'not-allowed');
      return;
    }
    setState('listening');
    // If the recogniser ever stops reporting, give up cleanly instead of sitting on "Listening…".
    clearWatchdog();
    watchdog.current = setTimeout(() => {
      watchdog.current = null;
      if (stateRef.current !== 'listening') return;
      errored.current = true;
      try {
        stoppedOnPurpose.current = true;
        ExpoSpeechRecognitionModule.abort();
      } catch {
        // ignore
      }
      if (longRef.current.active && longRef.current.buffer) {
        finishLong(); // what was heard of a long message is kept, not lost
        return;
      }
      longRef.current = noLong();
      setLongMode(false);
      setState('idle');
      handlersRef.current.onError?.(ERROR_TEXT['speech-timeout'], 'speech-timeout');
    }, 30000);
    const auto = lang === 'auto';
    // The automatic Hindi/English switch exists only on Android 14+ (API 34). Older phones fail to start the
    // recogniser if these options are even passed, so they are added only where Android has them.
    const canSwitch = auto && Platform.OS === 'android' && Number(Platform.Version) >= 34;
    ExpoSpeechRecognitionModule.start({
      // 'auto': start in Indian English (understands Hinglish too) and, on Android 14+, let the phone switch
      // to Hindi by itself.
      lang: auto ? 'en-IN' : lang,
      interimResults: true,
      continuous: false,
      addsPunctuation: true,
      contextualStrings: handlersRef.current.hints?.length ? handlersRef.current.hints.slice(0, 60) : undefined,
      volumeChangeEventOptions: { enabled: true, intervalMillis: 120 },
      androidIntentOptions: {
        // A natural pause mid-sentence is not the end of it. The minimum is how long he has to START speaking
        // before the round ends as silence: long when he was just called or is giving an instruction (he may be
        // thinking), short when JARVIS asked a yes / no, so "haan" is not made to wait.
        // In a long message the pauses between thoughts are longer, and each round may run longer.
        EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS: longRef.current.active ? 2500 : 1400,
        EXTRA_SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS: longRef.current.active ? 2500 : 1400,
        EXTRA_SPEECH_INPUT_MINIMUM_LENGTH_MILLIS: quick ? 3000 : 7000,
        ...(canSwitch
          ? {
              EXTRA_ENABLE_LANGUAGE_SWITCH: 'balanced' as const,
              EXTRA_LANGUAGE_SWITCH_ALLOWED_LANGUAGES: ['en-IN', 'hi-IN'],
              EXTRA_LANGUAGE_DETECTION_ALLOWED_LANGUAGES: ['en-IN', 'hi-IN'],
            }
          : {}),
      },
    });
  }, [lang]);
  useEffect(() => {
    startRef.current = start;
  }, [start]);

  const stop = useCallback(() => ExpoSpeechRecognitionModule.stop(), []);
  const cancel = useCallback(() => {
    if (probing.current) {
      finishProbe(false);
      return;
    }
    clearWatchdog();
    errored.current = true; // suppress the "end" callback after an abort
    stoppedOnPurpose.current = true;
    longRef.current = noLong();
    setLongMode(false);
    ExpoSpeechRecognitionModule.abort();
    setState('idle');
  }, []);

  /**
   * "Is anyone talking in the room?" — listens for about two seconds using the phone's ON-DEVICE recogniser
   * (nothing is sent anywhere) and resolves true if speech starts. Used before JARVIS speaks a reminder.
   * Resolves false when the mic is already in use, permission is missing, or the on-device model is absent.
   */
  const probe = useCallback(
    (ms = 2200) =>
      new Promise<boolean>((resolve) => {
        if (stateRef.current === 'listening' || probing.current) return resolve(false);
        probing.current = { resolve, timer: setTimeout(() => finishProbe(false), ms) };
        try {
          ExpoSpeechRecognitionModule.start({
            lang: 'en-IN',
            interimResults: true,
            continuous: false,
            requiresOnDeviceRecognition: true,
            androidIntentOptions: { EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS: 1500 },
          });
        } catch {
          finishProbe(false);
        }
      }),
    [],
  );

  return { state, partial, start, stop, cancel, probe, level, longMode };
}
