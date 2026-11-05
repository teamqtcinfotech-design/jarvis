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
import { usePrefs } from './prefs';
import { PresenceState, usePresence } from './presence';
import { useSession } from './session';
import { say } from './speech';
import type { Priority, Reminder, ServerAlert } from './types';
import { onPush } from './wake';

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
  ref?: Reminder['ref'];
  alertId?: string;
  alertKind?: ServerAlert['kind'];
  requestId?: string;
  fireAt: number;
  followups: number;
  concealed?: boolean;
  mode?: 'loud' | 'silent';
}

/** "Sir, while you were away: … Shall I read them?" — built by the engine, asked by the conversation. */
export interface HeldOffer {
  id: number;
  count: number;
  text: string; // English, Roman letters (shown and spoken with the Indian voice)
  textHi: string; // Devanagari, for the Hindi voice
}

interface EngineValue {
  presence: ReturnType<typeof usePresence>;
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
    sound: 'default',
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

/** "2 private reminders and 1 request from your PA" in English and Hindi. */
function describeHeld(items: EngineItem[], isMD: boolean): { en: string; hi: string } {
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
    en.push(`${plural(requests, 'request')} from ${isMD ? 'your PA' : 'Sir'}`);
    hi.push(`${isMD ? 'आपकी PA' : 'सर'} की ${requests} रिक्वेस्ट`);
  }
  if (assigned) {
    en.push(`${plural(assigned, 'task')} from Sir`);
    hi.push(`सर के ${assigned} टास्क`);
  }
  if (decided) {
    en.push(`${plural(decided, 'answer')} from Sir`);
    hi.push(`सर के ${decided} जवाब`);
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
  const address = user?.role === 'MD' ? 'Sir' : user?.name ?? '';

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

  const deliver = useCallback(
    (item: EngineItem, mode: 'loud' | 'silent') => {
      const shown: EngineItem = { ...item, mode, concealed: mode === 'silent' && item.isPrivate };
      if (!currentRef.current) setCurrent(shown);
      else {
        queueRef.current.push(shown);
        queueRef.current.sort((a, b) => RANK[a.priority] - RANK[b.priority]);
        setQueued(queueRef.current.length);
      }
      if (mode === 'loud') {
        Haptics.notificationAsync(
          item.priority === 'URGENT' ? Haptics.NotificationFeedbackType.Warning : Haptics.NotificationFeedbackType.Success,
        ).catch(() => undefined);
        say(spokenFor(item));
      } else {
        buzzSilently(item.priority === 'URGENT');
      }
      if (item.priority === 'URGENT' && item.followups < FOLLOWUP_MINUTES.length) {
        followups.current.set(item.key, {
          item: { ...item, followups: item.followups + 1 },
          at: Date.now() + FOLLOWUP_MINUTES[item.followups] * 60_000,
        });
      }
    },
    [spokenFor, setCurrent],
  );

  const hold = useCallback(
    (item: EngineItem) => {
      if (heldRef.current.some((h) => h.key === item.key)) return;
      const next = [...heldRef.current, item].sort((a, b) => RANK[a.priority] - RANK[b.priority] || a.fireAt - b.fireAt);
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
    const what = describeHeld(items, isMD);
    const sir = isMD ? 'Sir' : address;
    const them = items.length === 1 ? 'it' : 'them';
    offerSeq.current += 1;
    setOffer({
      id: offerSeq.current,
      count: items.length,
      text: `${sir}, while you were away: ${what.en}. Shall I read ${them}?`,
      textHi: `${isMD ? 'सर' : address}, जब आप दूर थे: ${what.hi}। पढ़ कर सुनाऊँ?`,
    });
    Haptics.notificationAsync(Haptics.NotificationFeedbackType.Warning).catch(() => undefined);
  }, [address, isMD]);

  const setVoiceProbe = useCallback((probe: (() => Promise<boolean>) | null) => {
    voiceProbe.current = probe;
  }, []);

  const setTalking = useCallback((talking: boolean) => {
    talkingRef.current = talking;
  }, []);

  const route = useCallback(
    (item: EngineItem, state: PresenceState, now: number) => {
      handled.current.add(item.key);
      const missed = item.source === 'reminder' && item.followups === 0 && now - item.fireAt > STALE_MS;
      if (missed) return hold(item); // fired while the app was closed or the phone asleep
      if (item.priority === 'LOW' && item.source === 'server') {
        return state === 'present' ? deliver(item, 'silent') : hold(item);
      }
      if (state === 'present') {
        // Mid-conversation with JARVIS: show it quietly, never speak over him (he can act on it by voice).
        if (talkingRef.current) return deliver(item, 'silent');
        // With you — but is someone talking in the room? Then it is a meeting: vibrate, hold the words.
        const probe = voiceProbe.current;
        if (!probe) return deliver(item, 'loud');
        probe()
          .catch(() => false)
          .then((voices) => {
            if (!voices) return deliver(item, 'loud');
            deliver(item, 'silent');
            hold(item);
          });
        return;
      }
      if (state === 'away') return hold(item);
      // meeting / face down / quiet hours: only urgent interrupts, and silently
      return item.priority === 'URGENT' ? deliver(item, 'silent') : hold(item);
    },
    [deliver, hold],
  );

  const tick = useCallback(() => {
    const now = Date.now();
    const due: EngineItem[] = [];
    for (const r of remindersRef.current) {
      if (!handled.current.has(r.key) && Date.parse(r.fire_at) <= now) due.push(fromReminder(r));
    }
    for (const a of alertsRef.current) {
      const key = `alert:${a.id}`;
      if (!handled.current.has(key)) due.push(fromAlert(a));
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
    for (const item of due) {
      if (currentRef.current?.key === item.key) setCurrent(null);
      route(item, presenceRef.current, now);
    }
    offerHeldIfBack();
  }, [route, offerHeldIfBack, setCurrent]);

  const scheduleOs = useCallback(async (reminders: Reminder[]) => {
    const now = Date.now();
    const upcoming = reminders.filter((r) => Date.parse(r.fire_at) > now + 5000).slice(0, 40);
    setUpcomingCount(upcoming.length);
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
          sound: r.priority === 'URGENT' ? 'default' : undefined,
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
      await scheduleOs(rem.data.reminders).catch(() => undefined);
      tick();
    } catch {
      // Offline: keep last known data; OS notifications already scheduled still fire.
    }
  }, [scheduleOs, tick]);

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

  // Tapping an OS notification opens the app on that alert.
  const markActive = presence.markActive;
  useEffect(() => {
    const sub = Notifications.addNotificationResponseReceivedListener((response) => {
      const key = response.notification.request.content.data?.key as string | undefined;
      const r = remindersRef.current.find((x) => x.key === key);
      if (!r) return;
      markActive();
      handled.current.add(r.key);
      setHeld(heldRef.current.filter((h) => h.key !== r.key));
      const item = fromReminder(r);
      if (currentRef.current) queueRef.current.unshift(currentRef.current);
      setCurrent({ ...item, mode: 'silent' });
    });
    return () => sub.remove();
  }, [markActive, setCurrent, setHeld]);

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
    for (const item of items) say(spokenFor(item)); // one after another
    acknowledgeAllHeld().catch(() => undefined);
  }, [spokenFor, acknowledgeAllHeld]);

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
        say(res.status === 202 ? `${address}, sent to Sir for approval.` : `Done, ${address}.`, { interrupt: true });
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
    [acknowledge, address],
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

  const value: EngineValue = {
    presence,
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
