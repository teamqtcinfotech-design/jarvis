/**
 * The JARVIS conversation, shared by the main screen (orb) and the full-transcript view.
 *
 * Rules:
 * - Two-way, like a phone call: once the user taps the orb or says "Hey JARVIS", JARVIS listens, replies,
 *   and listens again after every reply, until the user says "bas" / "thank you" or goes quiet.
 * - Quiet handling: a couple of silent re-listens (he may be thinking), then one gentle "Sir?", then a couple
 *   more, then JARVIS rests (tap or "Hey JARVIS" wakes it again). Silence never means "he's gone".
 * - While a card waits, everything said goes to the AI with a description of the card; the AI decides
 *   whether it was a yes, a no, a change, or something unrelated. No fixed words.
 * - Audio never leaves the phone; only the recognised text goes to our server.
 */
import * as Haptics from 'expo-haptics';
import { router } from 'expo-router';
import React, { createContext, useContext, useEffect, useRef, useState } from 'react';
import { AppState } from 'react-native';

import { type HeldOffer, useAlertEngine } from './alertEngine';
import { ApiError, api, currentServer, deviceTokens, newIdempotencyKey } from './api';
import { dataChanged } from './bus';
import { CONFIRMABLE, TASK_DRAFTS, describeDraft } from './drafts';
import { dayLabel } from './format';
import { usePrefs } from './prefs';
import { useSession } from './session';
import { onSpeakingChange, say, stopSpeaking, type SpeakLang } from './speech';
import type { AskAnswer, Draft, DraftAction, StaffOption } from './types';
import { SILENCE_CODES, useVoiceCapture } from './voice';
import { onWake, wakeConsumePending, wakePause, wakeResume, wakeStart, wakeStatus, wakeSupported } from './wake';

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
// Quiet rounds (each about 6 s of the recogniser hearing nothing): re-listen silently, nudge once, re-listen, rest.
const NUDGE_AFTER_ROUNDS = 2;
const REST_AFTER_ROUNDS = 5;

let seq = 0;
const newId = () => `m${Date.now()}-${seq++}`;

export function ConversationProvider({ children }: { children: React.ReactNode }) {
  const { user } = useSession();
  const { prefs } = usePrefs();
  const { setVoiceProbe, offer, acceptOffer, declineOffer, setHeldVisible, held } = useAlertEngine();
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
  const offerRef = useRef<HeldOffer | null>(null); // "shall I read them?" is waiting for an answer
  const presentedOffer = useRef(0);
  const wokeAtRef = useRef(0);
  // JARVIS just asked "which one?": the options, so the next words are matched against them by the AI.
  const choicesRef = useRef<{ intent: 'complete_task' | 'accept_request' | 'decline_request'; items: { id: string; title: string }[] } | null>(null);
  const fns = useRef<{
    heard: (t: string) => void;
    voiceError: (m: string, c: string) => void;
    presentOffer: (o: HeldOffer, greetFirst: boolean) => void;
    wake: () => void;
  }>({ heard: () => undefined, voiceError: () => undefined, presentOffer: () => undefined, wake: () => undefined });

  const voice = useVoiceCapture(prefs.listenLang, {
    hints,
    onFinal: (text) => fns.current.heard(text),
    onError: (message, code) => fns.current.voiceError(message, code),
  });
  const { start: startVoice, stop: stopVoice, cancel: cancelVoice, probe } = voice;

  // The alert engine borrows the microphone for its 2-second "is anyone talking?" check
  // (the background listener steps aside for it).
  useEffect(() => {
    setVoiceProbe(async () => {
      wakePause();
      await new Promise((r) => setTimeout(r, 250));
      try {
        return await probe();
      } finally {
        wakeResume();
      }
    });
    return () => setVoiceProbe(null);
  }, [setVoiceProbe, probe]);

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
    if (wakeSupported && prefs.wakeWordEnabled) {
      // One microphone user at a time: the background listener lets go first, then the recogniser starts.
      wakePause();
      setTimeout(startVoice, 250);
    } else {
      startVoice();
    }
  };
  /** Show a JARVIS line, speak it, and (unless the conversation is over) listen again. */
  const reply = (show: string, speakText: string | undefined, lang: SpeakLang, keepOpen: boolean, extra: Partial<Msg> = {}) => {
    push('jarvis', show, extra);
    expectingRef.current = keepOpen;
    silenceRef.current = 0;
    say(speakText || show, { interrupt: true, lang, fallback: show, onDone: keepOpen ? listen : undefined });
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
    setHeldVisible(false); // he is talking: nothing should sit on top of the conversation
    silenceRef.current = 0;
    expectingRef.current = false;
    push('user', text);
    const pendingOffer = offerRef.current;
    if (pendingOffer) {
      interpret(text, '', `JARVIS asked: "${pendingOffer.text}" (${pendingOffer.count} alerts kept while away)`);
      return;
    }
    const card = waitingCard();
    if (card) interpret(text, card.transcript, describeDraft(card));
    else interpret(text, previousRef.current, '', choicesRef.current);
  };

  const voiceError = (message: string, code: string) => {
    if (SILENCE_CODES.has(code)) {
      if (!expectingRef.current) return; // the user tapped once and said nothing: just rest
      silenceRef.current += 1;
      if (silenceRef.current < NUDGE_AFTER_ROUNDS) {
        listen(); // he may be thinking: keep listening quietly
      } else if (silenceRef.current === NUDGE_AFTER_ROUNDS) {
        const hi = hiLine(`${address}?`, isMD ? 'सर?' : `${address}?`);
        if (hi) say(hi.speak, { interrupt: true, lang: 'hi', fallback: hi.show, onDone: listen });
        else say(`${address}?`, { interrupt: true, onDone: listen });
      } else if (silenceRef.current < REST_AFTER_ROUNDS) {
        listen();
      } else {
        // Rest. A tap or "Hey JARVIS" starts the conversation again; nothing is lost.
        silenceRef.current = 0;
        expectingRef.current = false;
        if (offerRef.current) {
          offerRef.current = null;
          declineOffer(); // no answer: they stay in the list, quietly
        }
      }
      return;
    }
    expectingRef.current = false;
    push('jarvis', message, { tone: 'error' });
  };

  // "Sir, while you were away: … Shall I read them?" — asked once JARVIS is idle (or right after "Hey JARVIS").
  const presentOffer = (o: HeldOffer, greetFirst: boolean) => {
    offerRef.current = o;
    const hi = langRef.current === 'hi';
    const show = (greetFirst ? (isMD ? 'Ji Sir? ' : 'Ji? ') : '') + o.text;
    const speak = (greetFirst ? (isMD ? 'जी सर? ' : 'जी? ') : '') + (hi ? o.textHi : o.text);
    push('jarvis', show);
    expectingRef.current = true;
    silenceRef.current = 0;
    say(speak, { interrupt: true, lang: hi || greetFirst ? 'hi' : 'en', fallback: show, onDone: listen });
  };

  // Woken by "Hey JARVIS": come to the orb screen, greet (and offer held alerts in the same breath), listen.
  const wake = () => {
    if (!user) return;
    setHeldVisible(false);
    try {
      router.navigate('/');
    } catch {
      // not mounted yet; the greeting still works
    }
    if (offer && offer.id !== presentedOffer.current) {
      presentedOffer.current = offer.id;
      presentOffer(offer, true);
      return;
    }
    greet();
  };

  const interpret = async (text: string, previous: string, awaiting: string, choices: typeof choicesRef.current = null) => {
    setBusy('thinking');
    try {
      const res = await api<Draft>('/assistant/interpret/', {
        method: 'POST',
        body: { text, previous, awaiting, choices, history: history().slice(0, -1) },
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

    const pendingOffer = offerRef.current;
    if (pendingOffer) {
      offerRef.current = null;
      if (d.action === 'confirm_pending') {
        // Read aloud by the engine; shown here as text so he can see what was read.
        const lines = held.map((h) => `• ${h.isPrivate && h.concealed ? 'A private reminder' : h.title}`);
        push('jarvis', `While you were away:\n${lines.join('\n')}`);
        expectingRef.current = true;
        silenceRef.current = 0;
        acceptOffer();
        say('', { onDone: listen }); // queued after the read-out, then JARVIS listens again
        return;
      }
      if (d.action === 'cancel_pending') {
        declineOffer();
        const hi = hiLine(`Okay ${address}, they're in your list.`, `ठीक है${isMD ? ' सर' : ''}, वो आपकी लिस्ट में हैं।`);
        if (hi) reply(hi.show, hi.speak, 'hi', false);
        else reply(`Okay ${address}, they're in your list.`, undefined, 'en', false);
        return;
      }
      declineOffer(); // he asked for something else: leave them in the list and carry on
    }

    if (d.action === 'ask') {
      push('jarvis', d.spoken_reply);
      say(speakText, { interrupt: true, lang, fallback: d.spoken_reply }); // "Ek second Sir…" while the search runs
      ask(d.ask_text, d.reply_language);
      return;
    }
    if (d.action === 'end') {
      setDraft(null);
      previousRef.current = '';
      reply(d.spoken_reply, speakText, lang, false); // "bas / thank you": the conversation is over
      return;
    }
    if (d.action === 'briefing') {
      push('jarvis', d.spoken_reply);
      say(speakText, { interrupt: true, lang, fallback: d.spoken_reply });
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
      reply(d.spoken_reply || `Okay ${address}, cancelled.`, speakText, lang, true);
      return;
    }
    choicesRef.current = null;
    if (d.needs_clarification || d.action === 'choose_destination') {
      previousRef.current = d.transcript;
      if (CONFIRMABLE.includes(d.action) || d.action === 'choose_destination') setDraft(d);
      // "Which one?" — remember the options so the answer ("both", "the franchise one") is matched against them.
      const intent = d.action === 'complete' ? 'complete_task' : d.action === 'accept_request' || d.action === 'decline_request' ? d.action : null;
      if (intent && d.target_candidates?.length) {
        choicesRef.current = { intent, items: d.target_candidates.map((c) => ({ id: c.id, title: c.title })) };
      }
      reply(d.spoken_reply, speakText, lang, true); // a question: listen for the answer
      return;
    }
    previousRef.current = '';
    if (CONFIRMABLE.includes(d.action)) {
      idemKey.current = newIdempotencyKey();
      editedRef.current = false;
      setDraftError('');
      setDraft(d); // a new card, or the corrected version of the waiting one
      reply(d.spoken_reply, speakText, lang, true); // "shall I save it?"
      return;
    }
    // Anything else (an answer, small talk): say it and keep listening. A waiting card simply stays on screen.
    reply(d.spoken_reply, speakText, lang, true);
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
      expectingRef.current = true;
      silenceRef.current = 0;
      say(a.speak_text || a.text, { lang: a.speak_lang === 'hi' ? 'hi' : 'en', fallback: a.text, onDone: listen }); // queued after the holding line
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
      expectingRef.current = true;
      silenceRef.current = 0;
      say(res.data.text, { onDone: listen });
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
        const list = d.targets?.length ? d.targets : d.target ? [d.target] : [];
        if (!list.length) return setBusy('');
        const verb = d.action === 'accept_request' ? 'accept' : 'decline';
        for (const t of list) {
          await api(`/requests/${t.id}/${verb}/`, { method: 'POST', body: {}, idempotencyKey: `${key}-${t.id}` });
        }
        spoken = list.length === 1
          ? (verb === 'accept' ? `Accepted, ${address}. It's on your list.` : `Declined, ${address}.`)
          : `${verb === 'accept' ? 'Accepted' : 'Declined'} all ${list.length}, ${address}.`;
      } else if (d.action === 'complete') {
        const list = d.targets?.length ? d.targets : d.target ? [d.target] : [];
        if (!list.length) return setBusy('');
        let sentToSir = 0;
        for (const t of list) {
          const path = t.scope === 'private' ? `/private-tasks/${t.id}/complete/` : `/shared-tasks/${t.id}/complete/`;
          const res = await api(path, { method: 'POST', body: { expected_version: t.version ?? 1 } });
          if (res.status === 202) sentToSir += 1;
        }
        spoken = list.length === 1
          ? (sentToSir ? `Sent to Sir to confirm, ${address}.` : `Marked as done, ${address}.`)
          : `Marked ${list.length} as done, ${address}.` + (sentToSir ? ` ${sentToSir} sent to Sir to confirm.` : '');
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
        reply(d.done_reply, d.done_speak || d.done_reply, d.done_lang === 'hi' ? 'hi' : 'en', true, { tone: 'saved' });
      } else {
        reply(spoken, undefined, 'en', true, { tone: 'saved' });
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
    setHeldVisible(false);
    silenceRef.current = 0;
    expectingRef.current = true; // a tap opens the conversation: JARVIS keeps listening after each reply
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
    const show = isMD ? 'Ji Sir?' : `Ji ${user?.name ?? ''}?`;
    push('jarvis', show);
    expectingRef.current = true;
    say(isMD ? 'जी सर?' : 'जी?', { lang: 'hi', interrupt: true, fallback: show, onDone: listen });
  };

  useEffect(() => {
    fns.current = { heard, voiceError, presentOffer, wake };
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

  const listening = voice.state === 'listening';

  // A held-alerts offer is asked as soon as JARVIS is idle (not mid-sentence, not mid-search). Right after
  // "Hey JARVIS" the held list may arrive a moment late: if he has not started talking yet, ask it then.
  useEffect(() => {
    if (!offer || offer.id === presentedOffer.current || busy || speaking) return;
    const justWoke = Date.now() - wokeAtRef.current < 8000 && !voice.partial;
    if (listening && !justWoke) return;
    presentedOffer.current = offer.id;
    const kick = setTimeout(() => {
      if (listening) cancelVoice();
      fns.current.presentOffer(offer, false);
    }, 0);
    return () => clearTimeout(kick);
  }, [offer, listening, busy, speaking, voice.partial, cancelVoice]);

  // "Hey JARVIS": the native listener either tells us directly (app open) or brings the app to the front
  // with a pending flag (app was closed). One microphone user at a time: it pauses while JARVIS talks or listens.
  useEffect(() => {
    if (!wakeSupported) return;
    const fire = () => {
      wokeAtRef.current = Date.now();
      fns.current.wake();
    };
    const off = onWake(fire);
    const sub = AppState.addEventListener('change', (s) => {
      if (s === 'active' && wakeConsumePending()) fire();
    });
    const kick = setTimeout(() => {
      if (wakeConsumePending()) fire();
    }, 400);
    return () => {
      off();
      sub.remove();
      clearTimeout(kick);
    };
  }, []);
  useEffect(() => {
    if (!wakeSupported || !prefs.wakeWordEnabled) return;
    if (listening || speaking || busy) wakePause();
    else wakeResume();
  }, [listening, speaking, busy, prefs.wakeWordEnabled]);

  // After a reboot, a reinstall, or Xiaomi stopping it: switch the listener back on whenever the app comes
  // to the front (it is a background service, so Android needs the app itself to restart it).
  useEffect(() => {
    if (!wakeSupported || !prefs.wakeWordEnabled || !user) return;
    let cancelled = false;
    const ensure = () => {
      wakeStatus()
        .then(async (st) => {
          if (cancelled || !st.supported || st.running || !st.micAllowed) return;
          const tokens = await deviceTokens();
          if (cancelled) return;
          await wakeStart({
            serverUrl: currentServer(),
            accessToken: tokens.access,
            refreshToken: tokens.refresh,
            address: user.role === 'MD' ? 'Sir' : user.name,
            speakWhenLocked: prefs.privateAloudWhenLocked,
          });
        })
        .catch(() => undefined);
    };
    const kick = setTimeout(ensure, 800);
    const sub = AppState.addEventListener('change', (s) => s === 'active' && ensure());
    return () => {
      cancelled = true;
      clearTimeout(kick);
      sub.remove();
    };
  }, [prefs.wakeWordEnabled, prefs.privateAloudWhenLocked, user]);

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

  const orb: OrbState = listening ? 'listening' : busy ? 'thinking' : speaking ? 'speaking' : 'idle';
  const status = listening ? 'Listening…'
    : busy === 'searching' ? 'Searching the internet…'
    : busy === 'thinking' ? 'Thinking…'
    : busy === 'saving' ? 'Saving…'
    : speaking ? 'Speaking… tap to interrupt'
    : offer ? 'Shall I read them? · tap to answer'
    : draft && !draft.needs_clarification && draft.action !== 'choose_destination' ? 'Waiting for your answer · tap to speak'
    : draft ? 'Tap to answer'
    : prefs.wakeWordEnabled ? 'Say “Hey JARVIS”, or tap and speak'
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
