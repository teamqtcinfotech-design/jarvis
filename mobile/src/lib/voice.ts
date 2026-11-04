import { ExpoSpeechRecognitionModule, useSpeechRecognitionEvent } from 'expo-speech-recognition';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Platform } from 'react-native';

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
}

/** Android's built-in recogniser ('auto' = Hindi + English, or 'hi-IN'). Audio never reaches our server. */
export function useVoiceCapture(lang: string, handlers: Handlers = {}) {
  const [state, setState] = useState<CaptureState>('idle');
  const [partial, setPartial] = useState('');
  const latest = useRef('');
  const finalText = useRef('');
  const errored = useRef(false);
  const handlersRef = useRef(handlers);
  useEffect(() => {
    handlersRef.current = handlers;
  });

  useSpeechRecognitionEvent('start', () => setState('listening'));
  useSpeechRecognitionEvent('result', (event) => {
    const text = event.results[0]?.transcript ?? '';
    latest.current = text;
    if (event.isFinal) finalText.current = text;
    setPartial(text);
  });
  useSpeechRecognitionEvent('end', () => {
    if (errored.current) return;
    setState('done');
    const text = (finalText.current || latest.current).trim();
    if (text) handlersRef.current.onFinal?.(text);
    else handlersRef.current.onError?.(ERROR_TEXT['no-speech'], 'no-speech');
  });
  useSpeechRecognitionEvent('error', (event) => {
    if (event.error === 'aborted') {
      setState('idle');
      return;
    }
    errored.current = true;
    setState('error');
    handlersRef.current.onError?.(ERROR_TEXT[event.error] ?? event.message ?? 'Speech recognition failed.', event.error);
  });

  const start = useCallback(async () => {
    stopSpeaking();
    setPartial('');
    latest.current = '';
    finalText.current = '';
    errored.current = false;
    const perm = await ExpoSpeechRecognitionModule.requestPermissionsAsync();
    if (!perm.granted) {
      errored.current = true;
      setState('error');
      handlersRef.current.onError?.(ERROR_TEXT['not-allowed'], 'not-allowed');
      return;
    }
    setState('listening');
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
      androidIntentOptions: {
        EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS: 1600,
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
    errored.current = true; // suppress the "end" callback after an abort
    ExpoSpeechRecognitionModule.abort();
    setState('idle');
  }, []);

  return { state, partial, start, stop, cancel };
}
