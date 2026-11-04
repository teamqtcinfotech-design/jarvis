import * as SecureStore from 'expo-secure-store';
import React, { createContext, useCallback, useContext, useEffect, useState } from 'react';

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
};

const KEY = 'jarvis.prefs';

interface PrefsValue {
  prefs: Prefs;
  update: (patch: Partial<Prefs>) => void;
}

const PrefsContext = createContext<PrefsValue | null>(null);

export function PrefsProvider({ children }: { children: React.ReactNode }) {
  const [prefs, setPrefs] = useState<Prefs>(DEFAULTS);

  useEffect(() => {
    SecureStore.getItemAsync(KEY).then((raw) => {
      if (!raw) return;
      try {
        const loaded = { ...DEFAULTS, ...JSON.parse(raw) } as Prefs;
        setPrefs(loaded);
        setVoiceAccent(loaded.voiceAccent);
      } catch {
        // ignore corrupt prefs
      }
    });
  }, []);

  const update = useCallback((patch: Partial<Prefs>) => {
    setPrefs((prev) => {
      const next = { ...prev, ...patch };
      SecureStore.setItemAsync(KEY, JSON.stringify(next)).catch(() => undefined);
      if (patch.voiceAccent) setVoiceAccent(patch.voiceAccent);
      return next;
    });
  }, []);

  return <PrefsContext.Provider value={{ prefs, update }}>{children}</PrefsContext.Provider>;
}

export function usePrefs() {
  const ctx = useContext(PrefsContext);
  if (!ctx) throw new Error('usePrefs must be inside PrefsProvider');
  return ctx;
}
