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
import { Animated, AppState } from 'react-native';

import { type AlertQuestion, type EngineItem, type HeldOffer, questionFor, useAlertEngine } from './alertEngine';
import { addressHiOf, addressOf, mdHe, mdWord, mdWordHi } from './names';
import { useDashboard } from './useDashboard';
import { ApiError, api, currentServer, deviceTokens, newIdempotencyKey } from './api';
import { dataChanged } from './bus';
import { downloadCsv, downloadPdf, shareText } from './download';
import { CONFIRMABLE, TASK_DRAFTS, describeDraft } from './drafts';
import { dayLabel, timeLabel } from './format';
import { usePrefs } from './prefs';
import { useSession } from './session';
import { onSpeakingChange, say, stopSpeaking, type SpeakLang } from './speech';
import type { AskAnswer, DataView, Draft, DraftAction, StaffOption } from './types';
import { SILENCE_CODES, useVoiceCapture } from './voice';
import { onWake, pushToken, wakeConsumePending, wakeNoteEmpty, wakePause, wakeResume, wakeStart, wakeStatus, wakeSupported } from './wake';

export interface Msg {
  id: string;
  role: 'user' | 'jarvis';
  text: string;
  tone?: 'error' | 'saved';
  sources?: AskAnswer['sources'];
  /** When it was said (so the screen can tell whether an answer card is still the latest thing). */
  at: number;
}

export type OrbState = 'idle' | 'listening' | 'thinking' | 'speaking';
type Busy = '' | 'thinking' | 'searching' | 'saving';

/** An answer drawn as data (timeline / chart / table), kept so it can be read, downloaded or removed. */
export interface Answer {
  id: string;
  view: DataView;
  question: string;
  /** Spoken text that goes with it (a briefing). */
  text?: string;
  at: number;
}

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
  /** 0..1 loudness while listening, for the orb. */
  level: Animated.Value;
  answers: Answer[];
  removeAnswer: (id: string) => void;
  clearAnswers: () => void;
  /** Fetch one report / view and show it as an answer card (the Reports row; a tap on a person). `query` = its filters. */
  showView: (kind: string, label: string, query?: string) => void;
  /** A task he asked to open by voice ("pehla wala kholo"): the home screen opens its sheet, then clears this. */
  itemToOpen: string;
  clearItemToOpen: () => void;
  /** Phone only: the full-screen talk sheet. Opened by the floating orb, "Hey JARVIS" and held-alert offers. */
  talkOpen: boolean;
  /** When the talk sheet was last opened (a tap, "Hey JARVIS", an offer): answers older than that are not "current". */
  talkOpenedAt: number;
  /** How it was opened: by voice (the orb in the middle) or to type (the pencil). */
  talkMode: 'voice' | 'type';
  openTalk: (mode?: 'voice' | 'type') => void;
  closeTalk: () => void;
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
const AI_DONE_OK: DraftAction[] = ['private_task', 'pa_task', 'pa_meeting', 'md_request', 'pa_self_task', 'remember',
  'accept_request', 'decline_request', 'complete'];
const BASE_HINTS = ['JARVIS', 'Jarvis', 'Hey JARVIS'];
// Quiet rounds (each about 6 s of the recogniser hearing nothing). While JARVIS is waiting on him (a question, a
// card, just woken): listen once more, ask "Sir?" once, listen, then rest. After a plain answer: rest after the
// first quiet round. Resting hands the microphone back to "Hey JARVIS", which cannot listen while this does.
const NUDGE_AFTER_ROUNDS = 2;
const REST_AFTER_ROUNDS = 3;
// "Hey JARVIS" as the phone's speech recogniser writes it, at the start of what was heard.
const WAKE_PHRASE = /^\s*(?:(?:hey|hi|hello|ok|okay|he|hay|hai|a|oh|हे|हाय|हेलो)[\s,]+)?(?:jarvis|jarwis|jarvish|jervis|jarves|javis|jarbis|जार्विस|जारविस)[\s,.!?]*/i;
// Words the room says on its own. Heard right after a wake, with nothing else, they mean the wake was false.
const FILLER = new Set(['ok', 'okay', 'k', 'haan', 'ha', 'han', 'hmm', 'hm', 'mm', 'yes', 'no', 'nah', 'hello', 'hi', 'hey', 'achha', 'accha',
  'acha', 'theek', 'thik', 'hai', 'ji', 'kya', 'what', 'oh', 'ah', 'um', 'uh', 'yeah', 'yep', 'huh', 'so', 'ठीक', 'हाँ', 'हां', 'जी',
  'अच्छा', 'क्या', 'हैं', 'है', 'नहीं']);
const isFiller = (text: string) => {
  const words = text.toLowerCase().replace(/[^\p{L}\p{N} ]/gu, ' ').split(/\s+/).filter(Boolean);
  return words.length > 0 && words.length <= 2 && words.every((w) => FILLER.has(w));
};

let seq = 0;
const newId = () => `m${Date.now()}-${seq++}`;

export function ConversationProvider({ children }: { children: React.ReactNode }) {
  const { user } = useSession();
  const { prefs, applyServerPrefs } = usePrefs();
  const { setVoiceProbe, setTalking, acknowledgeByRef, acknowledge, current: currentAlert, offer, acceptOffer, declineOffer,
    setHeldVisible, held, setPresenter, answerQuestion } = useAlertEngine();
  const { data: dash } = useDashboard();
  const paName = dash?.pa_name ?? '';
  const isMD = user?.role === 'MD';
  const address = addressOf(user);
  const addressHi = addressHiOf(user);
  const md = mdWord(dash);            // how the MD is referred to on the PA's side ("Sent to Ma'am")
  const mdHi = mdWordHi(dash);

  const [messages, setMessages] = useState<Msg[]>([]);
  const [draft, setDraftState] = useState<Draft | null>(null);
  const [draftError, setDraftError] = useState('');
  const [busy, setBusy] = useState<Busy>('');
  const [speaking, setSpeaking] = useState(false);
  const [hints, setHints] = useState<string[]>(BASE_HINTS);
  const [answers, setAnswers] = useState<Answer[]>([]);
  const [talkOpen, setTalkOpen] = useState(false);
  const [talkOpenedAt, setTalkOpenedAt] = useState(0);
  const [talkMode, setTalkMode] = useState<'voice' | 'type'>('voice');
  const [itemToOpen, setItemToOpen] = useState('');
  const answersRef = useRef<Answer[]>([]); // what is on screen, for "is list ka PDF" and "pehla wala"
  const lastQuestion = useRef('');

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
  const calledRef = useRef(false); // he has just called JARVIS ("Ji Sir?") and has not said anything yet
  // JARVIS just spoke an alert and asked its question ("Accept it?", "Done, or later?"): the next words answer it.
  const questionRef = useRef<{ item: EngineItem; q: AlertQuestion; spoken: string } | null>(null);
  // JARVIS just asked "which one?": the options, so the next words are matched against them by the AI.
  const choicesRef = useRef<{
    intent: 'complete_task' | 'accept_request' | 'decline_request' | 'change_task' | 'delete_task';
    items: { id: string; title: string }[];
  } | null>(null);
  const fns = useRef<{
    heard: (t: string) => void;
    voiceError: (m: string, c: string) => void;
    presentOffer: (o: HeldOffer, greetFirst: boolean) => void;
    presentAlert: (item: EngineItem, spoken: string) => void;
    wake: () => void;
  }>({ heard: () => undefined, voiceError: () => undefined, presentOffer: () => undefined, presentAlert: () => undefined,
    wake: () => undefined });

  const voice = useVoiceCapture(prefs.listenLang, {
    hints,
    onFinal: (text) => fns.current.heard(text),
    onError: (message, code) => fns.current.voiceError(message, code),
  });
  const { start: startVoice, cancel: cancelVoice, probe, level } = voice;

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
    messagesRef.current = [...messagesRef.current, { id: newId(), role, text, at: Date.now(), ...extra }];
    setMessages(messagesRef.current);
  };
  const setDraft = (d: Draft | null) => {
    draftRef.current = d;
    setDraftState(d);
  };
  const addAnswer = (view: DataView, question: string, text?: string) => {
    setAnswers((prev) => [{ id: newId(), view, question, text, at: Date.now() }, ...prev].slice(0, 20));
  };
  const removeAnswer = (id: string) => setAnswers((prev) => prev.filter((a) => a.id !== id));
  const clearAnswers = () => setAnswers([]);
  const clearItemToOpen = () => setItemToOpen('');
  /** The data card he is looking at, in a few plain fields, sent along with what he says. */
  const onScreen = () => {
    const top = answersRef.current[0];
    if (!top) return null;
    return {
      kind: top.view.kind,
      title: top.view.title,
      items: top.view.rows.slice(0, 15).filter((r) => r.id && r.title).map((r) => ({ id: r.id, title: r.title })),
      // The request this card answered, so "sirf meetings" / "sab dikhao" start from the same list.
      filters: top.view.request ?? null,
    };
  };
  const showView = async (kind: string, label: string, query = '') => {
    setBusy('thinking');
    try {
      const res = await api<DataView>(`/views/${kind}/${query ? `?${query}` : ''}`);
      addAnswer(res.data, label);
    } catch {
      push('jarvis', `Couldn't load ${label.toLowerCase()} right now.`, { tone: 'error' });
    } finally {
      setBusy('');
    }
  };
  const openTalk = (mode: 'voice' | 'type' = 'voice') => {
    setTalkOpen(true);
    setTalkMode(mode);
    setTalkOpenedAt(Date.now());
  };
  // Closing the sheet ends the conversation too: the microphone and the voice stop, so nothing keeps running
  // out of sight (and "Hey JARVIS" is listening again at once).
  const closeTalk = () => {
    setTalkOpen(false);
    expectingRef.current = false;
    silenceRef.current = 0;
    calledRef.current = false;
    questionRef.current = null; // an unanswered alert question stays on its banner, nothing more is asked
    cancelVoice();
    stopSpeaking();
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
  const heard = (said: string) => {
    Haptics.selectionAsync().catch(() => undefined); // instant "got it" tick
    setHeldVisible(false); // he is talking: nothing should sit on top of the conversation
    silenceRef.current = 0;
    expectingRef.current = false;
    // "Hey JARVIS" said while JARVIS was already listening is a call, not an instruction: answer it here, at
    // once. With more after it ("Hey JARVIS, kal 5 baje…") only the rest is the instruction.
    const text = said.replace(WAKE_PHRASE, '').trim();
    if (!text) {
      greet();
      return;
    }
    // Just called, and all that followed was a stray word from the room ("okay", "haan"): the wake was false.
    // Close quietly instead of answering it.
    if (calledRef.current && !questionRef.current && isFiller(text)) {
      calledRef.current = false;
      expectingRef.current = false;
      wakeNoteEmpty();
      return;
    }
    calledRef.current = false;
    push('user', text);
    lastQuestion.current = text;
    const pendingOffer = offerRef.current;
    if (pendingOffer) {
      interpret(text, '', `JARVIS asked: "${pendingOffer.text}" (${pendingOffer.count} alerts kept while away)`);
      return;
    }
    const pq = questionRef.current;
    if (pq) {
      // The answer to the alert's question; anything else he says is understood as itself (the banner stays).
      interpret(text, '', `JARVIS asked: "${pq.q.en || pq.spoken}" about "${pq.item.title}"`);
      return;
    }
    const card = waitingCard();
    // The card itself goes along too, so a "yes" can take the time JARVIS offered, or a new time can be checked.
    if (card) interpret(text, card.transcript, describeDraft(card), null, card);
    else interpret(text, previousRef.current, '', choicesRef.current);
  };

  const voiceError = (message: string, code: string) => {
    if (SILENCE_CODES.has(code)) {
      if (!expectingRef.current) return; // the user tapped once and said nothing: just rest
      silenceRef.current += 1;
      // An alert's question with no answer: one more listen, then it stays on its banner, nothing more is said.
      if (questionRef.current) {
        if (silenceRef.current < 2) listen();
        else {
          questionRef.current = null;
          silenceRef.current = 0;
          expectingRef.current = false;
        }
        return;
      }
      // Woken by "Hey JARVIS", greeted, and nothing said (most likely a false wake): two quiet rounds, then rest
      // without a word. No "Sir?" nudge for a call that never had anything behind it.
      const calledOnly = calledRef.current && !draftRef.current && !offerRef.current && !choicesRef.current && !previousRef.current;
      if (calledOnly) {
        if (silenceRef.current < 2) listen();
        else {
          silenceRef.current = 0;
          expectingRef.current = false;
          calledRef.current = false;
          wakeNoteEmpty();
        }
        return;
      }
      // Is JARVIS waiting on him (a card, a question, a "which one?", an offer)?
      const waiting = !!draftRef.current || !!offerRef.current || !!choicesRef.current || !!previousRef.current;
      if (!waiting) {
        // He got his answer and said nothing more: stop here so "Hey JARVIS" is listening again straight away.
        silenceRef.current = 0;
        expectingRef.current = false;
        return;
      }
      if (silenceRef.current < NUDGE_AFTER_ROUNDS) {
        listen(); // he may be thinking: keep listening quietly
      } else if (silenceRef.current === NUDGE_AFTER_ROUNDS) {
        const hi = hiLine(`${address}?`, `${addressHi}?`);
        if (hi) say(hi.speak, { interrupt: true, lang: 'hi', fallback: hi.show, onDone: listen });
        else say(`${address}?`, { interrupt: true, onDone: listen });
      } else if (silenceRef.current < REST_AFTER_ROUNDS) {
        listen();
      } else {
        // Rest. A tap or "Hey JARVIS" starts the conversation again; nothing is lost.
        silenceRef.current = 0;
        expectingRef.current = false;
        calledRef.current = false;
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
    openTalk();
    const hi = langRef.current === 'hi';
    const show = (greetFirst ? `Ji ${address}? ` : '') + o.text;
    const speak = (greetFirst ? `जी ${isMD ? addressHi : ''}? `.replace(' ?', '?') : '') + (hi ? o.textHi : o.text);
    push('jarvis', show);
    expectingRef.current = true;
    silenceRef.current = 0;
    say(speak, { interrupt: true, lang: hi || greetFirst ? 'hi' : 'en', fallback: show, onDone: listen });
  };

  // An alert JARVIS brings to him by voice (a tapped notification, or one that is due while he is here): come to
  // the orb screen, say it, ask its one question, and listen once for the answer.
  const presentAlert = (item: EngineItem, spoken: string) => {
    const q = questionFor(item, isMD, { en: md, hi: mdHi });
    questionRef.current = q ? { item, q, spoken } : null;
    offerRef.current = null;
    setHeldVisible(false);
    openTalk();
    try {
      router.navigate('/');
    } catch {
      // not mounted yet; the voice still works
    }
    const line = q?.en ? `${spoken} ${q.en}` : spoken;
    push('jarvis', line);
    expectingRef.current = !!q;
    silenceRef.current = 0;
    calledRef.current = false;
    say(line, { interrupt: true, onDone: q ? listen : undefined });
  };

  // Woken by "Hey JARVIS": come to the orb screen, greet (and offer held alerts in the same breath), listen.
  const wake = () => {
    if (!user) return;
    setHeldVisible(false);
    openTalk();
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

  const interpret = async (text: string, previous: string, awaiting: string, choices: typeof choicesRef.current = null,
    pending: Draft | null = null) => {
    setBusy('thinking');
    try {
      // The waiting card without its long option lists (the server only needs what it says and when).
      const slim = pending ? { ...pending, categories: [], staff_candidates: [], target_candidates: [] } : null;
      const res = await api<Draft>('/assistant/interpret/', {
        method: 'POST',
        body: { text, previous, awaiting, choices, pending: slim, history: history().slice(0, -1), screen: onScreen() },
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
    // An answer with data: the screen gets a timeline / chart / table while the short reply is spoken.
    if (d.view && d.action === 'answer') addAnswer(d.view, lastQuestion.current);

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
        const hi = hiLine(`Okay ${address}, they're in your list.`, `ठीक है${isMD ? ` ${addressHi}` : ''}, वो आपकी लिस्ट में हैं।`);
        if (hi) reply(hi.show, hi.speak, 'hi', false);
        else reply(`Okay ${address}, they're in your list.`, undefined, 'en', false);
        return;
      }
      declineOffer(); // he asked for something else: leave them in the list and carry on
    }

    const pq = questionRef.current;
    if (pq) {
      // His answer to the alert's question. A yes or no in any words is carried out here; a real instruction
      // instead is handled as itself below, and the alert stays on its banner.
      questionRef.current = null;
      const sameItem = !d.target || d.target.id === pq.item.ref?.id || d.target.id === pq.item.requestId;
      const yes = d.action === 'confirm_pending'
        || (pq.q.kind === 'accept' && d.action === 'accept_request' && sameItem)
        || (pq.q.kind === 'done' && d.action === 'complete' && sameItem);
      const no = d.action === 'cancel_pending' || (pq.q.kind === 'accept' && d.action === 'decline_request' && sameItem);
      if (yes || no) {
        expectingRef.current = false;
        answerQuestion(pq.item, pq.q, yes).catch(() => undefined);
        return;
      }
    }

    if (d.action === 'ask') {
      push('jarvis', d.spoken_reply);
      say(speakText, { interrupt: true, lang, fallback: d.spoken_reply }); // "Ek second Sir…" while the search runs
      ask(d.ask_text, d.reply_language);
      return;
    }
    if (d.action === 'end') {
      // "bas / thank you / stop": the conversation is over. JARVIS says its goodbye and steps off the screen.
      setDraft(null);
      previousRef.current = '';
      push('jarvis', d.spoken_reply);
      expectingRef.current = false;
      calledRef.current = false;
      silenceRef.current = 0;
      say(speakText || d.spoken_reply, { interrupt: true, lang, fallback: d.spoken_reply, onDone: () => setTalkOpen(false) });
      return;
    }
    if (d.action === 'dismiss') {
      // "ye hatao / band karo": the banner if there is one, otherwise the card he is looking at.
      if (currentAlert) acknowledge(currentAlert).catch(() => undefined);
      else if (answersRef.current[0]) removeAnswer(answersRef.current[0].id);
      setHeldVisible(false);
      reply(d.spoken_reply || 'Done.', speakText, lang, true);
      return;
    }
    if (d.action === 'screen') {
      // Voice works the screen: download or share the card on it, open a task, go somewhere.
      const top = answersRef.current[0];
      const act = d.screen_action ?? '';
      reply(d.spoken_reply || 'Okay.', speakText, lang, act !== 'go_home' && act !== 'open_settings');
      if (act === 'export_pdf' && top) downloadPdf(top.view, top.text).catch(() => undefined);
      else if (act === 'export_csv' && top) downloadCsv(top.view).catch(() => undefined);
      else if (act === 'share' && top) shareText(top.view, top.text).catch(() => undefined);
      else if (act === 'open_item' && d.target) {
        setTalkOpen(false);
        setItemToOpen(d.target.id);
      } else if (act === 'open_settings') {
        setTalkOpen(false);
        router.push('/settings');
      } else if (act === 'go_home') {
        setTalkOpen(false);
        router.navigate('/');
      }
      return;
    }
    if (d.action === 'briefing') {
      push('jarvis', d.spoken_reply);
      say(speakText, { interrupt: true, lang, fallback: d.spoken_reply });
      briefing();
      return;
    }
    if (d.action === 'confirm_pending') {
      // The AI understood a "yes" in whatever words were used. If JARVIS had offered another time (a clash, a free
      // slot) and he took it, or he named a time himself, the card moves there first.
      const o = d.overrides;
      const cur = draftRef.current;
      if (cur && o && (o.due_date || o.due_time)) {
        editedRef.current = true; // the AI's "done" line named the old time
        if (cur.action === 'change_task') setDraft({ ...cur, changes: { ...cur.changes, ...o } });
        else setDraft({ ...cur, due_date: o.due_date ?? cur.due_date, due_time: o.due_time ?? cur.due_time });
      }
      save();
      return;
    }
    if (d.action === 'set_setting' && !d.needs_clarification && d.setting_patch) {
      // Settings change straight away (no yes needed): the server already validated them.
      api('/me/prefs/', { method: 'PATCH', body: d.setting_patch })
        .then((r) => applyServerPrefs(r.data))
        .catch(() => undefined);
      reply(d.spoken_reply, speakText, lang, true);
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
      const intent = d.action === 'complete' ? 'complete_task'
        : d.action === 'accept_request' || d.action === 'decline_request' || d.action === 'change_task' || d.action === 'delete_task' ? d.action
        : null;
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
      const res = await api<{ text: string; view?: DataView | null }>('/assistant/briefing/?kind=now', { timeoutMs: 45000 });
      setBusy('');
      if (res.data.view) addAnswer(res.data.view, 'Briefing', res.data.text);
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
        spoken = `Saved, ${address}.` + (d.due_date ? ` I'll remind you ${dayLabel(d.due_date).toLowerCase()}${d.due_time ? ` at ${timeLabel(d.due_time)}` : ''}.` : '');
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
        spoken = `Sent to ${md}, ${address}. I'll tell you when ${mdHe(dash)} answers.`;
      } else if (d.action === 'pa_self_task') {
        // Mansi's own task: straight onto her list, nothing is sent to Sir for approval.
        await api('/pa-tasks/self/', { method: 'POST', body: { ...body, description: d.transcript }, idempotencyKey: key });
        spoken = `Saved in your own tasks, ${address}.`
          + (d.due_date ? ` I'll remind you ${dayLabel(d.due_date).toLowerCase()}${d.due_time ? ` at ${timeLabel(d.due_time)}` : ''}.` : '');
      } else if (d.action === 'accept_request' || d.action === 'decline_request') {
        const list = d.targets?.length ? d.targets : d.target ? [d.target] : [];
        if (!list.length) return setBusy('');
        const verb = d.action === 'accept_request' ? 'accept' : 'decline';
        for (const t of list) {
          await api(`/requests/${t.id}/${verb}/`, { method: 'POST', body: {}, idempotencyKey: `${key}-${t.id}` });
          acknowledgeByRef('request', t.id).catch(() => undefined); // the banner about it goes away
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
          ? (sentToSir ? `Sent to ${md} to confirm, ${address}.` : `Marked as done, ${address}.`)
          : `Marked ${list.length} as done, ${address}.` + (sentToSir ? ` ${sentToSir} sent to ${md} to confirm.` : '');
      } else if (d.action === 'remember') {
        await api('/memories/', { method: 'POST', body: { text: d.memory_text }, idempotencyKey: key });
        spoken = `Done, ${address}. I'll remember that.`;
      } else if (d.action === 'change_task' || d.action === 'delete_task') {
        const list = d.targets?.length ? d.targets : d.target ? [d.target] : [];
        if (!list.length) return setBusy('');
        const patch = d.action === 'change_task' ? d.changes : { status: 'CANCELLED' };
        let sentToSir = 0;
        for (const t of list) {
          const path = t.scope === 'private' ? `/private-tasks/${t.id}/` : `/shared-tasks/${t.id}/`;
          const res = await api(path, { method: 'PATCH', body: { expected_version: t.version ?? 1, ...patch } });
          if (res.status === 202) sentToSir += 1;
        }
        const what = d.action === 'change_task' ? 'Changed' : 'Deleted';
        spoken = sentToSir ? `Sent to ${md} to confirm, ${address}.`
          : list.length === 1 ? `${what}, ${address}.${d.action === 'delete_task' ? " It's cancelled, not erased." : ''}`
          : `${what} ${list.length}, ${address}.`;
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
      // A tap while listening means "stop": drop whatever was half-heard and rest until the next tap or "Hey JARVIS".
      expectingRef.current = false;
      silenceRef.current = 0;
      cancelVoice();
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
    const show = `Ji ${address}?`;
    push('jarvis', show);
    expectingRef.current = true;
    calledRef.current = true; // he called: wait for him through the quiet rounds
    silenceRef.current = 0;
    say(isMD ? `जी ${addressHi}?` : 'जी?', { lang: 'hi', interrupt: true, fallback: show, onDone: listen });
  };

  useEffect(() => {
    fns.current = { heard, voiceError, presentOffer, presentAlert, wake };
  });
  // The alert engine hands alerts that carry a question to this conversation (voice and ear live here).
  useEffect(() => {
    setPresenter((item, spoken) => {
      fns.current.presentAlert(item, spoken);
      return true;
    });
    return () => setPresenter(null);
  }, [setPresenter]);
  useEffect(() => {
    answersRef.current = answers;
  }, [answers]);

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
    if (listening || speaking || busy) {
      // The pause expires by itself after a minute (so a closed app can never leave "Hey JARVIS" deaf);
      // keep renewing it for as long as JARVIS really is listening or talking.
      wakePause();
      const renew = setInterval(wakePause, 20_000);
      return () => clearInterval(renew);
    }
    wakeResume();
    return undefined;
  }, [listening, speaking, busy, prefs.wakeWordEnabled]);

  // Alerts that arrive mid-conversation show quietly instead of talking over him.
  useEffect(() => {
    setTalking(listening || speaking || busy !== '');
  }, [listening, speaking, busy, setTalking]);

  // The background service runs whenever someone is signed in: it is what brings reminders and messages to a
  // closed app. After a reboot, a reinstall, or Xiaomi stopping it, the app switches it back on whenever it comes
  // to the front, and matches the microphone part to the "Hey JARVIS" setting.
  useEffect(() => {
    if (!wakeSupported || !user) return;
    let cancelled = false;
    let pushRegistered = false;
    let configured = false;
    let configuredPaName = '';
    const ensure = () => {
      wakeStatus()
        .then(async (st) => {
          if (cancelled || !st.supported) return;
          const wantListening = prefs.wakeWordEnabled && st.micAllowed;
          // Always hand the background part this session's server and login once per app start: after an
          // update or a server switch it may still be holding the previous ones (and would then fetch
          // reminders and JARVIS's voice from the wrong place). The PA's name goes along once it is known.
          if (!configured || !st.running || st.listening !== wantListening || (paName && configuredPaName !== paName)) {
            const tokens = await deviceTokens();
            if (cancelled) return;
            await wakeStart({
              serverUrl: currentServer(),
              accessToken: tokens.access,
              refreshToken: tokens.refresh,
              address: addressOf(user),
              addressHi: addressHiOf(user),
              isMd: user.role === 'MD',
              paName,
              speakWhenLocked: prefs.privateAloudWhenLocked,
              wakeWord: wantListening,
            });
            configured = true;
            configuredPaName = paName;
          }
          // Live push: the server learns this phone's token once per app session (Firebase rotations are
          // re-registered natively).
          if (!pushRegistered) {
            const token = await pushToken();
            if (cancelled || !token) return;
            await api('/me/push-token/', { method: 'POST', body: { token, platform: 'android' } });
            pushRegistered = true;
          }
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
  }, [prefs.wakeWordEnabled, prefs.privateAloudWhenLocked, user, paName]);

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
    messages, draft, draftError, busy, speaking, listening, partial: voice.partial, orb, status, level,
    answers, removeAnswer, clearAnswers, showView, itemToOpen, clearItemToOpen, talkOpen, talkOpenedAt, talkMode, openTalk, closeTalk,
    micPress, send, editDraft, cancelDraft, save, clear, greet,
  };
  return <ConversationContext.Provider value={value}>{children}</ConversationContext.Provider>;
}

export function useConversation() {
  const ctx = useContext(ConversationContext);
  if (!ctx) throw new Error('useConversation must be inside ConversationProvider');
  return ctx;
}
