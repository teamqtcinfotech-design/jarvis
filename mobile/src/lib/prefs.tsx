import * as SecureStore from 'expo-secure-store';
import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { AppState } from 'react-native';

import { api } from './api';
import { setPinnedLang } from './lang';
import { useSession } from './session';
import { setVoiceAccent } from './speech';

export interface Prefs {
  /** How the phone listens. 'auto' = Hindi and English together (switches by itself on Android 14+;
   *  on older phones the Indian-English listener, which also understands Hinglish). */
  listenLang: 'auto' | 'hi-IN';
  /** Voice for English replies: Hindi accent (default) or Indian English. Hindi replies always use the Hindi voice. */
  voiceAccent: 'hi-IN' | 'en-IN';
  /** The language JARVIS replies in: 'auto' = the language you just used (the server decides), or always one. */
  replyLanguage: 'auto' | 'hi' | 'en';
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
  /** Work routine (server-side scheduling uses it): office hours, weekly off (Monday = 0), fixed blocks. */
  officeStart: string;
  officeEnd: string;
  weeklyOff: number[];
  routineBlocks: RoutineBlock[];
  /** How early reminders speak, by kind, when the task itself did not say. */
  remindBeforeMeeting: number;
  remindBeforeCall: number;
  remindBeforeTask: number;
  dayReminderTime: string;
  /** The spoken morning briefing, raised by the server at this time. */
  briefingEnabled: boolean;
  briefingTime: string;
}

export interface RoutineBlock {
  label: string;
  start: string;
  end: string;
  days: number[];
}

const DEFAULTS: Prefs = {
  listenLang: 'auto',
  voiceAccent: 'hi-IN',
  replyLanguage: 'auto',
  speakPrivateDetails: true,
  privateAloudWhenLocked: false,
  awayAfterMinutes: 10,
  quietHoursEnabled: true,
  quietStartHour: 22,
  quietEndHour: 7,
  wakeWordEnabled: false,
  meetingMode: false,
  officeStart: '10:00',
  officeEnd: '19:00',
  weeklyOff: [6],
  routineBlocks: [],
  remindBeforeMeeting: 15,
  remindBeforeCall: 5,
  remindBeforeTask: 30,
  dayReminderTime: '09:30',
  briefingEnabled: true,
  briefingTime: '09:00',
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
  // When the user changed something here, a server pull that was already in flight must not put the old value
  // back (that is how "meeting mode" used to switch itself on again).
  const lastLocalChange = useRef(0);

  useEffect(() => {
    SecureStore.getItemAsync(KEY).then((raw) => {
      if (!raw) return;
      try {
        const loaded = { ...DEFAULTS, ...clean(JSON.parse(raw)) } as Prefs;
        setPrefs(loaded);
        setVoiceAccent(loaded.voiceAccent);
        setPinnedLang(loaded.replyLanguage);
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
      if (patch.replyLanguage) setPinnedLang(patch.replyLanguage);
      return next;
    });
  }, []);

  const update = useCallback((patch: Partial<Prefs>) => {
    lastLocalChange.current = Date.now();
    setPrefs((prev) => {
      const next = { ...prev, ...patch };
      SecureStore.setItemAsync(KEY, JSON.stringify(next)).catch(() => undefined);
      if (patch.voiceAccent) setVoiceAccent(patch.voiceAccent);
      if (patch.replyLanguage) setPinnedLang(patch.replyLanguage);
      return next;
    });
    const sentAt = Date.now();
    api<Partial<Prefs>>('/me/prefs/', { method: 'PATCH', body: patch })
      .then((r) => {
        if (sentAt >= lastLocalChange.current) applyServerPrefs(r.data); // the server's copy after our change
      })
      .catch(() => undefined); // offline: local copy still applies
  }, [applyServerPrefs]);

  // Keep in step with the server: on sign-in, whenever the app comes to the front, and every so often.
  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    const pull = () => {
      const startedAt = Date.now();
      return api<Partial<Prefs>>('/me/prefs/')
        .then((r) => {
          if (cancelled || startedAt < lastLocalChange.current) return; // older than a change made here
          applyServerPrefs(r.data);
        })
        .catch(() => undefined);
    };
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
