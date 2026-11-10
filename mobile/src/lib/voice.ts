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

/** Android's built-in recogniser ('auto' = Hindi + English, or 'hi-IN'). Audio never reaches our server. */
export function useVoiceCapture(lang: string, handlers: Handlers = {}) {
  const [state, setState] = useState<CaptureState>('idle');
  const [partial, setPartial] = useState('');
  const latest = useRef('');
  const finalText = useRef('');
  const errored = useRef(false);
  const stateRef = useRef<CaptureState>('idle');
  const probing = useRef<Probe | null>(null);
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
    setPartial(text);
  });
  useSpeechRecognitionEvent('volumechange', (event) => {
    if (probing.current) return;
    setLevel((event.value + 2) / 12); // the recogniser reports -2 (silence) .. 10 (loud)
  });
  useSpeechRecognitionEvent('end', () => {
    setLevel(0);
    if (probing.current) {
      finishProbe(false);
      return;
    }
    clearWatchdog();
    if (errored.current) return;
    setState('done');
    const text = (finalText.current || latest.current).trim();
    if (text) handlersRef.current.onFinal?.(text);
    else handlersRef.current.onError?.(ERROR_TEXT['no-speech'], 'no-speech');
  });
  useSpeechRecognitionEvent('error', (event) => {
    if (probing.current) {
      finishProbe(false);
      return;
    }
    clearWatchdog();
    if (event.error === 'aborted') {
      setState('idle');
      return;
    }
    errored.current = true;
    setState('error');
    handlersRef.current.onError?.(ERROR_TEXT[event.error] ?? event.message ?? 'Speech recognition failed.', event.error);
  });

  const start = useCallback(async () => {
    if (probing.current) finishProbe(false); // the user comes first
    stopSpeaking();
    setPartial('');
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
        ExpoSpeechRecognitionModule.abort();
      } catch {
        // ignore
      }
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
        // A natural pause mid-sentence is not the end of it.
        EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS: 1200,
        EXTRA_SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS: 1200,
        EXTRA_SPEECH_INPUT_MINIMUM_LENGTH_MILLIS: 8000,
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

  const stop = useCallback(() => ExpoSpeechRecognitionModule.stop(), []);
  const cancel = useCallback(() => {
    if (probing.current) {
      finishProbe(false);
      return;
    }
    clearWatchdog();
    errored.current = true; // suppress the "end" callback after an abort
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

  return { state, partial, start, stop, cancel, probe, level };
}
