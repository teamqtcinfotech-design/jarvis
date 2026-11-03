import * as SecureStore from 'expo-secure-store';
import React, { createContext, useCallback, useContext, useEffect, useState } from 'react';

import { setVoiceLanguage } from './speech';

export interface Prefs {
  voiceLang: 'en-IN' | 'hi-IN';
  speakPrivateDetails: boolean;
  awayAfterMinutes: number;
  quietHoursEnabled: boolean;
  quietStartHour: number;
  quietEndHour: number;
}

const DEFAULTS: Prefs = {
  voiceLang: 'en-IN',
  speakPrivateDetails: true,
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
        setVoiceLanguage(loaded.voiceLang);
      } catch {
        // ignore corrupt prefs
      }
    });
  }, []);

  const update = useCallback((patch: Partial<Prefs>) => {
    setPrefs((prev) => {
      const next = { ...prev, ...patch };
      SecureStore.setItemAsync(KEY, JSON.stringify(next)).catch(() => undefined);
      if (patch.voiceLang) setVoiceLanguage(patch.voiceLang);
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
