import { Redirect, useLocalSearchParams, useRouter } from 'expo-router';
import React, { useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator,
  Animated,
  AppState,
  Easing,
  KeyboardAvoidingView,
  Linking,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  TextInput,
  View,
} from 'react-native';
import { SafeAreaView, useSafeAreaInsets } from 'react-native-safe-area-context';

import { DraftCard, TASK_DRAFTS, draftHeading } from '@/components/DraftCard';
import { Icon, IconButton, Txt } from '@/components/ui';
import { ApiError, api, newIdempotencyKey } from '@/lib/api';
import { dataChanged } from '@/lib/bus';
import { dayLabel } from '@/lib/format';
import { usePrefs } from '@/lib/prefs';
import { useSession } from '@/lib/session';
import { onSpeakingChange, say, stopSpeaking, type SpeakLang } from '@/lib/speech';
import { colors, fonts, radius } from '@/lib/theme';
import type { AskAnswer, Draft, DraftAction, SessionUser } from '@/lib/types';
import { SILENCE_CODES, useVoiceCapture } from '@/lib/voice';

interface Msg {
  id: string;
  role: 'user' | 'jarvis';
  text: string;
  tone?: 'error' | 'saved';
  sources?: AskAnswer['sources'];
}

const CONFIRMABLE: DraftAction[] = [...TASK_DRAFTS, 'accept_request', 'decline_request', 'complete', 'remember'];
const HHMM = /^([01]?\d|2[0-3]):([0-5]\d)$/;
// Done replies written by the AI are used only where "saved" really means done (a staff task waiting for
// the staff app, or a team split, gets an exact status line instead).
const AI_DONE_OK: DraftAction[] = ['private_task', 'pa_task', 'pa_meeting', 'md_request', 'remember', 'accept_request',
  'decline_request', 'complete'];

/** Short description of the waiting card, so the AI can tell yes / no / a change / something else apart. */
function describe(d: Draft): string {
  const what = d.action === 'remember' ? d.memory_text : d.target ? d.target.title : d.title;
  const when = d.due_date ? `, ${dayLabel(d.due_date)}${d.due_time ? ` ${d.due_time}` : ''}` : '';
  const who = d.action === 'delegation' && d.staff ? `, for ${d.staff.name}` : '';
  return `${draftHeading(d).title}: ${what}${when}${who}`.slice(0, 400);
}

const pick = <T,>(items: T[]) => items[Math.floor(Math.random() * items.length)];

let seq = 0;
const newId = () => `m${Date.now()}-${seq++}`;

function Pulse({ active }: { active: boolean }) {
  const [scale] = useState(() => new Animated.Value(1));
  useEffect(() => {
    if (!active) {
      scale.setValue(1);
      return;
    }
    const loop = Animated.loop(
      Animated.sequence([
        Animated.timing(scale, { toValue: 1.35, duration: 750, easing: Easing.out(Easing.quad), useNativeDriver: true }),
        Animated.timing(scale, { toValue: 1, duration: 750, easing: Easing.in(Easing.quad), useNativeDriver: true }),
      ]),
    );
    loop.start();
    return () => loop.stop();
  }, [active, scale]);
  return <Animated.View pointerEvents="none" style={[s.pulse, { transform: [{ scale }], opacity: active ? 0.22 : 0 }]} />;
}

export default function Talk() {
  const { user } = useSession();
  if (!user) return <Redirect href="/login" />;
  return <TalkScreen user={user} />;
}

function TalkScreen({ user }: { user: SessionUser }) {
  const params = useLocalSearchParams<{ mode?: string; wake?: string }>();
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const { prefs } = usePrefs();
  const isMD = user.role === 'MD';
  const address = isMD ? 'Sir' : user.name;

  const [messages, setMessages] = useState<Msg[]>([]);
  const [draft, setDraftState] = useState<Draft | null>(null);
  const [busy, setBusy] = useState<'' | 'thinking' | 'searching' | 'saving'>('');
  const [speaking, setSpeaking] = useState(false);
  const [typed, setTyped] = useState('');
  const [draftError, setDraftError] = useState('');

  const messagesRef = useRef<Msg[]>([]);
  const draftRef = useRef<Draft | null>(null);
  const editedRef = useRef(false); // the user changed the card by hand, so the AI's "done" line may be stale
  const silenceRef = useRef(0);
  const previousRef = useRef(''); // the sentence a clarification answer belongs to
  const langRef = useRef<SpeakLang>('en');
  const closedRef = useRef(false);
  const idemKey = useRef('');
  const scrollRef = useRef<React.ComponentRef<typeof ScrollView>>(null);
  const engine = useRef<{ heard: (t: string) => void; voiceError: (m: string, c: string) => void; open: () => void }>({
    heard: () => undefined,
    voiceError: () => undefined,
    open: () => undefined,
  });

  const voice = useVoiceCapture(prefs.listenLang, {
    onFinal: (text) => engine.current.heard(text),
    onError: (message, code) => engine.current.voiceError(message, code),
  });

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
    if (closedRef.current) return;
    voice.start();
  };
  /** Show a JARVIS line, speak it, then (optionally) listen again. */
  const reply = (show: string, speakText: string | undefined, lang: SpeakLang, listenAfter: boolean, extra: Partial<Msg> = {}) => {
    push('jarvis', show, extra);
    say(speakText || show, { interrupt: true, lang, onDone: listenAfter ? listen : undefined });
  };
  /** The card waiting for a yes / no / change (a card that is still asking a question is not "waiting"). */
  const waitingCard = () => {
    const d = draftRef.current;
    return d && !d.needs_clarification && d.action !== 'choose_destination' ? d : null;
  };
  const hiLine = (roman: string, devanagari: string) =>
    langRef.current === 'hi' ? { show: roman, speak: devanagari, lang: 'hi' as SpeakLang } : null;
  const close = () => {
    closedRef.current = true;
    voice.cancel();
    stopSpeaking();
    if (router.canGoBack()) router.back();
    else router.replace('/');
  };

  // ---------------------------------------------------------------- conversation
  // Everything the user says goes to the AI with what is on screen: it decides whether it is a yes, a no,
  // a change to the waiting card, an answer to JARVIS's question, or something new. No fixed words.
  const heard = (text: string) => {
    silenceRef.current = 0;
    push('user', text);
    const card = waitingCard();
    if (card) interpret(text, card.transcript, describe(card));
    else interpret(text, previousRef.current, '');
  };

  const voiceError = (message: string, code: string) => {
    if (SILENCE_CODES.has(code)) {
      silence();
      return;
    }
    push('jarvis', message, { tone: 'error' });
  };

  // Nobody spoke: ask once, then step back politely (never just switch off mid-conversation).
  const silence = () => {
    silenceRef.current += 1;
    const sir = isMD ? 'सर, ' : '';
    if (silenceRef.current === 1) {
      const hi = hiLine(pick([`${address}, aap wahan hain?`, `${address}, kya aap sun rahe hain?`]),
        pick([`${sir}आप वहाँ हैं?`, `${sir}क्या आप सुन रहे हैं?`]));
      if (hi) reply(hi.show, hi.speak, 'hi', true);
      else reply(pick([`Are you there, ${address}?`, `${address}, are you still there?`]), undefined, 'en', true);
      return;
    }
    silenceRef.current = 0;
    const hi = hiLine(`Theek hai ${address}, main yahin hoon. Jab zarurat ho, bas mic dabaiye.`,
      `ठीक है${isMD ? ' सर' : ''}, मैं यहीं हूँ। जब ज़रूरत हो, बस माइक दबाइए।`);
    if (hi) reply(hi.show, hi.speak, 'hi', false);
    else reply(`Okay ${address}, I'm right here whenever you need me.`, undefined, 'en', false);
  };

  const interpret = async (text: string, previous: string, awaiting = '') => {
    setBusy('thinking');
    try {
      const res = await api<Draft>('/assistant/interpret/', {
        method: 'POST',
        body: { text, previous, awaiting, history: history().slice(0, -1) },
        timeoutMs: 30000,
      });
      setBusy('');
      if (!closedRef.current) handleDraft(res.data);
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
      push('jarvis', d.spoken_reply);
      say(speakText, { interrupt: true, lang, onDone: close });
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
      reply(d.spoken_reply || `Okay ${address}, cancelled.`, speakText, lang, true);
      return;
    }
    if (d.needs_clarification || d.action === 'choose_destination') {
      previousRef.current = d.transcript;
      if (CONFIRMABLE.includes(d.action) || d.action === 'choose_destination') setDraft(d);
      reply(d.spoken_reply, speakText, lang, true);
      return;
    }
    previousRef.current = '';
    if (CONFIRMABLE.includes(d.action)) {
      idemKey.current = newIdempotencyKey();
      editedRef.current = false;
      setDraftError('');
      setDraft(d); // a new card, or the corrected version of the waiting one
      reply(d.spoken_reply, speakText, lang, true);
      return;
    }
    // Anything else (an answer, small talk): a waiting card simply stays on screen.
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
      if (closedRef.current) return;
      const a = res.data;
      push('jarvis', a.text, { sources: a.sources });
      // Queued after the holding line, then JARVIS listens for the next thing.
      say(a.speak_text || a.text, { lang: a.speak_lang === 'hi' ? 'hi' : 'en', onDone: listen });
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
      if (closedRef.current) return;
      push('jarvis', res.data.text);
      say(res.data.text, { onDone: listen });
    } catch {
      setBusy('');
      push('jarvis', "I couldn't prepare the briefing right now.", { tone: 'error' });
    }
  };

  const save = async () => {
    const d = draftRef.current;
    if (!d) return;
    voice.cancel();
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
      dataChanged.emit();
      // The AI's own "done" line (in the user's language) when it still matches what was saved.
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
    voice.cancel();
    stopSpeaking();
    setDraft(null);
    previousRef.current = '';
    push('jarvis', 'Cancelled. Nothing was saved.');
  };

  const micPress = () => {
    if (voice.state === 'listening') {
      voice.stop();
      return;
    }
    stopSpeaking();
    silenceRef.current = 0;
    listen();
  };

  const send = (text?: string) => {
    const t = (text ?? typed).trim();
    if (!t) return;
    setTyped('');
    voice.cancel();
    stopSpeaking();
    heard(t);
  };

  const open = () => {
    if (params.mode === 'type') return;
    if (params.wake === '1') {
      push('jarvis', isMD ? 'Ji Sir?' : `Ji ${user.name}?`);
      say(isMD ? 'जी सर?' : 'जी?', { lang: 'hi', interrupt: true, onDone: listen });
    } else {
      listen();
    }
  };

  useEffect(() => {
    engine.current = { heard, voiceError, open };
  });

  // Open: listen straight away (or greet when woken). Leave or background: release the mic and stop talking.
  useEffect(() => {
    const kick = setTimeout(() => engine.current.open(), 250);
    const unsubscribe = onSpeakingChange(setSpeaking);
    const sub = AppState.addEventListener('change', (state) => {
      if (state !== 'active') {
        voice.cancel();
        stopSpeaking();
      }
    });
    return () => {
      clearTimeout(kick);
      unsubscribe();
      sub.remove();
      closedRef.current = true;
      voice.cancel();
      stopSpeaking();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---------------------------------------------------------------- view
  const listening = voice.state === 'listening';
  const status = listening ? 'Listening…'
    : busy === 'searching' ? 'Searching the internet…'
    : busy === 'thinking' ? 'Thinking…'
    : busy === 'saving' ? 'Saving…'
    : speaking ? 'Speaking…'
    : 'Tap the mic to talk';
  const suggestions = isMD
    ? ['Aaj mera din kaisa hai?', 'Kal 11 baje Sharma ji ke saath meeting', 'PA ko bolo hotel book kare', 'Aaj ka mausam kaisa hai?']
    : ['Sir ke liye kal 3 baje bank meeting', 'Ramesh ko bolo report bheje', 'Aaj Sir free kab hain?', 'Aaj ka mausam kaisa hai?'];

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }} edges={['top']}>
      <View style={s.top}>
        <IconButton icon="chevron-back" label="Back" onPress={close} />
        <View style={{ flex: 1, alignItems: 'center' }}>
          <Txt style={s.brand}>JARVIS</Txt>
          <Txt variant="caption" style={{ color: listening ? colors.gold : colors.muted }}>{status}</Txt>
        </View>
        <View style={{ width: 40 }} />
      </View>

      <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
        <ScrollView
          ref={scrollRef}
          contentContainerStyle={{ padding: 18, paddingBottom: 24, flexGrow: 1 }}
          keyboardShouldPersistTaps="handled"
          onContentSizeChange={() => scrollRef.current?.scrollToEnd({ animated: true })}
        >
          {!messages.length && !listening ? (
            <View style={s.empty}>
              <Txt style={s.hello}>How can I help, {address}?</Txt>
              <Txt variant="small" style={{ textAlign: 'center', marginTop: 8 }}>
                Speak in English, Hindi or Hinglish. Ask anything, or give me a task.
              </Txt>
              <View style={s.suggestions}>
                {suggestions.map((q) => (
                  <Pressable key={q} style={({ pressed }) => [s.suggestion, pressed && { opacity: 0.7 }]} onPress={() => send(q)}>
                    <Txt variant="small" style={{ color: colors.text }}>{q}</Txt>
                  </Pressable>
                ))}
              </View>
            </View>
          ) : null}

          {messages.map((m) =>
            m.role === 'user' ? (
              <View key={m.id} style={s.userRow}>
                <View style={s.userBubble}>
                  <Txt variant="body">{m.text}</Txt>
                </View>
              </View>
            ) : (
              <View key={m.id} style={s.jarvisRow}>
                <View style={[s.avatar, m.tone === 'error' && { backgroundColor: colors.dangerSoft }, m.tone === 'saved' && { backgroundColor: colors.successSoft }]}>
                  <Icon
                    name={m.tone === 'error' ? 'alert' : m.tone === 'saved' ? 'checkmark' : 'sparkles'}
                    size={13}
                    color={m.tone === 'error' ? colors.danger : m.tone === 'saved' ? colors.success : colors.gold}
                  />
                </View>
                <View style={{ flex: 1 }}>
                  <Txt variant="body" style={[s.jarvisText, m.tone === 'error' && { color: colors.danger }]}>{m.text}</Txt>
                  {m.sources?.length ? (
                    <View style={s.sources}>
                      {m.sources.map((src) => (
                        <Pressable key={src.url} onPress={() => Linking.openURL(src.url)} style={s.source}>
                          <Icon name="link-outline" size={12} color={colors.muted} />
                          <Txt variant="caption" numberOfLines={1} style={{ marginLeft: 4, maxWidth: 200 }}>{src.title}</Txt>
                        </Pressable>
                      ))}
                    </View>
                  ) : null}
                </View>
              </View>
            ),
          )}

          {listening && voice.partial ? (
            <View style={s.userRow}>
              <View style={[s.userBubble, { opacity: 0.55 }]}>
                <Txt variant="body">{voice.partial}</Txt>
              </View>
            </View>
          ) : null}

          {busy === 'thinking' || busy === 'searching' ? (
            <View style={s.jarvisRow}>
              <View style={s.avatar}>
                <ActivityIndicator size="small" color={colors.gold} />
              </View>
              <Txt variant="small" style={{ marginTop: 4 }}>{busy === 'searching' ? 'Searching the internet…' : 'Thinking…'}</Txt>
            </View>
          ) : null}

          {draft ? (
            <DraftCard
              draft={draft}
              saving={busy === 'saving'}
              listening={listening}
              error={draftError}
              onChange={(patch) => {
                editedRef.current = true;
                setDraft({ ...draft, ...patch });
              }}
              onSave={save}
              onCancel={cancelDraft}
            />
          ) : null}
        </ScrollView>

        <View style={[s.composer, { paddingBottom: insets.bottom + 10 }]}>
          <View style={s.inputWrap}>
            <TextInput
              value={typed}
              onChangeText={setTyped}
              onFocus={() => {
                voice.cancel();
                stopSpeaking();
              }}
              placeholder={`Ask or tell JARVIS…`}
              placeholderTextColor={colors.faint}
              style={s.input}
              multiline
              autoFocus={params.mode === 'type'}
            />
            {typed.trim() ? (
              <Pressable onPress={() => send()} style={s.send} accessibilityLabel="Send">
                <Icon name="arrow-up" size={20} color={colors.white} />
              </Pressable>
            ) : null}
          </View>
          <View style={s.micWrap}>
            <Pulse active={listening} />
            <Pressable
              onPress={speaking && !listening ? () => stopSpeaking() : micPress}
              style={({ pressed }) => [s.mic, listening && { backgroundColor: colors.gold }, pressed && { transform: [{ scale: 0.95 }] }]}
              accessibilityLabel={listening ? 'Stop listening' : speaking ? 'Stop speaking' : 'Speak to JARVIS'}
            >
              <Icon name={listening ? 'mic' : speaking ? 'stop' : 'mic-outline'} size={26} color={colors.white} />
            </Pressable>
          </View>
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  top: { flexDirection: 'row', alignItems: 'center', paddingHorizontal: 10, paddingTop: 4, paddingBottom: 6 },
  brand: { fontFamily: fonts.serif, fontSize: 18, lineHeight: 26, letterSpacing: 4, paddingLeft: 4, color: colors.ink },
  empty: { flex: 1, justifyContent: 'center', alignItems: 'center', paddingVertical: 40 },
  hello: { fontFamily: fonts.serif, fontSize: 28, lineHeight: 38, color: colors.ink, textAlign: 'center' },
  suggestions: { marginTop: 26, alignSelf: 'stretch', rowGap: 10 },
  suggestion: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.line,
    borderRadius: radius.lg,
    paddingVertical: 12,
    paddingHorizontal: 16,
  },
  userRow: { flexDirection: 'row', justifyContent: 'flex-end', marginBottom: 14 },
  userBubble: { maxWidth: '82%', backgroundColor: colors.surfaceAlt, borderRadius: 20, borderBottomRightRadius: 6, paddingVertical: 10, paddingHorizontal: 14 },
  jarvisRow: { flexDirection: 'row', alignItems: 'flex-start', marginBottom: 16 },
  avatar: { width: 26, height: 26, borderRadius: 13, backgroundColor: colors.goldSoft, alignItems: 'center', justifyContent: 'center', marginRight: 10, marginTop: 1 },
  jarvisText: { fontSize: 16, lineHeight: 24 },
  sources: { flexDirection: 'row', flexWrap: 'wrap', marginTop: 6, columnGap: 8, rowGap: 6 },
  source: { flexDirection: 'row', alignItems: 'center', backgroundColor: colors.surfaceAlt, borderRadius: 999, paddingHorizontal: 8, paddingVertical: 3 },
  composer: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    paddingHorizontal: 14,
    paddingTop: 10,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: colors.line,
    backgroundColor: colors.bg,
  },
  inputWrap: {
    flex: 1,
    flexDirection: 'row',
    alignItems: 'flex-end',
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.line,
    borderRadius: 24,
    paddingLeft: 16,
    paddingRight: 6,
    minHeight: 48,
    marginRight: 10,
  },
  input: { flex: 1, fontSize: 16, color: colors.text, paddingVertical: 12, maxHeight: 120 },
  send: { width: 36, height: 36, borderRadius: 18, backgroundColor: colors.ink, alignItems: 'center', justifyContent: 'center', marginBottom: 6 },
  micWrap: { width: 56, height: 56, alignItems: 'center', justifyContent: 'center' },
  pulse: { position: 'absolute', width: 56, height: 56, borderRadius: 28, backgroundColor: colors.gold },
  mic: { width: 52, height: 52, borderRadius: 26, backgroundColor: colors.ink, alignItems: 'center', justifyContent: 'center' },
});
