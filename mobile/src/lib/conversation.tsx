/**
 * The JARVIS conversation, shared by the main screen (orb) and the full-transcript view.
 *
 * Rules:
 * - JARVIS listens when the user taps the orb, and again by itself only after it asked a question
 *   ("shall I save it?", "which Ramesh?"). Never after a plain answer.
 * - Silence after a question: one gentle "Sir?", then it waits quietly. Silence never means "he's gone".
 * - While a card waits, everything said goes to the AI with a description of the card; the AI decides
 *   whether it was a yes, a no, a change, or something unrelated. No fixed words.
 * - Audio never leaves the phone; only the recognised text goes to our server.
 */
import * as Haptics from 'expo-haptics';
import React, { createContext, useContext, useEffect, useRef, useState } from 'react';
import { AppState } from 'react-native';

import { ApiError, api, newIdempotencyKey } from './api';
import { dataChanged } from './bus';
import { CONFIRMABLE, TASK_DRAFTS, describeDraft } from './drafts';
import { dayLabel } from './format';
import { usePrefs } from './prefs';
import { useSession } from './session';
import { onSpeakingChange, say, stopSpeaking, type SpeakLang } from './speech';
import type { AskAnswer, Draft, DraftAction, StaffOption } from './types';
import { SILENCE_CODES, useVoiceCapture } from './voice';

export interface Msg {
  id: string;
  role: 'user' | 'jarvis';
  text: string;
  tone?: 'error' | 'saved';
  sources?: AskAnswer['sources'];
}

export type OrbState = 'idle' | 'listening' | 'thinking' | 'speaking';
type Busy = '' | 'thinking' | 'searching' | 'saving';

interface ConversationValue {
  messages: Msg[];
  draft: Draft | null;
  draftError: string;
  busy: Busy;
  speaking: boolean;
  listening: boolean;
  partial: string;
  orb: OrbState;
  status: string;
  micPress: () => void;
  send: (text: string) => void;
  editDraft: (patch: Partial<Draft>) => void;
  cancelDraft: () => void;
  save: () => void;
  clear: () => void;
  /** Used when JARVIS is woken by "Hey JARVIS": greets, then listens. */
  greet: () => void;
}

const ConversationContext = createContext<ConversationValue | null>(null);

const HHMM = /^([01]?\d|2[0-3]):([0-5]\d)$/;
// AI-written "done" lines are used only where "saved" really means done. A staff task still waiting for
// the staff app, or a team split, gets an exact status line instead.
const AI_DONE_OK: DraftAction[] = ['private_task', 'pa_task', 'pa_meeting', 'md_request', 'remember', 'accept_request',
  'decline_request', 'complete'];
const BASE_HINTS = ['JARVIS', 'Jarvis', 'Hey JARVIS'];

let seq = 0;
const newId = () => `m${Date.now()}-${seq++}`;

export function ConversationProvider({ children }: { children: React.ReactNode }) {
  const { user } = useSession();
  const { prefs } = usePrefs();
  const isMD = user?.role === 'MD';
  const address = isMD ? 'Sir' : user?.name ?? '';

  const [messages, setMessages] = useState<Msg[]>([]);
  const [draft, setDraftState] = useState<Draft | null>(null);
  const [draftError, setDraftError] = useState('');
  const [busy, setBusy] = useState<Busy>('');
  const [speaking, setSpeaking] = useState(false);
  const [hints, setHints] = useState<string[]>(BASE_HINTS);

  const messagesRef = useRef<Msg[]>([]);
  const draftRef = useRef<Draft | null>(null);
  const editedRef = useRef(false); // the user changed the card by hand, so the AI's "done" line may be stale
  const expectingRef = useRef(false); // JARVIS asked something and is waiting for the answer
  const silenceRef = useRef(0);
  const previousRef = useRef(''); // the sentence a clarification answer belongs to
  const langRef = useRef<SpeakLang>('en');
  const idemKey = useRef('');
  const engine = useRef<{ heard: (t: string) => void; voiceError: (m: string, c: string) => void }>({
    heard: () => undefined,
    voiceError: () => undefined,
  });

  const voice = useVoiceCapture(prefs.listenLang, {
    hints,
    onFinal: (text) => engine.current.heard(text),
    onError: (message, code) => engine.current.voiceError(message, code),
  });
  const { start: startVoice, stop: stopVoice, cancel: cancelVoice } = voice;

  // Names the recogniser should expect, so "JARVIS" and "Mansi" are not turned into other words.
  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    api<{ results: StaffOption[] }>('/staff/')
      .then((r) => {
        if (cancelled) return;
        const names = r.data.results.flatMap((s) => [s.name, s.name.split(' ')[0]]);
        setHints([...BASE_HINTS, ...new Set(names)].slice(0, 60));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [user]);

  // ---------------------------------------------------------------- helpers
  const push = (role: Msg['role'], text: string, extra: Partial<Msg> = {}) => {
    messagesRef.current = [...messagesRef.current, { id: newId(), role, text, ...extra }];
    setMessages(messagesRef.current);
  };
  const setDraft = (d: Draft | null) => {
    draftRef.current = d;
    setDraftState(d);
  };
  const history = () =>
    messagesRef.current.filter((m) => m.tone !== 'error').slice(-8).map((m) => ({ role: m.role, text: m.text }));
  const listen = () => {
    if (!user) return;
    startVoice();
  };
  /** Show a JARVIS line, speak it, and listen again only when it is a question. */
  const reply = (show: string, speakText: string | undefined, lang: SpeakLang, expectAnswer: boolean, extra: Partial<Msg> = {}) => {
    push('jarvis', show, extra);
    expectingRef.current = expectAnswer;
    silenceRef.current = 0;
    say(speakText || show, { interrupt: true, lang, onDone: expectAnswer ? listen : undefined });
  };
  /** The card waiting for a yes / no / change (a card still asking a question is not "waiting"). */
  const waitingCard = () => {
    const d = draftRef.current;
    return d && !d.needs_clarification && d.action !== 'choose_destination' ? d : null;
  };
  const hiLine = (roman: string, devanagari: string) =>
    langRef.current === 'hi' ? { show: roman, speak: devanagari, lang: 'hi' as SpeakLang } : null;

  // ---------------------------------------------------------------- what the user said
  const heard = (text: string) => {
    Haptics.selectionAsync().catch(() => undefined); // instant "got it" tick
    silenceRef.current = 0;
    expectingRef.current = false;
    push('user', text);
    const card = waitingCard();
    if (card) interpret(text, card.transcript, describeDraft(card));
    else interpret(text, previousRef.current, '');
  };

  const voiceError = (message: string, code: string) => {
    if (SILENCE_CODES.has(code)) {
      if (expectingRef.current && silenceRef.current === 0) {
        // One gentle nudge after a question, then wait quietly.
        silenceRef.current = 1;
        const hi = hiLine(`${address}?`, isMD ? 'सर?' : `${address}?`);
        if (hi) say(hi.speak, { interrupt: true, lang: 'hi', onDone: listen });
        else say(`${address}?`, { interrupt: true, onDone: listen });
      } else {
        silenceRef.current = 0;
        expectingRef.current = false;
      }
      return;
    }
    expectingRef.current = false;
    push('jarvis', message, { tone: 'error' });
  };

  const interpret = async (text: string, previous: string, awaiting: string) => {
    setBusy('thinking');
    try {
      const res = await api<Draft>('/assistant/interpret/', {
        method: 'POST',
        body: { text, previous, awaiting, history: history().slice(0, -1) },
        timeoutMs: 30000,
      });
      setBusy('');
      handleDraft(res.data);
    } catch (e) {
      setBusy('');
      const msg = e instanceof ApiError ? e.message : 'Something went wrong.';
      push('jarvis', msg, { tone: 'error' });
      say(`Sorry ${address}. ${msg}`, { interrupt: true });
    }
  };

  const handleDraft = (d: Draft) => {
    langRef.current = d.reply_language === 'hi' ? 'hi' : 'en';
    const speakText = d.speak_text || d.spoken_reply;
    const lang: SpeakLang = d.speak_lang === 'hi' ? 'hi' : 'en';

    if (d.action === 'ask') {
      push('jarvis', d.spoken_reply);
      say(speakText, { interrupt: true, lang }); // "Ek second Sir…" while the search runs
      ask(d.ask_text, d.reply_language);
      return;
    }
    if (d.action === 'end') {
      setDraft(null);
      previousRef.current = '';
      reply(d.spoken_reply, speakText, lang, false);
      return;
    }
    if (d.action === 'briefing') {
      push('jarvis', d.spoken_reply);
      say(speakText, { interrupt: true, lang });
      briefing();
      return;
    }
    if (d.action === 'confirm_pending') {
      save(); // the AI understood a "yes" in whatever words were used
      return;
    }
    if (d.action === 'cancel_pending') {
      setDraft(null);
      previousRef.current = '';
      reply(d.spoken_reply || `Okay ${address}, cancelled.`, speakText, lang, false);
      return;
    }
    if (d.needs_clarification || d.action === 'choose_destination') {
      previousRef.current = d.transcript;
      if (CONFIRMABLE.includes(d.action) || d.action === 'choose_destination') setDraft(d);
      reply(d.spoken_reply, speakText, lang, true); // a question: listen for the answer
      return;
    }
    previousRef.current = '';
    if (CONFIRMABLE.includes(d.action)) {
      idemKey.current = newIdempotencyKey();
      editedRef.current = false;
      setDraftError('');
      setDraft(d); // a new card, or the corrected version of the waiting one
      reply(d.spoken_reply, speakText, lang, true); // "shall I save it?" is a question
      return;
    }
    // Anything else (an answer, small talk): say it and stop. A waiting card simply stays on screen.
    reply(d.spoken_reply, speakText, lang, false);
  };

  const ask = async (question: string, lang: string) => {
    setBusy('searching');
    try {
      const res = await api<AskAnswer>('/assistant/ask/', {
        method: 'POST',
        body: { question, lang, history: history() },
        timeoutMs: 75000,
      });
      setBusy('');
      const a = res.data;
      push('jarvis', a.text, { sources: a.sources });
      expectingRef.current = false;
      say(a.speak_text || a.text, { lang: a.speak_lang === 'hi' ? 'hi' : 'en' }); // queued after the holding line
    } catch (e) {
      setBusy('');
      const msg = e instanceof ApiError && e.code === 'timeout' ? 'That search took too long.' : "I couldn't get the answer.";
      push('jarvis', `${msg} Please try again.`, { tone: 'error' });
      say(`Sorry ${address}, ${msg} Please try again.`, { interrupt: true });
    }
  };

  const briefing = async () => {
    setBusy('thinking');
    try {
      const res = await api<{ text: string }>('/assistant/briefing/?kind=now', { timeoutMs: 45000 });
      setBusy('');
      push('jarvis', res.data.text);
      expectingRef.current = false;
      say(res.data.text);
    } catch {
      setBusy('');
      push('jarvis', "I couldn't prepare the briefing right now.", { tone: 'error' });
    }
  };

  const save = async () => {
    const d = draftRef.current;
    if (!d) return;
    cancelVoice();
    const isTask = TASK_DRAFTS.includes(d.action);
    if (isTask) {
      if (!d.title.trim()) return setDraftError('Please add what the task should say.');
      if (d.due_time && !HHMM.test(d.due_time)) return setDraftError('Time must look like 14:30.');
      if (d.due_time && !d.due_date) return setDraftError('Pick a day for this time.');
      if (d.action === 'delegation' && !d.staff) return setDraftError('Choose who should do this.');
      if (d.action === 'team_delegation' && !d.team?.members.length) return setDraftError('No one in that team can take this.');
    }
    setBusy('saving');
    setDraftError('');
    const body = {
      title: d.title.trim(),
      notes: d.notes,
      kind: d.kind,
      priority: d.priority,
      due_date: d.due_date || null,
      due_time: d.due_time ? d.due_time.padStart(5, '0') : null,
      duration_minutes: d.duration_minutes,
      remind_before_minutes: d.remind_before_minutes,
      announcement: d.announcement,
      source: 'VOICE',
    };
    const key = idemKey.current;
    try {
      let spoken = '';
      if (d.action === 'private_task') {
        await api('/private-tasks/', { method: 'POST', body, idempotencyKey: key });
        spoken = `Saved, ${address}.` + (d.due_date ? ` I'll remind you ${dayLabel(d.due_date).toLowerCase()}${d.due_time ? ` at ${d.due_time}` : ''}.` : '');
      } else if (d.action === 'delegation') {
        const res = await api<any>('/delegations/', {
          method: 'POST',
          body: { ...body, staff_id: d.staff?.id, category_key: d.category_key, description: d.transcript },
          idempotencyKey: key,
          timeoutMs: 30000,
        });
        const who = res.data?.assignee?.staff_name ?? 'the staff member';
        spoken = res.status === 202
          ? `${address}, sent to ${who}. I'm waiting for the staff app to confirm and will tell you.`
          : `Done, ${address}. ${who} has the task now. I'll keep track of it.`;
      } else if (d.action === 'team_delegation') {
        const res = await api<{ assigned: string[]; pending: string[]; not_assigned: { name: string; reason: string }[] }>(
          '/delegations/team/',
          {
            method: 'POST',
            body: { ...body, staff_ids: d.team?.members.map((m) => m.id) ?? [], category_key: d.category_key, description: d.transcript },
            idempotencyKey: key,
            timeoutMs: 120000,
          },
        );
        const r = res.data;
        spoken = `${address}, given to ${r.assigned.length} ${r.assigned.length === 1 ? 'person' : 'people'}.`;
        if (r.pending.length) spoken += ` ${r.pending.length} still being confirmed.`;
        if (r.not_assigned.length) spoken += ` Could not give it to: ${r.not_assigned.map((n) => n.name).join(', ')}.`;
      } else if (d.action === 'pa_task' || d.action === 'pa_meeting') {
        const meeting = d.action === 'pa_meeting';
        const res = await api<any>('/pa-tasks/', {
          method: 'POST',
          body: { ...body, description: d.transcript, meeting_with_pa: meeting },
          idempotencyKey: key,
        });
        const who = res.data?.assignee?.staff_name ?? d.pa_name ?? 'your PA';
        spoken = meeting
          ? `Saved, ${address}. I've told ${who}, and I'll remind you both.`
          : `Given to ${who}, ${address}. I'll remind you both and tell you when it's done.`;
      } else if (d.action === 'md_request') {
        await api('/requests/', { method: 'POST', body, idempotencyKey: key });
        spoken = `Sent to Sir, ${address}. I'll tell you when he answers.`;
      } else if (d.action === 'accept_request' || d.action === 'decline_request') {
        if (!d.target) return setBusy('');
        const verb = d.action === 'accept_request' ? 'accept' : 'decline';
        await api(`/requests/${d.target.id}/${verb}/`, { method: 'POST', body: {}, idempotencyKey: key });
        spoken = verb === 'accept' ? `Accepted, ${address}. It's on your list.` : `Declined, ${address}.`;
      } else if (d.action === 'complete') {
        const t = d.target;
        if (!t) return setBusy('');
        const path = t.scope === 'private' ? `/private-tasks/${t.id}/complete/` : `/shared-tasks/${t.id}/complete/`;
        const res = await api(path, { method: 'POST', body: { expected_version: t.version ?? 1 } });
        spoken = res.status === 202 ? `Sent to Sir to confirm, ${address}.` : `Marked as done, ${address}.`;
      } else if (d.action === 'remember') {
        await api('/memories/', { method: 'POST', body: { text: d.memory_text }, idempotencyKey: key });
        spoken = `Done, ${address}. I'll remember that.`;
      }
      setBusy('');
      setDraft(null);
      previousRef.current = '';
      dataChanged.emit();
      Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success).catch(() => undefined);
      if (d.done_reply && !editedRef.current && AI_DONE_OK.includes(d.action)) {
        reply(d.done_reply, d.done_speak || d.done_reply, d.done_lang === 'hi' ? 'hi' : 'en', false, { tone: 'saved' });
      } else {
        reply(spoken, undefined, 'en', false, { tone: 'saved' });
      }
    } catch (e) {
      setBusy('');
      const msg = e instanceof ApiError ? e.message : 'Could not save.';
      setDraftError(`${msg} Nothing was saved${e instanceof ApiError && e.retryable ? ' — tap the button to try again' : ''}.`);
      say(`Sorry ${address}, it was not saved. ${msg}`, { interrupt: true });
    }
  };

  const cancelDraft = () => {
    cancelVoice();
    stopSpeaking();
    expectingRef.current = false;
    setDraft(null);
    previousRef.current = '';
    push('jarvis', 'Cancelled. Nothing was saved.');
  };

  const editDraft = (patch: Partial<Draft>) => {
    if (!draftRef.current) return;
    editedRef.current = true;
    setDraft({ ...draftRef.current, ...patch });
  };

  const micPress = () => {
    if (voice.state === 'listening') {
      stopVoice();
      return;
    }
    stopSpeaking();
    silenceRef.current = 0;
    listen();
  };

  const send = (text: string) => {
    const t = text.trim();
    if (!t) return;
    cancelVoice();
    stopSpeaking();
    heard(t);
  };

  const clear = () => {
    cancelVoice();
    stopSpeaking();
    messagesRef.current = [];
    setMessages([]);
    setDraft(null);
    previousRef.current = '';
    expectingRef.current = false;
  };

  const greet = () => {
    push('jarvis', isMD ? 'Ji Sir?' : `Ji ${user?.name ?? ''}?`);
    expectingRef.current = true;
    say(isMD ? 'जी सर?' : 'जी?', { lang: 'hi', interrupt: true, onDone: listen });
  };

  useEffect(() => {
    engine.current = { heard, voiceError };
  });

  // Speaking indicator; going to the background releases the mic and stops talking.
  useEffect(() => {
    const unsubscribe = onSpeakingChange(setSpeaking);
    const sub = AppState.addEventListener('change', (state) => {
      if (state !== 'active') {
        cancelVoice();
        stopSpeaking();
      }
    });
    return () => {
      unsubscribe();
      sub.remove();
    };
  }, [cancelVoice]);

  // Signing out clears everything.
  const prevUser = useRef(user);
  useEffect(() => {
    if (prevUser.current && !user) {
      const kick = setTimeout(clear, 0);
      return () => clearTimeout(kick);
    }
    prevUser.current = user;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user]);

  const listening = voice.state === 'listening';
  const orb: OrbState = listening ? 'listening' : busy ? 'thinking' : speaking ? 'speaking' : 'idle';
  const status = listening ? 'Listening…'
    : busy === 'searching' ? 'Searching the internet…'
    : busy === 'thinking' ? 'Thinking…'
    : busy === 'saving' ? 'Saving…'
    : speaking ? 'Speaking… tap to interrupt'
    : draft && !draft.needs_clarification && draft.action !== 'choose_destination' ? 'Waiting for your answer · tap to speak'
    : draft ? 'Tap to answer'
    : 'Tap and speak';

  const value: ConversationValue = {
    messages, draft, draftError, busy, speaking, listening, partial: voice.partial, orb, status,
    micPress, send, editDraft, cancelDraft, save, clear, greet,
  };
  return <ConversationContext.Provider value={value}>{children}</ConversationContext.Provider>;
}

export function useConversation() {
  const ctx = useContext(ConversationContext);
  if (!ctx) throw new Error('useConversation must be inside ConversationProvider');
  return ctx;
}
