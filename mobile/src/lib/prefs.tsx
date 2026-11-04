import * as SecureStore from 'expo-secure-store';
import React, { createContext, useCallback, useContext, useEffect, useState } from 'react';
import { AppState } from 'react-native';

import { api } from './api';
import { useSession } from './session';
import { setVoiceAccent } from './speech';

export interface Prefs {
  /** How the phone listens. 'auto' = Hindi and English together (switches by itself on Android 14+;
   *  on older phones the Indian-English listener, which also understands Hinglish). */
  listenLang: 'auto' | 'hi-IN';
  /** Voice for English replies: Hindi accent (default) or Indian English. Hindi replies always use the Hindi voice. */
  voiceAccent: 'hi-IN' | 'en-IN';
  /** Speak private task details aloud while the app is open on screen. */
  speakPrivateDetails: boolean;
  /** Also speak them when the app is in the background or the phone is locked. Off: "you have a private reminder". */
  privateAloudWhenLocked: boolean;
  awayAfterMinutes: number;
  quietHoursEnabled: boolean;
  quietStartHour: number;
  quietEndHour: number;
  /** "Hey JARVIS" listening in the background (needs the one-time setup in Settings). */
  wakeWordEnabled: boolean;
  /** Meeting mode: JARVIS stays silent and vibrates only. */
  meetingMode: boolean;
}

const DEFAULTS: Prefs = {
  listenLang: 'auto',
  voiceAccent: 'hi-IN',
  speakPrivateDetails: true,
  privateAloudWhenLocked: false,
  awayAfterMinutes: 10,
  quietHoursEnabled: true,
  quietStartHour: 22,
  quietEndHour: 7,
  wakeWordEnabled: false,
  meetingMode: false,
};

const KEY = 'jarvis.prefs';
const SYNC_MS = 45_000;

interface PrefsValue {
  prefs: Prefs;
  /** Change settings from the app: saved locally, sent to the server (shared with the background listener). */
  update: (patch: Partial<Prefs>) => void;
  /** Settings that arrived from the server (JARVIS changed them by voice, or another device did). */
  applyServerPrefs: (data: Partial<Prefs>) => void;
}

const PrefsContext = createContext<PrefsValue | null>(null);

function clean(data: Partial<Prefs> | null | undefined): Partial<Prefs> {
  const out: Partial<Prefs> = {};
  if (!data) return out;
  for (const k of Object.keys(DEFAULTS) as (keyof Prefs)[]) {
    if (data[k] !== undefined && typeof data[k] === typeof DEFAULTS[k]) (out as any)[k] = data[k];
  }
  return out;
}

export function PrefsProvider({ children }: { children: React.ReactNode }) {
  const { user } = useSession();
  const [prefs, setPrefs] = useState<Prefs>(DEFAULTS);

  useEffect(() => {
    SecureStore.getItemAsync(KEY).then((raw) => {
      if (!raw) return;
      try {
        const loaded = { ...DEFAULTS, ...clean(JSON.parse(raw)) } as Prefs;
        setPrefs(loaded);
        setVoiceAccent(loaded.voiceAccent);
      } catch {
        // ignore corrupt prefs
      }
    });
  }, []);

  const applyServerPrefs = useCallback((data: Partial<Prefs>) => {
    const patch = clean(data);
    if (!Object.keys(patch).length) return;
    setPrefs((prev) => {
      const next = { ...prev, ...patch };
      SecureStore.setItemAsync(KEY, JSON.stringify(next)).catch(() => undefined);
      if (patch.voiceAccent) setVoiceAccent(patch.voiceAccent);
      return next;
    });
  }, []);

  const update = useCallback((patch: Partial<Prefs>) => {
    setPrefs((prev) => {
      const next = { ...prev, ...patch };
      SecureStore.setItemAsync(KEY, JSON.stringify(next)).catch(() => undefined);
      if (patch.voiceAccent) setVoiceAccent(patch.voiceAccent);
      return next;
    });
    api('/me/prefs/', { method: 'PATCH', body: patch }).catch(() => undefined); // offline: local copy still applies
  }, []);

  // Keep in step with the server: on sign-in, whenever the app comes to the front, and every so often.
  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    const pull = () =>
      api<Partial<Prefs>>('/me/prefs/')
        .then((r) => !cancelled && applyServerPrefs(r.data))
        .catch(() => undefined);
    const kick = setTimeout(pull, 300);
    const timer = setInterval(pull, SYNC_MS);
    const sub = AppState.addEventListener('change', (s) => s === 'active' && pull());
    return () => {
      cancelled = true;
      clearTimeout(kick);
      clearInterval(timer);
      sub.remove();
    };
  }, [user, applyServerPrefs]);

  return <PrefsContext.Provider value={{ prefs, update, applyServerPrefs }}>{children}</PrefsContext.Provider>;
}

export function usePrefs() {
  const ctx = useContext(PrefsContext);
  if (!ctx) throw new Error('usePrefs must be inside PrefsProvider');
  return ctx;
}
