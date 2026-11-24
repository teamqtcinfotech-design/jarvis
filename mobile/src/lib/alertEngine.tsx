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
import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { AppState, Platform } from 'react-native';

import { ApiError, api, newIdempotencyKey } from './api';
import { dataChanged } from './bus';
import { langOfText, replyLang, twin } from './lang';
import { addressHiOf, addressOf, mdWord, mdWordHi } from './names';
import { usePrefs } from './prefs';
import { PresenceState, usePresence } from './presence';
import { useSession } from './session';
import { useDashboard } from './useDashboard';
import { say } from './speech';
import type { AlertQuestion, Priority, Reminder, ServerAlert } from './types';
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
// With live push (reminders and alerts arrive by themselves on this build) the poll is only a safety net.
const POLL_MS = 15_000; // a message from Sir, Mansi or Himanshu shows within seconds even if a push is lost
const TICK_MS = 4_000;
const STALE_MS = 2 * 60_000;
const FOLLOWUP_MINUTES = [5, 15];

export interface EngineItem {
  key: string;
  /** A message or a task from one of the three to another: never held back, flashing until "Got it". */
  personal?: boolean;
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
  /** The question JARVIS asks after it, from the server (null = none; undefined = the server did not say). */
  question?: AlertQuestion | null;
  mode?: 'loud' | 'silent';
  /** Why it was kept back instead of spoken: the offer later says "during your meeting" rather than "while you were away". */
  heldFor?: 'missed' | 'away' | 'meeting' | 'facedown' | 'quiet' | 'more';
  /** Ring until seen: work or a message from the team that waits for a Seen (the banner's main button). */
  ring?: boolean;
  /** "Seen" / "देख लिया": the button's word in the person's language. */
  seenLabel?: string;
  /** What the alert is about on the server (a work item, a message, a request), and its id. */
  refType?: string;
  refId?: string;
  /** Who it is from ("Sir", "Mansi"), for the banner. */
  from?: string;
}

/** "while you were away" / "during your meeting" …: one phrase for the whole batch, honest about the reason. */
function heldPhrase(items: EngineItem[]): { en: string; hi: string } {
  const reasons = new Set(items.map((i) => i.heldFor ?? 'away'));
  if (reasons.size === 1 && reasons.has('meeting')) return { en: 'during your meeting', hi: 'आपकी मीटिंग के दौरान' };
  if (reasons.size === 1 && reasons.has('quiet')) return { en: 'during quiet hours', hi: 'शांत समय में' };
  if (reasons.size === 1 && reasons.has('more')) return { en: 'there is more waiting', hi: 'और भी बाकी है' };
  if (reasons.size === 1 && reasons.has('facedown')) return { en: 'while your phone was face down', hi: 'जब फ़ोन उल्टा रखा था' };
  return { en: 'while you were away', hi: 'जब आप दूर थे' };
}

/** "Sir, while you were away: … Shall I read them?" — built by the engine, asked by the conversation. */
export interface HeldOffer {
  id: number;
  count: number;
  text: string; // English, Roman letters (shown and spoken with the Indian voice)
  textHi: string; // Devanagari, for the Hindi voice
  /** held: kept back while he was away / in a meeting; news: just arrived while he is here (counted, never read unasked). */
  kind: 'held' | 'news';
}

/** "3 updates have come. Shall I read them?" — the count only, never a title (his rule); the banner lists them. */
function countOffer(n: number, address: string, addressHi: string, when: { en: string; hi: string } | null): { text: string; textHi: string } {
  const lead = when ? `${address}, ${when.en}: ` : `${address}, `;
  const leadHi = when ? `${addressHi}, ${when.hi}: ` : `${addressHi}, `;
  if (n === 1) return { text: `${lead}one update has come. Shall I read it?`, textHi: `${leadHi}एक अपडेट आया है। पढ़ूँ?` };
  return { text: `${lead}${n} updates have come. Shall I read them?`, textHi: `${leadHi}${n} अपडेट आए हैं। पढ़ूँ?` };
}

export type { AlertQuestion } from './types';

/**
 * The one question JARVIS asks aloud right after an alert or reminder, and listens for the answer to. The server
 * writes it with the item (a request to Sir: "Accept it?"; a task Sir gave Mansi: "Shall I tell Sir you've seen
 * it?"; a reminder at its time: "Done, or remind you later?"; a too-full morning: "Shall I move X to tomorrow?").
 * A private reminder spoken only as "a private reminder" asks nothing. The fallback below is for a server that
 * did not send one (an older build).
 */
export function questionFor(item: EngineItem, isMD: boolean, md: { en: string; hi: string } = { en: 'Sir', hi: 'सर' }): AlertQuestion | null {
  if (item.concealed) return null;
  if (item.question !== undefined) return item.question;
  if (item.alertKind === 'NEW_REQUEST' && isMD && item.requestId) return { kind: 'accept', en: 'Accept it?', hi: 'मंज़ूर करूँ?' };
  if (item.alertKind === 'TASK_ASSIGNED' && item.alertId) {
    // Work given by anyone to anyone (the MD included): the fallback names the MD as the giver, as the old server did.
    const giver = isMD ? (item.from || 'the sender') : md.en;
    const giverHi = isMD ? (item.from || 'भेजने वाले') : md.hi;
    return { kind: 'seen', en: `Shall I tell ${giver} you've seen it?`, hi: `${giverHi} को बता दूँ कि आपने देख लिया?` };
  }
  if (item.source === 'reminder' && item.ref && item.kind !== 'MEETING') {
    if (item.label === 'checkin') return { kind: 'done', en: '', hi: '' }; // its own line already asks
    if (item.label === 'due' || item.label === 'day') return { kind: 'done', en: 'Done, or remind you later?', hi: 'हो गया, या बाद में याद दिलाऊँ?' };
  }
  return null;
}

/** The key an alert is announced under: a snoozed alert that comes back is a new announcement, not a repeat. */
const alertKey = (a: ServerAlert) => a.announce_key || `alert:${a.id}`;

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
  /** Everything on screen or behind it: the current item first, the rest by priority (for the banner's stack). */
  waiting: EngineItem[];
  /** Bring one of the waiting items to the front of the banner. */
  focus: (item: EngineItem) => void;
  held: EngineItem[];
  heldVisible: boolean;
  setHeldVisible: (v: boolean) => void;
  acknowledge: (item: EngineItem) => Promise<void>;
  acknowledgeAllHeld: () => Promise<void>;
  /** "Later": in `minutes`, at `until` (ISO), or the person's usual snooze. Resolves to when it comes back. */
  snooze: (item: EngineItem, minutes?: number, until?: string) => Promise<{ until: string; label: string; labelHi: string; note: string; noteHi: string } | null>;
  /** Everything waiting comes back later ("baaki 20 minute baad"); resolves to when, as the server says it. */
  snoozeAll: (minutes?: number, until?: string) => Promise<{ label: string; labelHi: string } | null>;
  complete: (item: EngineItem) => Promise<void>;
  decideRequest: (item: EngineItem, accept: boolean) => Promise<void>;
  reveal: () => void;
  refresh: () => Promise<void>;
  lastSync: number | null;
  notificationsGranted: boolean | null;
  upcomingCount: number;
  /** Ring until seen: "Seen" tapped on the banner. The ringing stops everywhere, the sender is told, and for work
   *  JARVIS asks at once "Shall I tell Sir when you'll do it?". */
  markSeen: (item: EngineItem) => Promise<void>;
  /** Off the screen here only (the server has already settled it, e.g. an answer to "by when?"). */
  dismiss: (item: EngineItem) => void;
  /** Everything waiting for him right now (banner, stack, held), the latest first: what his bulk words act on. */
  batchItems: () => EngineItem[];
  /** "Koi update hai?": the batch counted and offered to read ("padhun?"), nothing said here; null when nothing waits. */
  offerBatch: () => HeldOffer | null;
  /** His words on the whole batch, or a filtered part of it, done at once and in silence: all seen, all done, all
   *  off the screen (they stay on the lists), read these ones one at a time. Each resolves to how many it touched. */
  seenAll: (items: EngineItem[]) => Promise<number>;
  completeAll: (items: EngineItem[]) => Promise<number>;
  /** "Sab approve kar do" / "20 ke 20 decline": every approval or request among these decided at once; the rest untouched. */
  approveAll: (items: EngineItem[], yes: boolean) => Promise<number>;
  dismissAll: (items: EngineItem[]) => Promise<number>;
  readItems: (items: EngineItem[]) => void;
  /** Every unseen server message (news, decisions, nudges), newest first, for the Updates screen. */
  news: ServerAlert[];
  /** The Updates screen was opened: the small news on it counts as seen. */
  markRead: (ids: string[]) => Promise<void>;
  /** Several alerts came at once: they are told one at a time. The conversation calls this when one is settled or
   *  dropped, and the next (still waiting) is said with its question. */
  presentNext: () => void;
  /** The same yes / no and Seen from the Updates list, for an alert as the server lists it. */
  answerAlert: (a: ServerAlert, yes: boolean) => Promise<void>;
  seenAlert: (a: ServerAlert) => Promise<void>;
  /** "Next": this one stays waiting (back of the line); the next is said with its question. */
  skip: () => void;
  /** "Sab dikhao": every alert waiting, as cards with their buttons and the microphone (AlertsSheet). */
  allVisible: boolean;
  setAllVisible: (v: boolean) => void;
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
    question: r.question,
  };
}

function fromAlert(a: ServerAlert): EngineItem {
  return {
    key: alertKey(a),
    source: 'server',
    priority: a.priority,
    title: a.title,
    body: a.body,
    spoken: a.spoken_text,
    isPrivate: false,
    alertId: a.id,
    alertKind: a.kind,
    requestId: a.ref_type === 'request' ? a.ref_id : undefined,
    personal: a.ref_type === 'message' || a.kind === 'TASK_ASSIGNED' || a.kind === 'NEW_REQUEST' || !!a.ring,
    fireAt: Date.parse(a.snoozed_until && Date.parse(a.snoozed_until) > Date.parse(a.created_at) ? a.snoozed_until : a.created_at),
    followups: 0,
    question: a.question,
    ring: !!a.ring,
    seenLabel: a.seen_label || 'Seen',
    from: a.from_word || a.from_name || '',
    refType: a.ref_type,
    refId: a.ref_id,
  };
}

/** Which pile an item belongs to, for telling him "3 tasks, 2 approvals, 1 meeting, 2 messages" and for the list. */
export type AlertGroup = 'approval' | 'request' | 'meeting' | 'task' | 'message' | 'update';
export const GROUP_ORDER: AlertGroup[] = ['approval', 'request', 'meeting', 'task', 'message', 'update'];
export const GROUP_WORDS: Record<AlertGroup, { en: [string, string]; hi: string; title: string }> = {
  approval: { en: ['approval', 'approvals'], hi: 'मंज़ूरी', title: 'Approvals' },
  request: { en: ['request', 'requests'], hi: 'रिक्वेस्ट', title: 'Requests' },
  meeting: { en: ['meeting', 'meetings'], hi: 'मीटिंग', title: 'Meetings & calls' },
  task: { en: ['task', 'tasks'], hi: 'टास्क', title: 'Tasks' },
  message: { en: ['message', 'messages'], hi: 'संदेश', title: 'Messages' },
  update: { en: ['update', 'updates'], hi: 'अपडेट', title: 'Updates & reminders' },
};

export function groupOf(item: EngineItem): AlertGroup {
  if (item.question?.kind === 'approve') return 'approval';
  if (item.alertKind === 'NEW_REQUEST' && item.requestId) return 'request';
  if (item.refType === 'message') return 'message';
  if (item.source === 'reminder') return item.kind === 'MEETING' || item.kind === 'CALL' ? 'meeting' : 'task';
  if (item.alertKind === 'TASK_ASSIGNED') return /^meeting with|^call with|meeting/i.test(item.title) ? 'meeting' : 'task';
  return 'update';
}

/** "Sir, waiting for you: 2 tasks, Sign the lease and Pay the vendor; 1 approval, Vendor Advance; 1 message." */
export function describeWaiting(items: EngineItem[], address: string, addressHi: string): { en: string; hi: string } {
  if (!items.length) return { en: `${address}, nothing is waiting for you.`, hi: `${addressHi}, अभी कुछ भी इंतज़ार में नहीं है।` };
  const piles = new Map<AlertGroup, EngineItem[]>();
  for (const it of items) {
    const g = groupOf(it);
    piles.set(g, [...(piles.get(g) ?? []), it]);
  }
  const en: string[] = [];
  const hi: string[] = [];
  // Counts only, never a title (his rule): he reads them one at a time when he says so.
  for (const g of GROUP_ORDER) {
    const pile = piles.get(g);
    if (!pile?.length) continue;
    const w = GROUP_WORDS[g];
    en.push(`${pile.length} ${pile.length === 1 ? w.en[0] : w.en[1]}`);
    hi.push(`${pile.length} ${w.hi}`);
  }
  return { en: `${address}, waiting for you: ${en.join(', ')}.`, hi: `${addressHi}, आपके लिए इंतज़ार में: ${hi.join(', ')}।` };
}

function buzzSilently(urgent: boolean) {
  const pulses = urgent ? 3 : 1;
  for (let i = 0; i < pulses; i++) {
    setTimeout(() => Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Heavy).catch(() => undefined), i * 350);
  }
}

/** Speak a line that is already written (a server line): the voice follows its script. */
const speakLine = (text: string, opts: { interrupt?: boolean } = {}) => say(text, { ...opts, lang: langOfText(text) });

/** Speak one app-built line in the language JARVIS is using with this person (see lang.ts). */
const sayTwin = (en: string, hi: string, opts: { interrupt?: boolean } = {}) => {
  const t = twin(en, hi);
  say(t.text, { ...opts, lang: t.lang, fallback: t.fallback });
};

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
  const [queued, setQueuedCount] = useState(0);
  const [queueItems, setQueueItems] = useState<EngineItem[]>([]);
  const [held, setHeldState] = useState<EngineItem[]>([]);
  const [heldVisible, setHeldVisible] = useState(false);
  const [lastSync, setLastSync] = useState<number | null>(null);
  const [notificationsGranted, setNotificationsGranted] = useState<boolean | null>(null);
  const [upcomingCount, setUpcomingCount] = useState(0);
  const [news, setNews] = useState<ServerAlert[]>([]);
  // What the server lists right now (reminder keys and alert keys): a snoozed or repeating item whose task was
  // finished elsewhere in the meantime must not ring when its moment comes.
  const liveKeys = useRef(new Set<string>());
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

  // The queue lives in a ref (the timers read it); the screen gets a copy whenever its length is reported.
  const setQueued = useCallback((n: number) => {
    setQueuedCount(n);
    setQueueItems([...queueRef.current]);
  }, []);

  const focus = useCallback(
    (item: EngineItem) => {
      if (currentRef.current?.key === item.key) return;
      queueRef.current = queueRef.current.filter((q) => q.key !== item.key);
      if (currentRef.current) queueRef.current.unshift(currentRef.current);
      setQueued(queueRef.current.length);
      setCurrent(item);
    },
    [setCurrent, setQueued],
  );
  const waiting = useMemo(() => {
    const rest = [...queueItems].sort((a, b) => b.fireAt - a.fireAt); // the latest first, always (his rule)
    return current ? [current, ...rest.filter((q) => q.key !== current.key)] : rest;
  }, [current, queueItems]);

  const setHeld = useCallback((items: EngineItem[]) => {
    heldRef.current = items;
    setHeldState(items);
  }, []);

  const advance = useCallback(() => {
    const next = queueRef.current.shift() ?? null;
    setQueued(queueRef.current.length);
    setCurrent(next);
  }, [setCurrent, setQueued]);

  // Private details are spoken only while the app is open on screen (and allowed), unless the MD
  // has also allowed it when the phone is locked or the app is in the background.
  const privateAloud = useCallback(() => {
    const p = prefsRef.current;
    if (!p.speakPrivateDetails) return false;
    return AppState.currentState === 'active' || p.privateAloudWhenLocked;
  }, []);

  const spokenFor = useCallback(
    (item: EngineItem) => {
      if (!item.isPrivate || privateAloud()) return item.spoken;
      return replyLang() === 'hi' ? `${addressHi}, आपका एक प्राइवेट रिमाइंडर है।` : `${address}, you have a private reminder.`;
    },
    [address, addressHi, privateAloud],
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
      else if (currentRef.current.key === shown.key) setCurrent(shown);
      else {
        // The same alert can reach here twice when many arrive at once (a poll and a push): one row, the newest.
        queueRef.current = queueRef.current.filter((q) => q.key !== shown.key);
        queueRef.current.push(shown);
        queueRef.current.sort((a, b) => b.fireAt - a.fireAt); // the latest first
        setQueued(queueRef.current.length);
      }
      if (item.priority === 'URGENT' && item.followups < FOLLOWUP_MINUTES.length) {
        followups.current.set(item.key, {
          item: { ...item, followups: item.followups + 1 },
          at: Date.now() + FOLLOWUP_MINUTES[item.followups] * 60_000,
        });
      }
    },
    [setCurrent, setQueued],
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
        if (!asked) speakLine(spokenFor(item));
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
    (raw: EngineItem) => {
      // Ring until seen: being brought to the screen is NOT seeing it (18 Nov 2026: Seen is only ever the person's
      // own act — the Seen button, "dekh liya" by voice, or opening the task). It keeps ringing until then.
      const item = raw;
      handled.current.add(item.key);
      snoozed.current.delete(item.key);
      followups.current.delete(item.key);
      setHeld(heldRef.current.filter((h) => h.key !== item.key));
      queueRef.current = queueRef.current.filter((q) => q.key !== item.key);
      setQueued(queueRef.current.length);
      if (currentRef.current && currentRef.current.key !== item.key) queueRef.current.unshift(currentRef.current);
      setCurrent({ ...item, mode: 'silent' });
      const presenter = presenterRef.current;
      if (!presenter || !presenter(item, spokenFor(item))) speakLine(spokenFor(item), { interrupt: true });
      tell([item]);
    },
    [setCurrent, setHeld, setQueued, spokenFor, tell],
  );

  /** Several things at the same moment: told ONE AT A TIME. The first is said with its question; the rest go on the
   *  banner's stack at once and wait their turn: as soon as one is answered (or dropped), the next is said. */
  // "hold" is defined further down; this lets the function above it keep the rest for later.
  const holdRef = useRef<(item: EngineItem, why: NonNullable<EngineItem['heldFor']>) => void>(() => undefined);
  const toPresent = useRef<EngineItem[]>([]);
  const deliverTogether = useCallback(
    (items: EngineItem[]) => {
      if (items.length === 1) return deliver(items[0], 'loud');
      const order = [...items].sort((a, b) => b.fireAt - a.fireAt); // the latest first
      deliver(order[0], 'loud');
      order.slice(1).forEach((item) => {
        show(item, 'silent');
        if (!toPresent.current.some((t) => t.key === item.key)) toPresent.current.push(item);
      });
    },
    [deliver, show],
  );
  /** The next alert still waiting on the stack, brought to the front and said with its question; nothing while he
   *  is talking with JARVIS. */
  const presentNext = useCallback(() => {
    if (talkingRef.current) return; // tried again once the conversation is quiet (setTalking)
    while (toPresent.current.length) {
      const item = toPresent.current.shift()!;
      const stillHere = currentRef.current?.key === item.key || queueRef.current.some((q) => q.key === item.key);
      if (!stillHere) continue; // dealt with meanwhile (tapped, answered by voice, finished elsewhere)
      focus(item);
      deliver(item, 'loud');
      return;
    }
  }, [deliver, focus]);
  /** "Next" / "agla": the one on the banner goes to the back of the line (still waiting, nothing is lost) and the next
   *  one is said with its question. */
  const skip = useCallback(() => {
    const cur = currentRef.current;
    if (!cur) return;
    queueRef.current = queueRef.current.filter((q) => q.key !== cur.key);
    const next = toPresent.current.find((t) => t.key !== cur.key && queueRef.current.some((q) => q.key === t.key)) ?? queueRef.current[0] ?? null;
    queueRef.current.push(cur);
    setQueued(queueRef.current.length);
    toPresent.current = toPresent.current.filter((t) => t.key !== (next?.key ?? ''));
    if (!next) {
      setCurrent(cur);
      return;
    }
    focus(next);
    deliver(next, 'loud');
  }, [deliver, focus, setCurrent, setQueued]);
  const [allVisible, setAllVisible] = useState(false);
  const presentNextRef = useRef(presentNext);
  useEffect(() => {
    presentNextRef.current = presentNext;
  }, [presentNext]);
  const nextTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scheduleNext = useCallback((ms = 1200) => {
    if (!toPresent.current.length) return;
    if (nextTimer.current) clearTimeout(nextTimer.current);
    nextTimer.current = setTimeout(() => {
      nextTimer.current = null;
      presentNextRef.current();
    }, ms);
  }, []);
  const scheduleNextRef = useRef(scheduleNext);
  useEffect(() => {
    scheduleNextRef.current = scheduleNext;
  }, [scheduleNext]);

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
  useEffect(() => {
    holdRef.current = hold;
  }, [hold]);

  // He is back (picked the phone up, opened the app) and alerts were held: offer to read them, by count only.
  // The engine only builds the offer; the conversation asks it aloud and listens for the answer.
  const offerKind = useRef<'held' | 'news'>('held');
  const offerHeldIfBack = useCallback(() => {
    if (presenceRef.current !== 'present') return;
    const items = heldRef.current;
    if (items.length <= announcedHeld.current) return;
    announcedHeld.current = items.length;
    const words = countOffer(items.length, address, addressHi, heldPhrase(items));
    offerSeq.current += 1;
    offerKind.current = 'held';
    setOffer({ id: offerSeq.current, count: items.length, kind: 'held', ...words });
    Haptics.notificationAsync(Haptics.NotificationFeedbackType.Warning).catch(() => undefined);
  }, [address, addressHi]);

  // News that arrives while he is here (work given, messages, staff updates, nudges, suggestions) is never read out
  // unasked: it goes on the banner's stack, and after a moment JARVIS says only how many came and asks whether to
  // read them. Yes reads them one at a time (deliverTogether), each with its question; no leaves them on the list.
  const newsRef = useRef<EngineItem[]>([]);
  const newsTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const offerNews = useCallback(
    (items: EngineItem[]) => {
      for (const item of items) {
        show(item, 'silent');
        if (!newsRef.current.some((n) => n.key === item.key)) newsRef.current.push(item);
      }
      tell(items);
      Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success).catch(() => undefined);
      if (newsTimer.current) clearTimeout(newsTimer.current);
      newsTimer.current = setTimeout(() => {
        newsTimer.current = null;
        // Only what is still waiting on screen (he may have dealt with one meanwhile).
        const fresh = newsRef.current.filter((n) => currentRef.current?.key === n.key || queueRef.current.some((q) => q.key === n.key));
        newsRef.current = fresh;
        if (!fresh.length) return;
        offerSeq.current += 1;
        offerKind.current = 'news';
        setOffer({ id: offerSeq.current, count: fresh.length, kind: 'news', ...countOffer(fresh.length, address, addressHi, null) });
      }, 1500);
    },
    [show, tell, address, addressHi],
  );

  const setVoiceProbe = useCallback((probe: (() => Promise<boolean>) | null) => {
    voiceProbe.current = probe;
  }, []);

  const setPresenter = useCallback((presenter: AlertPresenter | null) => {
    presenterRef.current = presenter;
  }, []);

  // Alerts that arrived while he was talking with JARVIS: shown at once, and SAID as soon as he has finished (the
  // conversation has been quiet for a moment), with their question, so he never has to ask for them.
  const afterTalk = useRef<EngineItem[]>([]);
  const afterTalkTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sayAfterTalk = useRef<(items: EngineItem[]) => void>(() => undefined);
  const setTalking = useCallback((talking: boolean) => {
    talkingRef.current = talking;
    if (afterTalkTimer.current) clearTimeout(afterTalkTimer.current);
    afterTalkTimer.current = null;
    if (talking) return;
    if (!afterTalk.current.length) {
      scheduleNextRef.current(2500);
      return;
    }
    afterTalkTimer.current = setTimeout(() => {
      afterTalkTimer.current = null;
      if (talkingRef.current) return;
      // Only what is still waiting on screen: something he already dealt with by voice is not brought up again.
      const waiting = afterTalk.current.filter((i) => currentRef.current?.key === i.key || queueRef.current.some((q) => q.key === i.key));
      afterTalk.current = [];
      if (waiting.length) sayAfterTalk.current(waiting);
      else presentNextRef.current(); // the next of several that came together, now that he is quiet
    }, 2500);
  }, []);

  /**
   * Decide what happens to one item that is due now. Returns 'loud' when it should be spoken (the caller speaks
   * everything due at this moment as one sentence), or handles it itself (silent banner, or held for later).
   */
  const route = useCallback(
    (item: EngineItem, state: PresenceState, now: number): 'loud' | 'offer' | null => {
      handled.current.add(item.key);
      const missed = item.source === 'reminder' && item.followups === 0 && now - item.fireAt > STALE_MS;
      if (missed) {
        hold(item, 'missed'); // fired while the app was closed or the phone asleep
        return null;
      }
      if (item.priority === 'LOW' && item.source === 'server' && !item.ring) {
        // Small news ("Mansi started a task"): never spoken, never vibrates, never offered; it is in the lists.
        tell([item]);
        return null;
      }
      if (state === 'present') {
        // Mid-conversation with JARVIS: show it quietly, never speak over him (he can act on it by voice).
        if (talkingRef.current) {
          deliver(item, 'silent');
          afterTalk.current.push(item);
          return null;
        }
        // A reminder at its moment is said as it is; news is counted first and read only when he says so.
        return item.source === 'server' ? 'offer' : 'loud';
      }
      // A message or a task from one of the three: never kept back. Phone left on the desk: still said and shown;
      // in a meeting, quiet hours or face down: shown and buzzed, flashing until he taps it.
      if (item.personal) {
        if (state === 'away') return 'loud';
        deliver(item, 'silent');
        return null;
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

  useEffect(() => {
    sayAfterTalk.current = (items) => (items.length === 1 ? deliver(items[0], 'loud') : deliverTogether(items));
  }, [deliver, deliverTogether]);

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
      const key = alertKey(a);
      if (handled.current.has(key)) continue;
      if (!a.fresh) {
        handled.current.add(key); // told already, or old news: it stays in the lists, it is not announced
        // Still waiting for its Seen (said earlier by the phone's background part, or before the app restarted):
        // it is not said again, but its banner with the Seen button is back on screen.
        if (a.ring) show(fromAlert(a), 'silent');
        continue;
      }
      due.push(fromAlert(a));
    }
    for (const [k, s] of snoozed.current) {
      if (s.until <= now) {
        snoozed.current.delete(k);
        if (!liveKeys.current.has(k)) continue; // finished or cancelled elsewhere meanwhile: nothing to ring about
        due.push({ ...s.item, followups: Math.max(s.item.followups, 0) });
      }
    }
    for (const [k, f] of followups.current) {
      if (f.at <= now) {
        followups.current.delete(k);
        if (!liveKeys.current.has(k)) continue;
        // Already shown? Re-alert only if still not acknowledged.
        if (currentRef.current?.key === k) queueRef.current = queueRef.current.filter((q) => q.key !== k);
        due.push(f.item);
      }
    }
    due.sort((a, b) => RANK[a.priority] - RANK[b.priority] || a.fireAt - b.fireAt);
    const loud: EngineItem[] = [];
    const news: EngineItem[] = [];
    for (const item of due) {
      if (currentRef.current?.key === item.key) setCurrent(null);
      const way = route(item, presenceRef.current, now);
      if (way === 'loud') loud.push(item);
      else if (way === 'offer') news.push(item);
    }
    if (loud.length) speakNow(loud);
    if (news.length) offerNews(news);
    offerHeldIfBack();
  }, [route, speakNow, offerNews, offerHeldIfBack, setCurrent, show]);

  /**
   * "Shall I tell Sir when you'll do it?" for work already seen (Seen tapped on the banner, or on a locked phone's
   * notification, which brings the app up with "promise:<id>"). Only the question is said; the answer goes to the
   * server as words ("kal shaam tak").
   */
  const askPromise = useCallback(async (alertId: string, known?: EngineItem) => {
    try {
      const res = await api<ServerAlert>(`/alerts/${alertId}/`);
      const q = res.data.question;
      if (!q || q.kind !== 'promise') return; // answered already, or no longer theirs
      const item = { ...(known ?? fromAlert(res.data)), ring: false, question: q };
      presenterRef.current?.(item, '');
    } catch {
      // gone, or offline: nothing to ask
    }
  }, []);

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
    [advance, setHeld, setQueued],
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
      setNews(alerts.data.results);
      setBusyWindows(rem.data.busy_windows);
      setLastSync(Date.now());
      const live = new Set([...rem.data.reminders.map((r) => r.key), ...alerts.data.results.map(alertKey)]);
      liveKeys.current = live;
      pruneGone(live);
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
    const a = alertsRef.current.find((x) => alertKey(x) === key || `alert:${x.id}` === key);
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
      if (key.startsWith('promise:')) {
        askPromise(key.slice('promise:'.length)).catch(() => undefined);
        return;
      }
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
    // Seen tapped on a notification while the app stayed open under the shade: the native part leaves the key and
    // sends a push event instead of a return to the app.
    const offPush = onPush(() => {
      if (AppState.currentState === 'active') setTimeout(check, 200);
    });
    return () => {
      if (timer) clearTimeout(timer);
      sub.remove();
      offPush();
    };
  }, [markActive, itemForKey, presentItem, poll, askPromise]);

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
      toPresent.current = toPresent.current.filter((t) => t.key !== item.key);
      scheduleNext(); // one settled: the next of several that came together is said now
      const id = osId(item.key);
      Notifications.cancelScheduledNotificationAsync(id).catch(() => undefined);
      Notifications.dismissNotificationAsync(id).catch(() => undefined);
    },
    [advance, setHeld, setQueued, scheduleNext],
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

  // Ring until seen: the banner's Seen button.
  const markSeen = useCallback(
    async (item: EngineItem) => {
      clearKey(item);
      if (!item.alertId) return;
      try {
        const res = await api<{ question?: AlertQuestion | null }>(`/alerts/${item.alertId}/seen/`, { method: 'POST', body: {} });
        dataChanged.emit();
        if (res.data.question?.kind === 'promise') await askPromise(item.alertId, item);
      } catch {
        // Offline: it keeps ringing until the server hears the Seen (tap it again then).
      }
    },
    [clearKey, askPromise],
  );

  const acknowledgeAllHeld = useCallback(async () => {
    const items = [...heldRef.current];
    for (const item of items) await acknowledge(item);
    setHeldVisible(false);
  }, [acknowledge]);

  // "Yes, read them": one at a time, the latest first, each with its question; the next comes once he has answered
  // (presentNext). Held ones move onto the banner's stack for this.
  const acceptOffer = useCallback(() => {
    setOffer(null);
    const items = offerKind.current === 'news' ? newsRef.current : [...heldRef.current];
    newsRef.current = [];
    if (offerKind.current === 'held') {
      setHeld([]);
      announcedHeld.current = 0;
    }
    const alive = items.filter((i) => offerKind.current === 'held' || currentRef.current?.key === i.key || queueRef.current.some((q) => q.key === i.key));
    if (alive.length) deliverTogether(alive);
  }, [deliverTogether, setHeld]);

  // "No": nothing is read; they stay on the list (the latest first) until he opens them, and are not offered again
  // until something new comes.
  const declineOffer = useCallback(() => {
    setOffer(null);
    newsRef.current = [];
  }, []);


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

  /**
   * "Later": the reminder or alert comes back at `until` (or in `minutes`; neither = the person's usual snooze,
   * learned from what they say). Kept on the server, so the phone's background part honours the same time and a
   * restart loses nothing; the item leaves the screen now and returns as a fresh announcement. Resolves to the
   * time it comes back, as the server says it ("at 4:25 PM"), or null when only the phone could keep it.
   */
  const snooze = useCallback(
    async (item: EngineItem, minutes?: number, until?: string): Promise<{ until: string; label: string; labelHi: string; note: string; noteHi: string } | null> => {
      followups.current.delete(item.key);
      queueRef.current = queueRef.current.filter((q) => q.key !== item.key);
      setQueued(queueRef.current.length);
      setHeld(heldRef.current.filter((h) => h.key !== item.key));
      if (currentRef.current?.key === item.key) advance();
      handled.current.add(item.key);
      const body: Record<string, unknown> = until ? { until } : { minutes: minutes ?? prefsRef.current.snoozeMinutes };
      if (item.source === 'reminder') body.key = item.key;
      else if (item.alertId) body.alert_id = item.alertId;
      try {
        const res = await api<{ until: string; until_label: string; until_label_hi?: string; note?: string; note_hi?: string }>('/reminders/snooze/', { method: 'POST', body });
        dataChanged.emit();
        // The server's word on what the snooze does not do: "after its deadline; the deadline stays", the nudge after
        // the third "later". Spoken after the time it comes back.
        return { until: res.data.until, label: res.data.until_label, labelHi: res.data.until_label_hi ?? '', note: res.data.note ?? '', noteHi: res.data.note_hi ?? '' };
      } catch {
        // Offline: this phone keeps it itself until the server can be reached again.
        const at = until ? Date.parse(until) : Date.now() + (minutes ?? prefsRef.current.snoozeMinutes) * 60_000;
        snoozed.current.set(item.key, { item: { ...item, followups: 0, fireAt: Date.now() }, until: at });
        return null;
      }
    },
    [advance, setHeld, setQueued],
  );

  /** "Baaki baad mein" / "20 minute baad": everything waiting (on the banner, on the stack, held) comes back then as
   *  fresh news, counted again. Resolves to the time it comes back, as the server says it, or null when offline. */
  const snoozeAll = useCallback(
    async (minutes?: number, until?: string): Promise<{ label: string; labelHi: string } | null> => {
      setOffer(null);
      newsRef.current = [];
      toPresent.current = [];
      const seen = new Set<string>();
      const items = [currentRef.current, ...queueRef.current, ...heldRef.current].filter((i): i is EngineItem => {
        if (!i || seen.has(i.key)) return false;
        seen.add(i.key);
        return true;
      });
      let back: { label: string; labelHi: string } | null = null;
      for (const item of items) {
        const r = await snooze(item, minutes, until);
        if (r) back = { label: r.label, labelHi: r.labelHi };
      }
      return back;
    },
    [snooze],
  );

  const complete = useCallback(
    async (item: EngineItem) => {
      if (!item.ref) return acknowledge(item);
      const path = item.ref.scope === 'private' ? `/private-tasks/${item.ref.id}/complete/` : `/shared-tasks/${item.ref.id}/complete/`;
      try {
        const res = await api(path, { method: 'POST', body: { expected_version: item.ref.version } });
        await acknowledge(item);
        if (res.status === 202) sayTwin(`${address}, sent to ${md.en} for approval.`, `${addressHi}, ${md.hi} को मंज़ूरी के लिए भेज दिया।`, { interrupt: true });
        else sayTwin(`Done, ${address}.`, `हो गया, ${addressHi}।`, { interrupt: true });
        dataChanged.emit();
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          sayTwin(`${address}, this task changed. I've refreshed it.`, `${addressHi}, ये टास्क बदल गया था। मैंने रिफ़्रेश कर दिया।`, { interrupt: true });
          await acknowledge(item);
          dataChanged.emit();
        } else {
          sayTwin(`Sorry ${address}, I couldn't reach the server. Please try again.`,
            `माफ़ कीजिए ${addressHi}, सर्वर से संपर्क नहीं हो पाया। फिर से कोशिश कीजिए।`, { interrupt: true });
        }
      }
    },
    [acknowledge, address, addressHi, md.en, md.hi],
  );

  // ---- his words on the whole batch ("sab seen kar do", "sab baad mein", "sirf meetings padho") ----
  const batchItems = useCallback((): EngineItem[] => {
    const seen = new Set<string>();
    return [currentRef.current, ...queueRef.current, ...heldRef.current]
      .filter((i): i is EngineItem => !!i && !seen.has(i.key) && !!seen.add(i.key))
      .sort((a, b) => b.fireAt - a.fireAt);
  }, []);
  const settleBatch = useCallback(() => {
    setOffer(null);
    newsRef.current = [];
    toPresent.current = [];
  }, []);
  const offerBatch = useCallback((): HeldOffer | null => {
    const items = batchItems();
    if (!items.length) return null;
    newsRef.current = items;
    offerKind.current = 'news';
    offerSeq.current += 1;
    return { id: offerSeq.current, count: items.length, kind: 'news', ...countOffer(items.length, address, addressHi, null) };
  }, [batchItems, address, addressHi]);
  const seenAll = useCallback(
    async (items: EngineItem[]) => {
      settleBatch();
      for (const item of items) {
        if (item.alertId && item.ring) await api(`/alerts/${item.alertId}/seen/`, { method: 'POST', body: {} }).catch(() => undefined);
        await acknowledge(item);
      }
      dataChanged.emit();
      return items.length;
    },
    [acknowledge, settleBatch],
  );
  const completeAll = useCallback(
    async (items: EngineItem[]) => {
      settleBatch();
      let n = 0;
      for (const item of items) {
        // A reminder carries its task; an alert about work carries the task's id, and its version is fetched first.
        let ref = item.ref ? { scope: item.ref.scope, id: item.ref.id, version: item.ref.version } : null;
        if (!ref && item.refType === 'shared_task' && item.refId) {
          const t = await api<{ version?: number }>(`/shared-tasks/${item.refId}/`).catch(() => null);
          if (t) ref = { scope: 'shared', id: item.refId, version: t.data.version ?? 1 };
        }
        if (ref) {
          const path = ref.scope === 'private' ? `/private-tasks/${ref.id}/complete/` : `/shared-tasks/${ref.id}/complete/`;
          const ok = await api(path, { method: 'POST', body: { expected_version: ref.version } }).then(() => true).catch(() => false);
          if (ok) n += 1;
        }
        await acknowledge(item);
      }
      dataChanged.emit();
      return n;
    },
    [acknowledge, settleBatch],
  );
  const dismissAll = useCallback(
    async (items: EngineItem[]) => {
      settleBatch();
      for (const item of items) await acknowledge(item);
      return items.length;
    },
    [acknowledge, settleBatch],
  );
  const approveAll = useCallback(
    async (items: EngineItem[], yes: boolean) => {
      settleBatch();
      let n = 0;
      for (const item of items) {
        let ok = false;
        if (item.question?.kind === 'approve' && item.question.task) {
          ok = await api(`/shared-tasks/${item.question.task}/approve/`, { method: 'POST', body: { approve: yes }, idempotencyKey: newIdempotencyKey() })
            .then(() => true).catch(() => false);
        } else if (item.alertKind === 'NEW_REQUEST' && item.requestId) {
          ok = await api(`/requests/${item.requestId}/${yes ? 'accept' : 'decline'}/`, { method: 'POST', body: {}, idempotencyKey: newIdempotencyKey() })
            .then(() => true).catch(() => false);
        } else {
          continue; // not an approval: left as it is
        }
        if (ok) {
          n += 1;
          await acknowledge(item);
        }
      }
      dataChanged.emit();
      return n;
    },
    [acknowledge, settleBatch],
  );
  const readItems = useCallback(
    (items: EngineItem[]) => {
      settleBatch();
      if (items.length) deliverTogether(items);
    },
    [deliverTogether, settleBatch],
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
        if (accept) sayTwin(`Accepted, ${address}. It's on your list.`, `मंज़ूर कर दिया, ${addressHi}। आपकी लिस्ट में आ गया।`, { interrupt: true });
        else sayTwin(`Declined, ${address}. I've informed them.`, `मना कर दिया, ${addressHi}। उन्हें बता दिया है।`, { interrupt: true });
      } catch (e) {
        const msg = e instanceof ApiError ? e.message : 'Please try again.';
        sayTwin(`${address}, ${msg}`, `${addressHi}, ${msg}`, { interrupt: true });
      }
      await acknowledge(item);
      dataChanged.emit();
    },
    [acknowledge, address, addressHi],
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
        // "Later" with no time said: his usual snooze (learned), and the confirmation names the exact time.
        const back = await snooze(item);
        const minutes = prefsRef.current.snoozeMinutes;
        if (back) sayTwin(`Okay ${address}, I'll remind you ${back.label}.`, `ठीक है ${addressHi}, ${back.labelHi || back.label} याद दिला दूँगा।`, { interrupt: true });
        else sayTwin(`Okay ${address}, I'll remind you in ${minutes} minutes.`, `ठीक है ${addressHi}, ${minutes} मिनट बाद याद दिला दूँगा।`, { interrupt: true });
        return;
      }
      if (question.kind === 'move') {
        // The morning briefing offered to move one item to tomorrow: a yes moves it, a no leaves the day as it is.
        clearKey(item);
        try {
          if (item.alertId) await api(`/alerts/${item.alertId}/ack/`, { method: 'POST' });
          const t = question.target;
          if (yes && t && question.to) {
            const path = t.scope === 'private' ? `/private-tasks/${t.id}/` : `/shared-tasks/${t.id}/`;
            await api(path, { method: 'PATCH', body: { expected_version: t.version ?? 1, due_date: question.to } });
            dataChanged.emit();
            sayTwin(`Moved ${t.title} to tomorrow, ${address}.`, `${t.title} कल पर ले गया, ${addressHi}।`, { interrupt: true });
          } else if (!yes) {
            sayTwin(`Okay ${address}, leaving it.`, `ठीक है ${addressHi}, रहने देता हूँ।`, { interrupt: true });
          }
        } catch (e) {
          const msg = e instanceof ApiError ? e.message : "I couldn't reach the server.";
          sayTwin(`Sorry ${address}, ${msg}`, `माफ़ कीजिए ${addressHi}, ${msg}`, { interrupt: true });
        }
        return;
      }
      if (question.kind === 'place') {
        // "You're free till 4; shall I put these in?": the server lays the planned items into the free stretch (or, on
        // a no, leaves the day alone) and says what it did.
        clearKey(item);
        if (!item.alertId) return;
        try {
          const r = await api<{ show: string; say_hi: string }>(`/alerts/${item.alertId}/place/`, { method: 'POST', body: { yes } });
          dataChanged.emit();
          sayTwin(r.data.show, r.data.say_hi || r.data.show, { interrupt: true });
        } catch (e) {
          const msg = e instanceof ApiError ? e.message : "I couldn't reach the server.";
          sayTwin(`Sorry ${address}, ${msg}`, `माफ़ कीजिए ${addressHi}, ${msg}`, { interrupt: true });
        }
        return;
      }
      if (question.kind === 'approve') {
        // An approval asked of him: yes approves, no declines; whoever asked is told by the server. Deciding counts
        // as seen, so the ringing stops too.
        clearKey(item);
        try {
          if (question.task) {
            await api(`/shared-tasks/${question.task}/approve/`, { method: 'POST', body: { approve: yes }, idempotencyKey: newIdempotencyKey() });
          }
          if (item.alertId) await api(`/alerts/${item.alertId}/ack/`, { method: 'POST', body: { seen: false } }).catch(() => undefined);
          dataChanged.emit();
          if (yes) sayTwin(`Approved, ${address}. I've told them.`, `मंज़ूर कर दिया, ${addressHi}। उन्हें बता दिया।`, { interrupt: true });
          else sayTwin(`Declined, ${address}. I've told them.`, `मंज़ूर नहीं किया, ${addressHi}। उन्हें बता दिया।`, { interrupt: true });
        } catch (e) {
          const msg = e instanceof ApiError ? e.message : "I couldn't reach the server.";
          sayTwin(`Sorry ${address}, ${msg}`, `माफ़ कीजिए ${addressHi}, ${msg}`, { interrupt: true });
        }
        return;
      }
      if (question.kind === 'signout') {
        // "Signed in on another phone — not you?": yes signs that phone out at once; no = it was them.
        clearKey(item);
        try {
          if (yes && question.session) await api(`/auth/sessions/${question.session}/revoke/`, { method: 'POST', body: {} });
          if (item.alertId) await api(`/alerts/${item.alertId}/ack/`, { method: 'POST', body: { seen: true } });
          if (yes) sayTwin(`Signed that phone out, ${address}.`, `वो फ़ोन साइन आउट कर दिया, ${addressHi}।`, { interrupt: true });
        } catch (e) {
          const msg = e instanceof ApiError ? e.message : "I couldn't reach the server.";
          sayTwin(`Sorry ${address}, ${msg}`, `माफ़ कीजिए ${addressHi}, ${msg}`, { interrupt: true });
        }
        return;
      }
      // 'seen': yes = whoever gave the task (or sent the message) is told it was seen; no = it simply leaves the screen.
      clearKey(item);
      try {
        let to = md.en;
        let toHi = md.hi;
        if (item.alertId) {
          const res = await api<{ told_to?: string; told_to_hi?: string }>(`/alerts/${item.alertId}/ack/`, { method: 'POST', body: { seen: yes } });
          to = res.data?.told_to || to;
          toHi = res.data?.told_to_hi || toHi;
        }
        if (yes) sayTwin(`Told ${to}, ${address}.`, `${toHi} को बता दिया, ${addressHi}।`, { interrupt: true });
      } catch {
        sayTwin(`Sorry ${address}, I couldn't reach the server.`, `माफ़ कीजिए ${addressHi}, सर्वर से संपर्क नहीं हो पाया।`, { interrupt: true });
      }
    },
    [decideRequest, complete, snooze, clearKey, address, addressHi, md.en, md.hi],
  );

  const markRead = useCallback(
    async (ids: string[]) => {
      for (const id of ids) {
        await api(`/alerts/${id}/ack/`, { method: 'POST' }).catch(() => undefined);
      }
      setNews((prev) => prev.filter((a) => !ids.includes(a.id)));
      alertsRef.current = alertsRef.current.filter((a) => !ids.includes(a.id));
    },
    [],
  );

  // The Updates list acts on an alert as the server lists it (the same yes / no / Seen as the banner).
  const answerAlert = useCallback(
    async (a: ServerAlert, yes: boolean) => {
      const item = fromAlert(a);
      const q = a.question ?? questionFor(item, isMD, md);
      if (!q) return acknowledge(item);
      await answerQuestion(item, q, yes);
      setNews((prev) => prev.filter((x) => x.id !== a.id));
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps -- md is a fresh object each render; its two words are what matter
    [answerQuestion, acknowledge, isMD, md.en, md.hi],
  );
  const seenAlert = useCallback(
    async (a: ServerAlert) => {
      await markSeen(fromAlert(a));
      setNews((prev) => prev.map((x) => (x.id === a.id ? { ...x, seen_at: new Date().toISOString(), ring: false } : x)));
    },
    [markSeen],
  );

  const value: EngineValue = {
    presence,
    setPresenter,
    answerQuestion,
    markSeen,
    dismiss: clearKey,
    news,
    markRead,
    presentNext,
    answerAlert,
    seenAlert,
    skip,
    allVisible,
    setAllVisible,
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
    waiting,
    focus,
    held,
    heldVisible,
    setHeldVisible,
    acknowledge,
    acknowledgeAllHeld,
    snooze,
    snoozeAll,
    batchItems,
    offerBatch,
    seenAll,
    completeAll,
    approveAll,
    dismissAll,
    readItems,
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
