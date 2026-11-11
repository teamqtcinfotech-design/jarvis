/**
 * The alert brain on the phone.
 *
 *  Reminder / incoming request arrives
 *     -> presence: with you | in meeting | face down | quiet hours | away
 *     -> SPEAK (with you) | VIBRATE silently, generic text (meeting / face down / quiet, urgent only) | HOLD
 *  When he is back with the phone: "While you were away: N alerts, M urgent."
 *  Urgent alerts repeat (5 and 15 minutes) until acknowledged.
 *
 * Background safety net: every upcoming reminder is also scheduled as an exact OS
 * notification, so nothing is lost if the app is closed or the phone is asleep.
 */
import * as Haptics from 'expo-haptics';
import * as Notifications from 'expo-notifications';
import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { AppState, Platform } from 'react-native';

import { ApiError, api, newIdempotencyKey } from './api';
import { dataChanged } from './bus';
import { addressHiOf, addressOf, mdWord, mdWordHi } from './names';
import { usePrefs } from './prefs';
import { PresenceState, usePresence } from './presence';
import { useSession } from './session';
import { useDashboard } from './useDashboard';
import { say } from './speech';
import type { Priority, Reminder, ServerAlert } from './types';
import { onPush, wakeConsumeOpenKey, wakeSupported } from './wake';

Notifications.setNotificationHandler({
  handleNotification: async () => {
    const active = AppState.currentState === 'active';
    // In the foreground the engine decides (speak / vibrate / hold); the OS stays quiet.
    return { shouldShowBanner: !active, shouldShowList: true, shouldPlaySound: !active, shouldSetBadge: false };
  },
});

const CHANNELS = { URGENT: 'jarvis-urgent', HIGH: 'jarvis-normal', NORMAL: 'jarvis-normal', LOW: 'jarvis-low' } as const;
const RANK: Record<Priority, number> = { URGENT: 0, HIGH: 1, NORMAL: 2, LOW: 3 };
const POLL_MS = 20_000;
const TICK_MS = 4_000;
const STALE_MS = 2 * 60_000;
const FOLLOWUP_MINUTES = [5, 15];

export interface EngineItem {
  key: string;
  source: 'reminder' | 'server';
  priority: Priority;
  title: string;
  body: string;
  spoken: string;
  isPrivate: boolean;
  kind?: string;
  /** Reminders: before / due / day / checkin / followup (what moment this reminder marks). */
  label?: string;
  ref?: Reminder['ref'];
  alertId?: string;
  alertKind?: ServerAlert['kind'];
  requestId?: string;
  fireAt: number;
  followups: number;
  concealed?: boolean;
  mode?: 'loud' | 'silent';
  /** Why it was kept back instead of spoken: the offer later says "during your meeting" rather than "while you were away". */
  heldFor?: 'missed' | 'away' | 'meeting' | 'facedown' | 'quiet';
}

/** "while you were away" / "during your meeting" …: one phrase for the whole batch, honest about the reason. */
function heldPhrase(items: EngineItem[]): { en: string; hi: string } {
  const reasons = new Set(items.map((i) => i.heldFor ?? 'away'));
  if (reasons.size === 1 && reasons.has('meeting')) return { en: 'during your meeting', hi: 'आपकी मीटिंग के दौरान' };
  if (reasons.size === 1 && reasons.has('quiet')) return { en: 'during quiet hours', hi: 'शांत समय में' };
  if (reasons.size === 1 && reasons.has('facedown')) return { en: 'while your phone was face down', hi: 'जब फ़ोन उल्टा रखा था' };
  return { en: 'while you were away', hi: 'जब आप दूर थे' };
}

/** "Sir, while you were away: … Shall I read them?" — built by the engine, asked by the conversation. */
export interface HeldOffer {
  id: number;
  count: number;
  text: string; // English, Roman letters (shown and spoken with the Indian voice)
  textHi: string; // Devanagari, for the Hindi voice
}

/** The one question JARVIS asks aloud right after an alert, and listens for the answer to. */
export interface AlertQuestion {
  kind: 'accept' | 'done' | 'seen';
  /** Appended to the spoken line; empty when the line itself already asks (a check-in). */
  en: string;
  hi: string;
}

/**
 * Which alerts get a question (kept tight, so JARVIS never nags): a request to Sir ("Accept it?"), a task or
 * meeting Sir gave Mansi ("Shall I tell Sir you've seen it?"), a reminder at its time ("Done, or remind you
 * later?"). Decisions, staff news and "starts in 15 minutes" are spoken only.
 */
export function questionFor(item: EngineItem, isMD: boolean, md: { en: string; hi: string } = { en: 'Sir', hi: 'सर' }): AlertQuestion | null {
  if (item.alertKind === 'NEW_REQUEST' && isMD && item.requestId) return { kind: 'accept', en: 'Accept it?', hi: 'मंज़ूर करूँ?' };
  if (item.alertKind === 'TASK_ASSIGNED' && !isMD && item.alertId) {
    return { kind: 'seen', en: `Shall I tell ${md.en} you've seen it?`, hi: `${md.hi} को बता दूँ कि आपने देख लिया?` };
  }
  if (item.source === 'reminder' && item.ref && item.kind !== 'MEETING' && !item.concealed) {
    if (item.label === 'checkin') return { kind: 'done', en: '', hi: '' }; // its own line already asks
    if (item.label === 'due' || item.label === 'day') return { kind: 'done', en: 'Done, or remind you later?', hi: 'हो गया, या बाद में याद दिलाऊँ?' };
  }
  return null;
}

/** The conversation takes an alert over: speaks it, asks its question and listens. Returns true when it did. */
export type AlertPresenter = (item: EngineItem, spoken: string) => boolean;

interface EngineValue {
  presence: ReturnType<typeof usePresence>;
  /** The conversation lends the engine its voice and ear for alerts that carry a question. */
  setPresenter: (presenter: AlertPresenter | null) => void;
  /** The answer he gave to the question (yes / no), carried out here: accept or decline, done or later, told or not. */
  answerQuestion: (item: EngineItem, question: AlertQuestion, yes: boolean) => Promise<void>;
  meetingMode: boolean;
  setMeetingMode: (on: boolean) => void;
  offer: HeldOffer | null;
  acceptOffer: () => void;
  declineOffer: () => void;
  /** The conversation owns the microphone; it lends the engine a 2-second "is anyone talking?" check. */
  setVoiceProbe: (probe: (() => Promise<boolean>) | null) => void;
  /** While he is talking with JARVIS, new alerts show quietly instead of speaking over him. */
  setTalking: (talking: boolean) => void;
  /** Clear every alert about one thing (e.g. a request he just accepted by voice). */
  acknowledgeByRef: (refType: 'request' | 'shared_task', refId: string) => Promise<void>;
  current: EngineItem | null;
  queued: number;
  held: EngineItem[];
  heldVisible: boolean;
  setHeldVisible: (v: boolean) => void;
  acknowledge: (item: EngineItem) => Promise<void>;
  acknowledgeAllHeld: () => Promise<void>;
  snooze: (item: EngineItem, minutes?: number) => void;
  complete: (item: EngineItem) => Promise<void>;
  decideRequest: (item: EngineItem, accept: boolean) => Promise<void>;
  reveal: () => void;
  refresh: () => Promise<void>;
  lastSync: number | null;
  notificationsGranted: boolean | null;
  upcomingCount: number;
}

const EngineContext = createContext<EngineValue | null>(null);

const osId = (key: string) => key.replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 120);

/* eslint-disable import/namespace -- AndroidImportance / AndroidNotificationVisibility are exported at runtime
   via `export *` in expo-notifications/build/index.js (verified); eslint-plugin-import cannot follow that file. */
async function ensureChannels() {
  if (Platform.OS !== 'android') return;
  await Notifications.setNotificationChannelAsync(CHANNELS.URGENT, {
    name: 'Urgent reminders',
    importance: Notifications.AndroidImportance.MAX,
    vibrationPattern: [0, 400, 200, 400, 200, 400],
    lockscreenVisibility: Notifications.AndroidNotificationVisibility.PRIVATE,
  });
  await Notifications.setNotificationChannelAsync(CHANNELS.NORMAL, {
    name: 'Reminders',
    importance: Notifications.AndroidImportance.HIGH,
    sound: null,
    vibrationPattern: [0, 250],
    lockscreenVisibility: Notifications.AndroidNotificationVisibility.PRIVATE,
  });
  await Notifications.setNotificationChannelAsync(CHANNELS.LOW, {
    name: 'Low priority',
    importance: Notifications.AndroidImportance.LOW,
    sound: null,
    enableVibrate: false,
    lockscreenVisibility: Notifications.AndroidNotificationVisibility.PRIVATE,
  });
}
/* eslint-enable import/namespace */

function fromReminder(r: Reminder): EngineItem {
  return {
    key: r.key,
    source: 'reminder',
    priority: r.priority,
    title: r.title,
    body: r.fire_at_label,
    spoken: r.spoken,
    isPrivate: r.is_private,
    kind: r.kind,
    label: r.label,
    ref: r.ref,
    fireAt: Date.parse(r.fire_at),
    followups: 0,
  };
}

function fromAlert(a: ServerAlert): EngineItem {
  return {
    key: `alert:${a.id}`,
    source: 'server',
    priority: a.priority,
    title: a.title,
    body: a.body,
    spoken: a.spoken_text,
    isPrivate: false,
    alertId: a.id,
    alertKind: a.kind,
    requestId: a.ref_type === 'request' ? a.ref_id : undefined,
    fireAt: Date.parse(a.created_at),
    followups: 0,
  };
}

function buzzSilently(urgent: boolean) {
  const pulses = urgent ? 3 : 1;
  for (let i = 0; i < pulses; i++) {
    setTimeout(() => Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Heavy).catch(() => undefined), i * 350);
  }
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** "Sir, your meeting starts in 15 minutes." -> "your meeting starts in 15 minutes." (the address is said once). */
function withoutAddress(text: string, address: string): string {
  const t = text.trim();
  const low = t.toLowerCase();
  const a = address.toLowerCase();
  if (a && (low.startsWith(`${a}, `) || low.startsWith(`${a} `))) return t.slice(address.length + 1).trim();
  return t;
}

/** Several things due at once are said as one sentence, not a burst of separate announcements. */
function combined(lines: string[], address: string): string {
  if (lines.length === 1) return lines[0];
  const parts = lines.map((l) => {
    const s = withoutAddress(l, address);
    return /[.!?]$/.test(s) ? s : `${s}.`;
  });
  return `${address}, ${lines.length} things. ${parts.join(' ')}`;
}

/** "2 private reminders and 1 request from your PA" in English and Hindi. */
function describeHeld(items: EngineItem[], isMD: boolean, md: { en: string; hi: string }): { en: string; hi: string } {
  const n = (f: (i: EngineItem) => boolean) => items.filter(f).length;
  const privateRem = n((i) => i.source === 'reminder' && i.isPrivate);
  const otherRem = n((i) => i.source === 'reminder' && !i.isPrivate);
  const requests = n((i) => i.alertKind === 'NEW_REQUEST');
  const assigned = n((i) => i.alertKind === 'TASK_ASSIGNED');
  const decided = n((i) => i.alertKind === 'REQUEST_DECIDED');
  const staff = n((i) => i.alertKind === 'STAFF_UPDATE');
  const other = items.length - privateRem - otherRem - requests - assigned - decided - staff;
  const en: string[] = [];
  const hi: string[] = [];
  if (privateRem) {
    en.push(plural(privateRem, 'private reminder'));
    hi.push(`${privateRem} प्राइवेट रिमाइंडर`);
  }
  if (otherRem) {
    en.push(plural(otherRem, 'reminder'));
    hi.push(`${otherRem} रिमाइंडर`);
  }
  if (requests) {
    en.push(`${plural(requests, 'request')} from ${isMD ? 'your PA' : md.en}`);
    hi.push(`${isMD ? 'आपकी PA' : md.hi} की ${requests} रिक्वेस्ट`);
  }
  if (assigned) {
    en.push(`${plural(assigned, 'task')} from ${md.en}`);
    hi.push(`${md.hi} के ${assigned} टास्क`);
  }
  if (decided) {
    en.push(`${plural(decided, 'answer')} from ${md.en}`);
    hi.push(`${md.hi} के ${decided} जवाब`);
  }
  if (staff) {
    en.push(plural(staff, 'staff update'));
    hi.push(`स्टाफ़ के ${staff} अपडेट`);
  }
  if (other > 0) {
    en.push(plural(other, 'update'));
    hi.push(`${other} अपडेट`);
  }
  const join = (parts: string[], and: string) => (parts.length > 1 ? `${parts.slice(0, -1).join(', ')} ${and} ${parts[parts.length - 1]}` : parts[0] ?? '');
  return { en: join(en, 'and'), hi: join(hi, 'और') };
}

/** Where an alert points: the request or task it is about. */
function refOf(item: EngineItem): { type: string; id: string } | null {
  if (item.requestId) return { type: 'request', id: item.requestId };
  if (item.ref) return { type: 'shared_task', id: item.ref.id };
  return null;
}

export function AlertEngineProvider({ children }: { children: React.ReactNode }) {
  const { user } = useSession();
  const { prefs, update: updatePrefs } = usePrefs();
  const { data: dash } = useDashboard();
  const address = addressOf(user);
  const addressHi = addressHiOf(user);
  const md = { en: mdWord(dash), hi: mdWordHi(dash) };

  // Meeting mode is a shared setting (JARVIS can switch it by voice, even from the background listener).
  // A meeting in his diary also counts as "in a meeting"; switching the mode OFF during one (by hand or by
  // voice) overrides the diary until that meeting ends, otherwise the switch would seem to do nothing.
  const meetingMode = prefs.meetingMode;
  const [busyWindows, setBusyWindows] = useState<{ start: string; end: string }[]>([]);
  const [busyOverrideUntil, setBusyOverrideUntil] = useState(0);
  const busyWindowsRef = useRef(busyWindows);
  useEffect(() => {
    busyWindowsRef.current = busyWindows;
  }, [busyWindows]);
  const overrideDiaryNow = useCallback(() => {
    const now = Date.now();
    const current = busyWindowsRef.current.find((w) => Date.parse(w.start) <= now && now < Date.parse(w.end));
    setBusyOverrideUntil(current ? Date.parse(current.end) : 0);
  }, []);
  const setMeetingMode = useCallback(
    (on: boolean) => {
      updatePrefs({ meetingMode: on });
      if (on) setBusyOverrideUntil(0);
      else overrideDiaryNow();
    },
    [updatePrefs, overrideDiaryNow],
  );
  // Switched off by voice ("meeting mode band karo") or on another device: same override.
  const prevMeetingMode = useRef(meetingMode);
  useEffect(() => {
    if (prevMeetingMode.current && !meetingMode) overrideDiaryNow();
    prevMeetingMode.current = meetingMode;
  }, [meetingMode, overrideDiaryNow]);
  const presence = usePresence({
    awayAfterMinutes: prefs.awayAfterMinutes,
    meetingMode,
    busyWindows,
    busyOverrideUntil,
    quietHoursEnabled: prefs.quietHoursEnabled,
    quietStartHour: prefs.quietStartHour,
    quietEndHour: prefs.quietEndHour,
  });

  const [current, setCurrentState] = useState<EngineItem | null>(null);
  const [queued, setQueued] = useState(0);
  const [held, setHeldState] = useState<EngineItem[]>([]);
  const [heldVisible, setHeldVisible] = useState(false);
  const [lastSync, setLastSync] = useState<number | null>(null);
  const [notificationsGranted, setNotificationsGranted] = useState<boolean | null>(null);
  const [upcomingCount, setUpcomingCount] = useState(0);
  const [offer, setOffer] = useState<HeldOffer | null>(null);
  const offerSeq = useRef(0);
  const voiceProbe = useRef<(() => Promise<boolean>) | null>(null);
  const talkingRef = useRef(false);
  const presenterRef = useRef<AlertPresenter | null>(null);
  const isMD = user?.role === 'MD';

  const currentRef = useRef<EngineItem | null>(null);
  const queueRef = useRef<EngineItem[]>([]);
  const heldRef = useRef<EngineItem[]>([]);
  const announcedHeld = useRef(0);
  const handled = useRef(new Set<string>());
  const remindersRef = useRef<Reminder[]>([]);
  const alertsRef = useRef<ServerAlert[]>([]);
  const followups = useRef(new Map<string, { item: EngineItem; at: number }>());
  const snoozed = useRef(new Map<string, { item: EngineItem; until: number }>());
  const scheduledSig = useRef('');
  const presenceRef = useRef<PresenceState>(presence.state);
  const prefsRef = useRef(prefs);
  useEffect(() => {
    prefsRef.current = prefs;
  }, [prefs]);

  const setCurrent = useCallback((item: EngineItem | null) => {
    currentRef.current = item;
    setCurrentState(item);
  }, []);

  const setHeld = useCallback((items: EngineItem[]) => {
    heldRef.current = items;
    setHeldState(items);
  }, []);

  const advance = useCallback(() => {
    const next = queueRef.current.shift() ?? null;
    setQueued(queueRef.current.length);
    setCurrent(next);
  }, [setCurrent]);

  // Private details are spoken only while the app is open on screen (and allowed), unless the MD
  // has also allowed it when the phone is locked or the app is in the background.
  const privateAloud = useCallback(() => {
    const p = prefsRef.current;
    if (!p.speakPrivateDetails) return false;
    return AppState.currentState === 'active' || p.privateAloudWhenLocked;
  }, []);

  const spokenFor = useCallback(
    (item: EngineItem) => (item.isPrivate && !privateAloud() ? `${address}, you have a private reminder.` : item.spoken),
    [address, privateAloud],
  );

  // Told once: whatever the app speaks or shows is reported to the server, so the background part never says
  // it again (and the other way round). Best effort; the next report retries nothing, the server list is the truth.
  const tell = useCallback((items: EngineItem[]) => {
    const keys = items.filter((i) => i.source === 'reminder').map((i) => i.key);
    const ids = items.map((i) => i.alertId).filter((id): id is string => !!id);
    if (!keys.length && !ids.length) return;
    api('/told/', { method: 'POST', body: { keys, ids } }).catch(() => undefined);
  }, []);

  /** Put an item on screen (banner now, or queued behind the current one) and register its urgent follow-ups. */
  const show = useCallback(
    (item: EngineItem, mode: 'loud' | 'silent') => {
      const shown: EngineItem = { ...item, mode, concealed: mode === 'silent' && item.isPrivate };
      if (!currentRef.current) setCurrent(shown);
      else {
        queueRef.current.push(shown);
        queueRef.current.sort((a, b) => RANK[a.priority] - RANK[b.priority]);
        setQueued(queueRef.current.length);
      }
      if (item.priority === 'URGENT' && item.followups < FOLLOWUP_MINUTES.length) {
        followups.current.set(item.key, {
          item: { ...item, followups: item.followups + 1 },
          at: Date.now() + FOLLOWUP_MINUTES[item.followups] * 60_000,
        });
      }
    },
    [setCurrent],
  );

  const deliver = useCallback(
    (item: EngineItem, mode: 'loud' | 'silent') => {
      show(item, mode);
      if (mode === 'loud') {
        Haptics.notificationAsync(
          item.priority === 'URGENT' ? Haptics.NotificationFeedbackType.Warning : Haptics.NotificationFeedbackType.Success,
        ).catch(() => undefined);
        // An alert with a question is handed to the conversation: it says the line, asks, and listens once.
        const presenter = presenterRef.current;
        const asked = !!presenter && !!questionFor(item, isMD, md) && presenter(item, spokenFor(item));
        if (!asked) say(spokenFor(item));
      } else {
        buzzSilently(item.priority === 'URGENT');
      }
      tell([item]);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps -- md is a fresh object each render; its two words are what matter
    [show, spokenFor, tell, isMD, md.en, md.hi],
  );

  /**
   * He tapped this alert's notification (or JARVIS is bringing it to him on return): put it on screen and start
   * the conversation on it at once, speak it, ask its question, listen for the answer.
   */
  const presentItem = useCallback(
    (item: EngineItem) => {
      handled.current.add(item.key);
      snoozed.current.delete(item.key);
      followups.current.delete(item.key);
      setHeld(heldRef.current.filter((h) => h.key !== item.key));
      queueRef.current = queueRef.current.filter((q) => q.key !== item.key);
      setQueued(queueRef.current.length);
      if (currentRef.current && currentRef.current.key !== item.key) queueRef.current.unshift(currentRef.current);
      setCurrent({ ...item, mode: 'silent' });
      const presenter = presenterRef.current;
      if (!presenter || !presenter(item, spokenFor(item))) say(spokenFor(item), { interrupt: true });
      tell([item]);
    },
    [setCurrent, setHeld, spokenFor, tell],
  );

  /** Several things at the same moment: shown one by one, said as ONE sentence. */
  const deliverTogether = useCallback(
    (items: EngineItem[]) => {
      if (items.length === 1) return deliver(items[0], 'loud');
      items.forEach((item) => show(item, 'loud'));
      Haptics.notificationAsync(
        items.some((i) => i.priority === 'URGENT') ? Haptics.NotificationFeedbackType.Warning : Haptics.NotificationFeedbackType.Success,
      ).catch(() => undefined);
      say(combined(items.map(spokenFor), address));
      tell(items);
    },
    [deliver, show, spokenFor, tell, address],
  );

  const hold = useCallback(
    (item: EngineItem, why: NonNullable<EngineItem['heldFor']>) => {
      if (heldRef.current.some((h) => h.key === item.key)) return;
      const next = [...heldRef.current, { ...item, heldFor: why }].sort(
        (a, b) => RANK[a.priority] - RANK[b.priority] || a.fireAt - b.fireAt,
      );
      setHeld(next);
    },
    [setHeld],
  );

  // He is back (picked the phone up, opened the app) and alerts were held: offer to read them.
  // The engine only builds the offer; the conversation asks it aloud and listens for the answer.
  const offerHeldIfBack = useCallback(() => {
    if (presenceRef.current !== 'present') return;
    const items = heldRef.current;
    if (items.length <= announcedHeld.current) return;
    announcedHeld.current = items.length;
    const what = describeHeld(items, isMD, md);
    const when = heldPhrase(items);
    const them = items.length === 1 ? 'it' : 'them';
    offerSeq.current += 1;
    setOffer({
      id: offerSeq.current,
      count: items.length,
      text: `${address}, ${when.en}: ${what.en}. Shall I read ${them}?`,
      textHi: `${addressHi}, ${when.hi}: ${what.hi}। पढ़ कर सुनाऊँ?`,
    });
    Haptics.notificationAsync(Haptics.NotificationFeedbackType.Warning).catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- md is a fresh object each render; its two words are what matter
  }, [address, addressHi, isMD, md.en, md.hi]);

  const setVoiceProbe = useCallback((probe: (() => Promise<boolean>) | null) => {
    voiceProbe.current = probe;
  }, []);

  const setPresenter = useCallback((presenter: AlertPresenter | null) => {
    presenterRef.current = presenter;
  }, []);

  const setTalking = useCallback((talking: boolean) => {
    talkingRef.current = talking;
  }, []);

  /**
   * Decide what happens to one item that is due now. Returns 'loud' when it should be spoken (the caller speaks
   * everything due at this moment as one sentence), or handles it itself (silent banner, or held for later).
   */
  const route = useCallback(
    (item: EngineItem, state: PresenceState, now: number): 'loud' | null => {
      handled.current.add(item.key);
      const missed = item.source === 'reminder' && item.followups === 0 && now - item.fireAt > STALE_MS;
      if (missed) {
        hold(item, 'missed'); // fired while the app was closed or the phone asleep
        return null;
      }
      if (item.priority === 'LOW' && item.source === 'server') {
        // Small news ("Mansi started a task"): never spoken, never vibrates, never offered; it is in the lists.
        tell([item]);
        return null;
      }
      if (state === 'present') {
        // Mid-conversation with JARVIS: show it quietly, never speak over him (he can act on it by voice).
        if (talkingRef.current) {
          deliver(item, 'silent');
          return null;
        }
        return 'loud';
      }
      if (state === 'away') {
        hold(item, 'away');
        return null;
      }
      // meeting / face down / quiet hours: only urgent interrupts, and silently
      if (item.priority === 'URGENT') deliver(item, 'silent');
      else hold(item, state);
      return null;
    },
    [deliver, hold, tell],
  );

  /** Everything due right now that may be spoken: one voice check for the room, then one sentence. */
  const speakNow = useCallback(
    (items: EngineItem[]) => {
      const probe = voiceProbe.current;
      const go = (voices: boolean) => {
        if (voices) {
          // Someone is talking in the room: he is here, so it goes on screen with a vibration, just not aloud.
          // (It is NOT "while you were away": he can see it, and it is never offered again.)
          items.forEach((item) => deliver(item, 'silent'));
          return;
        }
        deliverTogether(items);
      };
      if (!probe) go(false);
      else probe().catch(() => false).then(go);
    },
    [deliver, deliverTogether],
  );

  const tick = useCallback(() => {
    // While JARVIS is not on screen, the phone's always-on part (ReminderWatcher) delivers reminders and news and
    // reports them as told. This JavaScript side keeps running in the background too, so without this guard the same
    // reminder was said twice: once by the watcher and once from here. Everything is re-checked on return
    // (the "active" handler below polls first, so anything the watcher already said is skipped).
    if (wakeSupported && AppState.currentState !== 'active') return;
    const now = Date.now();
    const due: EngineItem[] = [];
    for (const r of remindersRef.current) {
      if (handled.current.has(r.key) || Date.parse(r.fire_at) > now) continue;
      if (r.told_at) {
        handled.current.add(r.key); // the background part already said it while the app was closed
        continue;
      }
      due.push(fromReminder(r));
    }
    for (const a of alertsRef.current) {
      const key = `alert:${a.id}`;
      if (handled.current.has(key)) continue;
      if (!a.fresh) {
        handled.current.add(key); // told already, or old news: it stays in the lists, it is not announced
        continue;
      }
      due.push(fromAlert(a));
    }
    for (const [k, s] of snoozed.current) {
      if (s.until <= now) {
        snoozed.current.delete(k);
        due.push({ ...s.item, followups: Math.max(s.item.followups, 0) });
      }
    }
    for (const [k, f] of followups.current) {
      if (f.at <= now) {
        followups.current.delete(k);
        // Already shown? Re-alert only if still not acknowledged.
        if (currentRef.current?.key === k) queueRef.current = queueRef.current.filter((q) => q.key !== k);
        due.push(f.item);
      }
    }
    due.sort((a, b) => RANK[a.priority] - RANK[b.priority] || a.fireAt - b.fireAt);
    const loud: EngineItem[] = [];
    for (const item of due) {
      if (currentRef.current?.key === item.key) setCurrent(null);
      if (route(item, presenceRef.current, now) === 'loud') loud.push(item);
    }
    if (loud.length) speakNow(loud);
    offerHeldIfBack();
  }, [route, speakNow, offerHeldIfBack, setCurrent]);

  /** The server list is the truth: anything no longer in it (task finished, request decided) leaves the screen. */
  const pruneGone = useCallback(
    (live: Set<string>) => {
      const stays = (i: EngineItem) => live.has(i.key) || snoozed.current.has(i.key) || followups.current.has(i.key);
      queueRef.current = queueRef.current.filter(stays);
      setQueued(queueRef.current.length);
      const remaining = heldRef.current.filter(stays);
      if (remaining.length !== heldRef.current.length) {
        setHeld(remaining);
        announcedHeld.current = Math.min(announcedHeld.current, remaining.length);
      }
      if (currentRef.current && !stays(currentRef.current)) advance();
    },
    [advance, setHeld],
  );

  const scheduleOs = useCallback(async (reminders: Reminder[]) => {
    const now = Date.now();
    const upcoming = reminders.filter((r) => Date.parse(r.fire_at) > now + 5000).slice(0, 40);
    setUpcomingCount(upcoming.length);
    // With the background part on this phone, it delivers reminders while the app is closed (and reports them
    // as told). A second alarm from here would only make a second notification for the same reminder.
    if (wakeSupported) {
      if (scheduledSig.current !== 'off') {
        scheduledSig.current = 'off';
        await Notifications.cancelAllScheduledNotificationsAsync().catch(() => undefined);
      }
      return;
    }
    const sig = upcoming.map((r) => r.key + r.fire_at).join('|');
    if (sig === scheduledSig.current) return;
    scheduledSig.current = sig;
    await Notifications.cancelAllScheduledNotificationsAsync();
    for (const r of upcoming) {
      await Notifications.scheduleNotificationAsync({
        identifier: osId(r.key),
        content: {
          // Many phones show notification text on the lock screen, so a private reminder never puts its title there.
          title: r.is_private ? 'JARVIS' : 'JARVIS · Reminder',
          body: r.is_private ? 'You have a private reminder. Open JARVIS to see it.' : r.title,
          data: { key: r.key },
          sound: r.priority === 'URGENT', // the phone's default notification sound, urgent only
        },
        trigger: {
          type: Notifications.SchedulableTriggerInputTypes.DATE,
          date: new Date(r.fire_at),
          channelId: CHANNELS[r.priority],
        },
      });
    }
  }, []);

  const poll = useCallback(async () => {
    try {
      const [rem, alerts] = await Promise.all([
        api<{ reminders: Reminder[]; busy_windows: { start: string; end: string }[] }>('/reminders/?hours=48'),
        api<{ results: ServerAlert[] }>('/alerts/'),
      ]);
      remindersRef.current = rem.data.reminders;
      alertsRef.current = alerts.data.results;
      setBusyWindows(rem.data.busy_windows);
      setLastSync(Date.now());
      pruneGone(new Set([...rem.data.reminders.map((r) => r.key), ...alerts.data.results.map((a) => `alert:${a.id}`)]));
      await scheduleOs(rem.data.reminders).catch(() => undefined);
      tick();
    } catch {
      // Offline: keep last known data; OS notifications already scheduled still fire.
    }
  }, [scheduleOs, tick, pruneGone]);

  // Permissions + channels
  useEffect(() => {
    (async () => {
      await ensureChannels().catch(() => undefined);
      const res = await Notifications.requestPermissionsAsync().catch(() => null);
      setNotificationsGranted(Boolean(res?.granted));
    })();
  }, []);

  // Polling loop while the app is in the foreground
  useEffect(() => {
    const kick = setTimeout(poll, 0);
    let pollTimer = setInterval(poll, POLL_MS);
    const tickTimer = setInterval(tick, TICK_MS);
    const sub = AppState.addEventListener('change', (s) => {
      if (s === 'active') {
        poll();
        clearInterval(pollTimer);
        pollTimer = setInterval(poll, POLL_MS);
      } else {
        clearInterval(pollTimer);
      }
    });
    const unsub = dataChanged.subscribe(() => {
      poll();
    });
    const offPush = onPush(() => poll()); // a live push while the app is open: fetch it now, not in 20 s
    return () => {
      clearTimeout(kick);
      clearInterval(pollTimer);
      clearInterval(tickTimer);
      sub.remove();
      unsub();
      offPush();
    };
  }, [poll, tick]);

  // Presence changes (e.g. picked the phone up, meeting ended) can release held alerts.
  useEffect(() => {
    const prev = presenceRef.current;
    presenceRef.current = presence.state;
    if (prev !== 'present' && presence.state === 'present') offerHeldIfBack();
  }, [presence.state, offerHeldIfBack]);

  // Tapping a notification opens the app on that alert, and JARVIS starts talking about it at once.
  const markActive = presence.markActive;
  const itemForKey = useCallback((key: string): EngineItem | null => {
    const r = remindersRef.current.find((x) => x.key === key);
    if (r) return fromReminder(r);
    const a = alertsRef.current.find((x) => `alert:${x.id}` === key);
    return a ? fromAlert(a) : null;
  }, []);
  useEffect(() => {
    // Notifications scheduled by the app itself (phones without the always-on part).
    const sub = Notifications.addNotificationResponseReceivedListener((response) => {
      const key = response.notification.request.content.data?.key as string | undefined;
      const item = key ? itemForKey(key) : null;
      if (!item) return;
      markActive();
      presentItem(item);
    });
    return () => sub.remove();
  }, [markActive, itemForKey, presentItem]);
  useEffect(() => {
    // Notifications from the always-on part: the key rides on the intent that brought the app up. The app is
    // told on return (and once at start), fetches the latest lists, then opens the conversation on that item.
    if (!wakeSupported) return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const check = () => {
      const key = wakeConsumeOpenKey();
      if (!key) return;
      markActive();
      poll()
        .catch(() => undefined)
        .then(() => {
          const item = itemForKey(key);
          if (item) presentItem(item);
        });
    };
    timer = setTimeout(check, 400);
    const sub = AppState.addEventListener('change', (s) => {
      if (s !== 'active') return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(check, 300);
    });
    return () => {
      if (timer) clearTimeout(timer);
      sub.remove();
    };
  }, [markActive, itemForKey, presentItem, poll]);

  const clearKey = useCallback(
    (item: EngineItem) => {
      followups.current.delete(item.key);
      snoozed.current.delete(item.key);
      queueRef.current = queueRef.current.filter((q) => q.key !== item.key);
      setQueued(queueRef.current.length);
      const remaining = heldRef.current.filter((h) => h.key !== item.key);
      setHeld(remaining);
      announcedHeld.current = Math.min(announcedHeld.current, remaining.length);
      if (currentRef.current?.key === item.key) advance();
      const id = osId(item.key);
      Notifications.cancelScheduledNotificationAsync(id).catch(() => undefined);
      Notifications.dismissNotificationAsync(id).catch(() => undefined);
    },
    [advance, setHeld],
  );

  const acknowledge = useCallback(
    async (item: EngineItem) => {
      clearKey(item);
      try {
        if (item.source === 'reminder') await api('/reminders/ack/', { method: 'POST', body: { key: item.key } });
        else if (item.alertId) await api(`/alerts/${item.alertId}/ack/`, { method: 'POST' });
      } catch {
        // Stays acknowledged locally for this session; the server copy is retried on the next ack.
      }
    },
    [clearKey],
  );

  const acknowledgeAllHeld = useCallback(async () => {
    const items = [...heldRef.current];
    for (const item of items) await acknowledge(item);
    setHeldVisible(false);
  }, [acknowledge]);

  // Offered once, then counted as seen: read aloud or not, they stay in his lists and are never offered again.
  const acceptOffer = useCallback(() => {
    setOffer(null);
    const items = [...heldRef.current];
    if (items.length) say(combined(items.map(spokenFor), address)); // one sentence, the address once
    acknowledgeAllHeld().catch(() => undefined);
  }, [spokenFor, acknowledgeAllHeld, address]);

  const declineOffer = useCallback(() => {
    setOffer(null);
    acknowledgeAllHeld().catch(() => undefined);
  }, [acknowledgeAllHeld]);

  const acknowledgeByRef = useCallback(
    async (refType: 'request' | 'shared_task', refId: string) => {
      const hit = (item: EngineItem) => {
        const r = refOf(item);
        return !!r && r.type === refType && r.id === refId;
      };
      const items = [currentRef.current, ...queueRef.current, ...heldRef.current].filter((i): i is EngineItem => !!i && hit(i));
      for (const item of items) await acknowledge(item);
    },
    [acknowledge],
  );

  const snooze = useCallback(
    (item: EngineItem, minutes = 10) => {
      followups.current.delete(item.key);
      queueRef.current = queueRef.current.filter((q) => q.key !== item.key);
      setHeld(heldRef.current.filter((h) => h.key !== item.key));
      if (currentRef.current?.key === item.key) advance();
      snoozed.current.set(item.key, { item: { ...item, followups: 0, fireAt: Date.now() }, until: Date.now() + minutes * 60_000 });
    },
    [advance, setHeld],
  );

  const complete = useCallback(
    async (item: EngineItem) => {
      if (!item.ref) return acknowledge(item);
      const path = item.ref.scope === 'private' ? `/private-tasks/${item.ref.id}/complete/` : `/shared-tasks/${item.ref.id}/complete/`;
      try {
        const res = await api(path, { method: 'POST', body: { expected_version: item.ref.version } });
        await acknowledge(item);
        say(res.status === 202 ? `${address}, sent to ${md.en} for approval.` : `Done, ${address}.`, { interrupt: true });
        dataChanged.emit();
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          say(`${address}, this task changed. I've refreshed it.`, { interrupt: true });
          await acknowledge(item);
          dataChanged.emit();
        } else {
          say(`Sorry ${address}, I couldn't reach the server. Please try again.`, { interrupt: true });
        }
      }
    },
    [acknowledge, address, md.en],
  );

  const decideRequest = useCallback(
    async (item: EngineItem, accept: boolean) => {
      if (!item.requestId) return acknowledge(item);
      try {
        await api(`/requests/${item.requestId}/${accept ? 'accept' : 'decline'}/`, {
          method: 'POST',
          body: {},
          idempotencyKey: newIdempotencyKey(),
        });
        say(accept ? `Accepted, ${address}. It's on your list.` : `Declined, ${address}. I've informed them.`, { interrupt: true });
      } catch (e) {
        const msg = e instanceof ApiError ? e.message : 'Please try again.';
        say(`${address}, ${msg}`, { interrupt: true });
      }
      await acknowledge(item);
      dataChanged.emit();
    },
    [acknowledge, address],
  );

  const reveal = useCallback(() => {
    if (currentRef.current) setCurrent({ ...currentRef.current, concealed: false });
  }, [setCurrent]);

  // What his yes or no to the alert's question does. Each path says one short line and clears the banner.
  const answerQuestion = useCallback(
    async (item: EngineItem, question: AlertQuestion, yes: boolean) => {
      if (question.kind === 'accept') return decideRequest(item, yes);
      if (question.kind === 'done') {
        if (yes) return complete(item);
        snooze(item, 15);
        say(`Okay ${address}, I'll remind you in 15 minutes.`, { interrupt: true });
        return;
      }
      // 'seen': yes = the MD is told the PA has seen it; no = it simply leaves the screen.
      clearKey(item);
      try {
        if (item.alertId) await api(`/alerts/${item.alertId}/ack/`, { method: 'POST', body: { seen: yes } });
        if (yes) say(`Told ${md.en}, ${address}.`, { interrupt: true });
      } catch {
        say(`Sorry ${address}, I couldn't reach the server.`, { interrupt: true });
      }
    },
    [decideRequest, complete, snooze, clearKey, address, md.en],
  );

  const value: EngineValue = {
    presence,
    setPresenter,
    answerQuestion,
    meetingMode,
    setMeetingMode,
    offer,
    acceptOffer,
    declineOffer,
    setVoiceProbe,
    setTalking,
    acknowledgeByRef,
    current,
    queued,
    held,
    heldVisible,
    setHeldVisible,
    acknowledge,
    acknowledgeAllHeld,
    snooze,
    complete,
    decideRequest,
    reveal,
    refresh: poll,
    lastSync,
    notificationsGranted,
    upcomingCount,
  };

  return <EngineContext.Provider value={value}>{children}</EngineContext.Provider>;
}

export function useAlertEngine() {
  const ctx = useContext(EngineContext);
  if (!ctx) throw new Error('useAlertEngine must be inside AlertEngineProvider');
  return ctx;
}
