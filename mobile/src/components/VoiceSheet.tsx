import { useRouter } from 'expo-router';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator,
  Animated,
  Easing,
  KeyboardAvoidingView,
  Modal,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  TextInput,
  View,
} from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { ApiError, api, newIdempotencyKey } from '@/lib/api';
import { dataChanged } from '@/lib/bus';
import { addDays, dayLabel, isoDate } from '@/lib/format';
import { usePrefs } from '@/lib/prefs';
import { useSession } from '@/lib/session';
import { say, stopSpeaking } from '@/lib/speech';
import { colors, kindLabel, priorityColor, priorityLabel, radius } from '@/lib/theme';
import type { CategoryOption, Draft, DraftAction, Kind, Priority, StaffOption } from '@/lib/types';
import { useVoiceCapture } from '@/lib/voice';

import { Button, Chip, Icon, Tag, Txt } from './ui';

type Phase = 'capture' | 'typing' | 'thinking' | 'draft' | 'saving' | 'saved' | 'answer' | 'error';

const PRIORITIES: Priority[] = ['URGENT', 'HIGH', 'NORMAL', 'LOW'];
const KINDS: Kind[] = ['TASK', 'MEETING', 'CALL', 'PAYMENT', 'FOLLOW_UP'];
const HHMM = /^([01]?\d|2[0-3]):([0-5]\d)$/;

const ACTION_TITLE: Record<DraftAction, string> = {
  private_task: 'Personal task',
  md_request: 'Request for MD',
  delegation: 'Assign to staff',
  pa_task: 'Task for your PA',
  pa_meeting: 'Meeting with your PA',
  team_delegation: 'Assign to team',
  choose_destination: 'Where should this go?',
  accept_request: 'Accept request',
  decline_request: 'Decline request',
  complete: 'Mark as done',
  briefing: 'Briefing',
  answer: 'JARVIS',
};

function Pulse({ active }: { active: boolean }) {
  const [scale] = useState(() => new Animated.Value(1));
  useEffect(() => {
    if (!active) {
      scale.setValue(1);
      return;
    }
    const loop = Animated.loop(
      Animated.sequence([
        Animated.timing(scale, { toValue: 1.25, duration: 700, easing: Easing.out(Easing.quad), useNativeDriver: true }),
        Animated.timing(scale, { toValue: 1, duration: 700, easing: Easing.in(Easing.quad), useNativeDriver: true }),
      ]),
    );
    loop.start();
    return () => loop.stop();
  }, [active, scale]);
  return <Animated.View style={[s.pulse, { transform: [{ scale }], opacity: active ? 0.35 : 0 }]} />;
}

// Hands-free confirmation: after JARVIS asks "Shall I save it?", the reply is understood as yes / no / a correction.
const YES_WORDS = new Set(['haan', 'han', 'ha', 'haanji', 'ji', 'yes', 'yeah', 'yep', 'yup', 'ok', 'okay', 'theek', 'thik',
  'hai', 'sahi', 'save', 'karo', 'kar', 'do', 'assign', 'confirm', 'bilkul', 'done', 'go', 'ahead', 'chalo', 'send',
  'bhej', 'please', 'sir', 'hmm', 'sure', 'right']);
const NO_WORDS = new Set(['nahi', 'nahin', 'nai', 'na', 'no', 'cancel', 'mat', 'karo', 'rehne', 'do', 'ruko', 'stop',
  'chhodo', 'chodo', 'nope', 'ji', 'please', 'kar']);

function classifyReply(text: string): 'yes' | 'no' | 'correction' {
  const words = text.toLowerCase().replace(/[.,!?।]/g, ' ').split(/\s+/).filter(Boolean);
  if (!words.length || words.length > 5) return 'correction';
  if (words.every((w) => YES_WORDS.has(w))) return 'yes';
  if (words.every((w) => NO_WORDS.has(w))) return 'no';
  return 'correction';
}

const CONFIRMABLE: DraftAction[] = ['private_task', 'md_request', 'delegation', 'team_delegation', 'pa_task',
  'pa_meeting', 'accept_request', 'decline_request', 'complete'];
const MAX_AUTO_LISTENS = 4;

/** Mounted only while open (the parent renders it conditionally), so every open starts fresh. */
export function VoiceSheet({ startMode, onClose }: { startMode: 'voice' | 'type'; onClose: () => void }) {
  const { user } = useSession();
  const { prefs } = usePrefs();
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const isMD = user?.role === 'MD';
  const address = isMD ? 'Sir' : user?.name ?? '';

  const [phase, setPhase] = useState<Phase>(startMode === 'voice' ? 'capture' : 'typing');
  const [typed, setTyped] = useState('');
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState('');
  const [allStaff, setAllStaff] = useState<StaffOption[] | null>(null);
  const [showAllStaff, setShowAllStaff] = useState(false);
  const [previousText, setPreviousText] = useState('');
  const [listenFor, setListenFor] = useState<'command' | 'confirm' | 'answer'>('command');
  const [voiceHint, setVoiceHint] = useState('');
  const idemKey = useRef('');
  const modeRef = useRef<'command' | 'confirm' | 'answer'>('command');
  const autoListens = useRef(0);
  const autoListenRef = useRef<(mode: 'confirm' | 'answer') => void>(() => undefined);
  const replyRef = useRef<(text: string) => void>(() => undefined);

  // Editable fields
  const [title, setTitle] = useState('');
  const [priority, setPriority] = useState<Priority>('NORMAL');
  const [kind, setKind] = useState<Kind>('TASK');
  const [dueDate, setDueDate] = useState('');
  const [dueTime, setDueTime] = useState('');
  const [action, setAction] = useState<DraftAction>('private_task');
  const [staffId, setStaffId] = useState<number | null>(null);
  const [categoryKey, setCategoryKey] = useState('');
  const [categories, setCategories] = useState<CategoryOption[]>([]);

  const interpret = useCallback(
    async (text: string, previous?: string) => {
      if (!text.trim()) return;
      setPhase('thinking');
      setError('');
      setVoiceHint('');
      try {
        const res = await api<Draft>('/assistant/interpret/', {
          method: 'POST',
          body: { text, previous: previous ?? previousText },
          timeoutMs: 30000,
        });
        const d = res.data;
        setDraft(d);
        setTitle(d.title);
        setPriority(d.priority);
        setKind(d.kind);
        setDueDate(d.due_date);
        setDueTime(d.due_time);
        setAction(d.action);
        setStaffId(d.staff?.id ?? null);
        setCategoryKey(d.category_key ?? '');
        if (d.categories?.length) setCategories(d.categories);
        idemKey.current = newIdempotencyKey();
        setPreviousText(d.needs_clarification ? d.transcript : '');
        setPhase(d.action === 'answer' || d.action === 'briefing' ? 'answer' : 'draft');
        // Fully voice-driven: after JARVIS speaks, it listens for the answer or for "haan / nahi".
        if (d.needs_clarification) {
          say(d.spoken_reply, { interrupt: true, onDone: () => autoListenRef.current('answer') });
        } else if (CONFIRMABLE.includes(d.action)) {
          say(d.spoken_reply, { interrupt: true, onDone: () => autoListenRef.current('confirm') });
        } else {
          say(d.spoken_reply, { interrupt: true });
        }
      } catch (e) {
        setError(e instanceof ApiError ? e.message : 'Something went wrong.');
        setPhase('error');
      }
    },
    [previousText],
  );

  // Speech ends -> straight to the brain (event-driven, no effect polling).
  const voice = useVoiceCapture(prefs.voiceLang, {
    onFinal: (text) => {
      const mode = modeRef.current;
      modeRef.current = 'command';
      setListenFor('command');
      if (mode === 'confirm') replyRef.current(text);
      else interpret(text); // a new command, or the answer to JARVIS's question (uses the earlier sentence)
    },
    onError: (message) => {
      const mode = modeRef.current;
      modeRef.current = 'command';
      setListenFor('command');
      if (mode === 'command') {
        setError(message);
        setPhase('error');
      } else {
        setVoiceHint('Tap the mic to answer, or use the buttons.'); // keep the card; no harm done
      }
    },
  });
  const { start: startVoice, cancel: cancelVoice } = voice;

  const stopAutoListen = useCallback(() => {
    if (modeRef.current !== 'command') cancelVoice();
    modeRef.current = 'command';
    setListenFor('command');
  }, [cancelVoice]);

  useEffect(() => {
    autoListenRef.current = (mode) => {
      if (autoListens.current >= MAX_AUTO_LISTENS) return;
      autoListens.current += 1;
      modeRef.current = mode;
      setListenFor(mode);
      setVoiceHint('');
      startVoice();
    };
  }, [startVoice]);

  const begin = useCallback(
    (mode: 'voice' | 'type') => {
      setError('');
      if (mode === 'voice') {
        setPhase('capture');
        startVoice();
      } else {
        stopSpeaking();
        setPhase('typing');
      }
    },
    [startVoice],
  );

  // Open: start listening immediately in voice mode. Close: release the mic and stop talking.
  useEffect(() => {
    const kick = setTimeout(() => {
      if (startMode === 'voice') startVoice();
    }, 0);
    return () => {
      clearTimeout(kick);
      cancelVoice();
      stopSpeaking();
    };
  }, [startMode, startVoice, cancelVoice]);

  const loadStaff = useCallback(async () => {
    setShowAllStaff(true);
    if (allStaff) return;
    try {
      const res = await api<{ results: StaffOption[]; categories: CategoryOption[] }>('/staff/');
      setAllStaff(res.data.results);
      if (res.data.categories?.length) setCategories(res.data.categories);
    } catch {
      setAllStaff([]);
    }
  }, [allStaff]);

  const confirm = useCallback(async () => {
    if (!draft) return;
    if (action === 'choose_destination') return; // JARVIS is still waiting to hear where this goes
    if (['private_task', 'md_request', 'delegation', 'team_delegation', 'pa_task', 'pa_meeting'].includes(action)) {
      if (!title.trim()) return setError('Please add a title.');
      if (dueTime && !HHMM.test(dueTime)) return setError('Time must look like 14:30.');
      if (dueTime && !dueDate) return setError('Pick a date for this time.');
      if (action === 'delegation' && !staffId) return setError('Choose who should do this.');
      if (action === 'team_delegation' && !draft.team?.members.length) return setError('No one in that team can take this.');
    }
    setPhase('saving');
    setError('');
    const body = {
      title: title.trim(),
      notes: draft.notes,
      kind,
      priority,
      due_date: dueDate || null,
      due_time: dueTime ? dueTime.padStart(5, '0') : null,
      duration_minutes: draft.duration_minutes,
      remind_before_minutes: draft.remind_before_minutes,
      announcement: draft.announcement,
      source: 'VOICE',
    };
    try {
      let spoken = '';
      if (action === 'private_task') {
        await api('/private-tasks/', { method: 'POST', body, idempotencyKey: idemKey.current });
        spoken = `Saved, ${address}.` + (dueDate ? ` I'll remind you ${dayLabel(dueDate).toLowerCase()}${dueTime ? ` at ${dueTime}` : ''}.` : '');
      } else if (action === 'delegation') {
        const res = await api<any>('/delegations/', {
          method: 'POST',
          // description = the full spoken instruction; TaskPanel keeps it on the task (guide §3).
          body: { ...body, staff_id: staffId, category_key: categoryKey, description: draft.transcript },
          idempotencyKey: idemKey.current,
          timeoutMs: 30000,
        });
        const who = res.data?.assignee?.staff_name ?? 'staff';
        spoken = res.status === 202
          ? `${address}, sent to TaskPanel for ${who}. I'm waiting for TaskPanel to confirm and will tell you.`
          : `Assigned to ${who} in TaskPanel. I'll track it, ${address}.`;
      } else if (action === 'team_delegation') {
        const res = await api<{ assigned: string[]; pending: string[]; not_assigned: { name: string; reason: string }[] }>(
          '/delegations/team/',
          {
            method: 'POST',
            body: { ...body, staff_ids: draft.team?.members.map((m) => m.id) ?? [], category_key: categoryKey, description: draft.transcript },
            idempotencyKey: idemKey.current,
            timeoutMs: 120000,
          },
        );
        const r = res.data;
        spoken = `${address}, assigned to ${r.assigned.length} ${r.assigned.length === 1 ? 'person' : 'people'} in TaskPanel.`;
        if (r.pending.length) spoken += ` ${r.pending.length} waiting for TaskPanel to confirm.`;
        if (r.not_assigned.length) spoken += ` Not assigned: ${r.not_assigned.map((n) => n.name).join(', ')}.`;
      } else if (action === 'pa_task' || action === 'pa_meeting') {
        const meeting = action === 'pa_meeting';
        const res = await api<any>('/pa-tasks/', {
          method: 'POST',
          body: { ...body, description: draft.transcript, meeting_with_pa: meeting },
          idempotencyKey: idemKey.current,
        });
        const who = res.data?.assignee?.staff_name ?? draft.pa_name ?? 'your PA';
        spoken = meeting
          ? `Saved, ${address}. I've informed ${who}, and I'll remind you both.`
          : `Given to ${who}, ${address}. I'll remind you both and tell you when it's done.`;
      } else if (action === 'md_request') {
        await api('/requests/', { method: 'POST', body, idempotencyKey: idemKey.current });
        spoken = `Sent to Sir for approval, ${address}.`;
      } else if (action === 'accept_request' || action === 'decline_request') {
        if (!draft.target) return setPhase('draft');
        const verb = action === 'accept_request' ? 'accept' : 'decline';
        await api(`/requests/${draft.target.id}/${verb}/`, { method: 'POST', body: {}, idempotencyKey: idemKey.current });
        spoken = verb === 'accept' ? `Accepted, ${address}. It's on your list.` : `Declined, ${address}.`;
      } else if (action === 'complete') {
        const t = draft.target;
        if (!t) return setPhase('draft');
        const path = t.scope === 'private' ? `/private-tasks/${t.id}/complete/` : `/shared-tasks/${t.id}/complete/`;
        const res = await api(path, { method: 'POST', body: { expected_version: t.version ?? 1 } });
        spoken = res.status === 202 ? `Sent to Sir for approval, ${address}.` : `Marked done, ${address}.`;
      }
      say(spoken, { interrupt: true });
      setPhase('saved');
      dataChanged.emit();
      setTimeout(onClose, 1100);
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : 'Could not save.';
      setError(`${msg} Nothing was saved${e instanceof ApiError && e.retryable ? ' — tap Retry' : ''}.`);
      setPhase('draft');
    }
  }, [draft, action, title, kind, priority, dueDate, dueTime, staffId, categoryKey, address, onClose]);

  // What the user said after "Shall I save it?": yes -> save, no -> cancel, anything else -> a correction.
  useEffect(() => {
    replyRef.current = (text: string) => {
      const reply = classifyReply(text);
      if (reply === 'yes') confirm();
      else if (reply === 'no') {
        say(`Okay ${address}, cancelled. Nothing was saved.`, { interrupt: true });
        setTimeout(onClose, 900);
      } else if (draft) {
        interpret(text, draft.transcript); // e.g. "nahi, personal mein daalo" / "time 5 baje kar do"
      }
    };
  }, [confirm, interpret, draft, address, onClose]);

  const answerClarification = () => {
    autoListens.current = 0;
    autoListenRef.current('answer');
  };

  const today = isoDate(new Date());
  const dateChips = [
    { label: 'Today', value: today },
    { label: 'Tomorrow', value: isoDate(addDays(new Date(), 1)) },
    { label: 'Day after', value: isoDate(addDays(new Date(), 2)) },
    { label: 'No date', value: '' },
  ];
  const staffOptions: StaffOption[] = showAllStaff && allStaff ? allStaff : draft?.staff_candidates ?? [];
  const editable = draft && ['private_task', 'md_request', 'delegation', 'team_delegation', 'pa_task', 'pa_meeting'].includes(action);
  const paName = draft?.pa_name || 'your PA';
  const destination =
    action === 'private_task' ? 'Your personal tasks · private, only you'
      : action === 'pa_task' ? `Your PA · ${paName} does it · you both get reminders`
      : action === 'pa_meeting' ? `Your meeting with ${paName} · ${paName} is informed · you both get reminders`
      : action === 'delegation' ? (draft?.staff ? `${draft.staff.name} · ${draft.staff.department} · via TaskPanel` : 'A staff member · via TaskPanel')
      : action === 'team_delegation' ? `${draft?.team?.label ?? 'Team'} · ${draft?.team?.members.length ?? 0} people · one TaskPanel task each`
      : action === 'md_request' ? 'Sir · needs his approval'
      : action === 'choose_destination' ? 'Not decided yet — tell JARVIS: personal, your PA, or a staff name'
      : '';
  const listening = voice.state === 'listening';

  return (
    <Modal visible transparent animationType="slide" onRequestClose={onClose}>
      <Pressable style={s.backdrop} onPress={onClose} />
      <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
        <View style={[s.sheet, { paddingBottom: insets.bottom + 16 }]}>
          <View style={s.grabber} />

          {(phase === 'capture' || phase === 'thinking') && (
            <View style={s.center}>
              <Txt variant="label">{phase === 'thinking' ? 'Thinking' : listening ? 'Listening' : 'Starting mic'}</Txt>
              <View style={s.micWrap}>
                <Pulse active={listening} />
                <Pressable onPress={listening ? voice.stop : () => begin('voice')} style={s.mic}>
                  {phase === 'thinking' ? <ActivityIndicator color={colors.white} /> : <Icon name="mic" size={34} color={colors.white} />}
                </Pressable>
              </View>
              <Txt variant="title" style={s.transcript}>
                {voice.partial || (phase === 'thinking' ? '' : `Speak naturally, ${address}…`)}
              </Txt>
              {previousText ? <Txt variant="small">Answering: {draft?.question}</Txt> : null}
              <Txt variant="caption" style={{ marginTop: 12, textAlign: 'center' }}>
                English, Hindi or Hinglish · e.g. “kal subah 11 baje Sharma ke saath meeting, urgent”
              </Txt>
              <Button title="Type instead" kind="ghost" icon="create-outline" onPress={() => { cancelVoice(); begin('type'); }} style={{ marginTop: 8 }} />
            </View>
          )}

          {phase === 'typing' && (
            <View>
              <Txt variant="label">Tell JARVIS</Txt>
              <TextInput
                value={typed}
                onChangeText={setTyped}
                placeholder={isMD ? 'e.g. Ramesh ko bolo parso tak GST file bheje' : 'e.g. Sir ke liye kal 3 baje bank meeting'}
                placeholderTextColor={colors.faint}
                style={[s.input, { minHeight: 90, textAlignVertical: 'top' }]}
                multiline
                autoFocus
              />
              <View style={{ flexDirection: 'row', marginTop: 12 }}>
                <Button title="Speak" kind="secondary" icon="mic-outline" onPress={() => begin('voice')} style={{ flex: 1, marginRight: 8 }} />
                <Button title="Continue" icon="arrow-forward" onPress={() => interpret(typed)} disabled={!typed.trim()} style={{ flex: 1.4 }} />
              </View>
            </View>
          )}

          {phase === 'error' && (
            <View style={s.center}>
              <Icon name="alert-circle-outline" size={36} color={colors.danger} />
              <Txt variant="heading" style={{ marginTop: 10, textAlign: 'center' }}>{error}</Txt>
              <View style={{ flexDirection: 'row', marginTop: 18 }}>
                <Button title="Type" kind="secondary" onPress={() => begin('type')} style={{ marginRight: 8 }} />
                <Button title="Try again" icon="mic" onPress={() => begin('voice')} />
              </View>
            </View>
          )}

          {phase === 'answer' && draft && (
            <View>
              <Txt variant="label">{ACTION_TITLE[draft.action]}</Txt>
              <Txt variant="title" style={{ marginTop: 8, lineHeight: 30 }}>{draft.spoken_reply}</Txt>
              <View style={{ flexDirection: 'row', marginTop: 18 }}>
                {draft.action === 'briefing' ? (
                  <Button title="Full briefing" kind="secondary" icon="sunny-outline" onPress={() => { onClose(); router.push('/briefing'); }} style={{ flex: 1, marginRight: 8 }} />
                ) : (
                  <Button title="Ask again" kind="secondary" icon="mic-outline" onPress={() => begin('voice')} style={{ flex: 1, marginRight: 8 }} />
                )}
                <Button title="Done" onPress={onClose} style={{ flex: 1 }} />
              </View>
            </View>
          )}

          {(phase === 'draft' || phase === 'saving' || phase === 'saved') && draft && (
            <ScrollView keyboardShouldPersistTaps="handled" style={{ maxHeight: 560 }}>
              <View style={s.draftHead}>
                <Txt variant="label">{ACTION_TITLE[action]}</Txt>
                <Tag label={draft.engine === 'ai' ? 'AI' : 'Offline parser'} color={colors.inkSoft} />
              </View>
              <Txt variant="small" style={{ marginTop: 4, fontStyle: 'italic' }}>“{draft.transcript}”</Txt>

              {destination ? (
                <View style={s.destination}>
                  <Icon name="navigate-outline" size={16} color={colors.ink} />
                  <Txt variant="small" style={{ marginLeft: 8, flex: 1, color: colors.ink }}>
                    <Txt variant="small" style={{ fontWeight: '700', color: colors.ink }}>Goes to: </Txt>
                    {destination}
                  </Txt>
                </View>
              ) : null}

              {draft.needs_clarification ? (
                <View style={s.question}>
                  <Icon name="help-circle-outline" size={20} color={colors.gold} />
                  <Txt variant="heading" style={{ flex: 1, marginLeft: 8 }}>{draft.question}</Txt>
                  {listening && listenFor === 'answer' ? null : (
                    <Button title="Answer" icon="mic" compact kind="gold" onPress={answerClarification} />
                  )}
                </View>
              ) : null}

              {listening && listenFor !== 'command' ? (
                <View style={s.listening}>
                  <Icon name="mic" size={16} color={colors.white} />
                  <Txt variant="small" style={{ color: colors.white, marginLeft: 8, flex: 1 }}>
                    {listenFor === 'confirm'
                      ? 'Listening… say “haan” to save, “nahi” to cancel, or tell me what to change.'
                      : 'Listening for your answer…'}
                    {voice.partial ? `  “${voice.partial}”` : ''}
                  </Txt>
                </View>
              ) : voiceHint ? (
                <Txt variant="caption" style={{ marginTop: 8 }}>{voiceHint}</Txt>
              ) : null}

              {editable ? (
                <>
                  {action === 'team_delegation' && draft.team ? (
                    <View style={s.teamBox}>
                      <Txt variant="small" style={{ color: colors.ink }}>
                        {draft.team.members.map((m) => m.name).join(', ')}
                      </Txt>
                      {draft.team.skipped.length ? (
                        <Txt variant="caption" style={{ marginTop: 4 }}>
                          On leave, skipped: {draft.team.skipped.map((m) => m.name).join(', ')}
                        </Txt>
                      ) : null}
                    </View>
                  ) : null}

                  <Txt variant="label" style={s.fieldLabel}>Title</Txt>
                  <TextInput value={title} onChangeText={setTitle} style={s.input} placeholder="What needs doing" placeholderTextColor={colors.faint} />

                  {action === 'delegation' ? (
                    <>
                      <Txt variant="label" style={s.fieldLabel}>Assign to (TaskPanel staff)</Txt>
                      <View style={s.rowWrap}>
                        {staffOptions.map((st) => (
                          <Chip
                            key={st.id}
                            label={`${st.name} · ${st.department}${st.on_leave ? ' · on leave' : ''}`}
                            color={st.on_leave ? colors.danger : colors.ink}
                            selected={staffId === st.id}
                            onPress={() => setStaffId(st.id)}
                          />
                        ))}
                        {!showAllStaff ? <Chip label="Other staff…" icon="search-outline" onPress={loadStaff} /> : null}
                      </View>
                      <Txt variant="label" style={s.fieldLabel}>Category</Txt>
                      <View style={s.rowWrap}>
                        {categories.map((c) => (
                          <Chip key={c.key} label={c.department ? `${c.name} · ${c.department}` : c.name} selected={categoryKey === c.key} onPress={() => setCategoryKey(c.key)} />
                        ))}
                        {!categories.length ? <Chip label="Load categories" icon="list-outline" onPress={loadStaff} /> : null}
                      </View>
                      {draft.warnings?.length ? (
                        <View style={s.warn}>
                          {draft.warnings.map((w) => (
                            <Txt key={w} variant="small" style={{ color: colors.warn }}>• {w}</Txt>
                          ))}
                        </View>
                      ) : null}
                    </>
                  ) : null}

                  <Txt variant="label" style={s.fieldLabel}>When</Txt>
                  <View style={s.rowWrap}>
                    {dateChips.map((c) => (
                      <Chip key={c.label} label={c.label} selected={dueDate === c.value} onPress={() => setDueDate(c.value)} />
                    ))}
                    {dueDate && !dateChips.some((c) => c.value === dueDate) ? <Chip label={dayLabel(dueDate)} selected /> : null}
                  </View>
                  <View style={[s.rowWrap, { alignItems: 'center' }]}>
                    <TextInput
                      value={dueTime}
                      onChangeText={(v) => setDueTime(v.replace(/[^0-9:]/g, '').slice(0, 5))}
                      placeholder="HH:MM"
                      placeholderTextColor={colors.faint}
                      keyboardType="numbers-and-punctuation"
                      style={[s.input, { width: 96, marginTop: 0, marginRight: 8 }]}
                    />
                    {['10:00', '12:00', '15:00', '18:00'].map((t) => (
                      <Chip key={t} label={t} selected={dueTime === t} onPress={() => setDueTime(t)} />
                    ))}
                    {dueTime ? <Chip label="No time" onPress={() => setDueTime('')} /> : null}
                  </View>

                  <Txt variant="label" style={s.fieldLabel}>Priority</Txt>
                  <View style={s.rowWrap}>
                    {PRIORITIES.filter((p) => action !== 'delegation' || p !== 'LOW').map((p) => (
                      <Chip key={p} label={priorityLabel[p]} color={priorityColor[p]} selected={priority === p} onPress={() => setPriority(p)} />
                    ))}
                  </View>

                  <Txt variant="label" style={s.fieldLabel}>Type</Txt>
                  <View style={s.rowWrap}>
                    {KINDS.map((k) => (
                      <Chip key={k} label={kindLabel[k]} selected={kind === k} onPress={() => setKind(k)} />
                    ))}
                  </View>
                  {kind === 'PAYMENT' ? (
                    <Txt variant="caption" style={{ marginTop: 2 }}>Reminder only — JARVIS never makes payments.</Txt>
                  ) : null}
                </>
              ) : (
                <View style={s.targetBox}>
                  {draft.target ? (
                    <Txt variant="title" style={{ fontSize: 19 }}>{draft.target.title}</Txt>
                  ) : (
                    draft.target_candidates.map((c) => (
                      <Chip key={c.id} label={c.title} onPress={() => setDraft({ ...draft, target: c, needs_clarification: false })} />
                    ))
                  )}
                </View>
              )}

              {error ? <Txt variant="small" style={{ color: colors.danger, marginTop: 12 }}>{error}</Txt> : null}

              <View style={{ flexDirection: 'row', marginTop: 18 }}>
                <Button title="Cancel" kind="secondary" onPress={() => { stopAutoListen(); onClose(); }} style={{ flex: 1, marginRight: 8 }} />
                <Button
                  title={
                    phase === 'saved'
                      ? 'Saved'
                      : error
                        ? 'Retry'
                        : ({
                            private_task: 'Save',
                            delegation: 'Assign',
                            team_delegation: `Assign to ${draft.team?.members.length ?? 0}`,
                            pa_task: 'Give to PA',
                            pa_meeting: 'Save meeting',
                            md_request: 'Send to MD',
                            accept_request: 'Accept',
                            decline_request: 'Decline',
                          } as Partial<Record<DraftAction, string>>)[action] ?? 'Confirm'
                  }
                  icon={phase === 'saved' ? 'checkmark-circle' : 'checkmark'}
                  loading={phase === 'saving'}
                  disabled={phase === 'saved' || action === 'choose_destination' || (!editable && !draft.target)}
                  onPress={() => { stopAutoListen(); confirm(); }}
                  style={{ flex: 1.6 }}
                />
              </View>
              <Txt variant="caption" style={{ marginTop: 10, textAlign: 'center' }}>
                Nothing is saved until you confirm.
              </Txt>
            </ScrollView>
          )}
        </View>
      </KeyboardAvoidingView>
    </Modal>
  );
}

const s = StyleSheet.create({
  backdrop: { flex: 1, backgroundColor: colors.overlay },
  sheet: {
    backgroundColor: colors.bg,
    borderTopLeftRadius: radius.xl,
    borderTopRightRadius: radius.xl,
    paddingHorizontal: 20,
    paddingTop: 10,
  },
  grabber: { alignSelf: 'center', width: 44, height: 5, borderRadius: 3, backgroundColor: colors.line, marginBottom: 16 },
  center: { alignItems: 'center', paddingVertical: 8 },
  micWrap: { width: 120, height: 120, alignItems: 'center', justifyContent: 'center', marginVertical: 18 },
  pulse: { position: 'absolute', width: 120, height: 120, borderRadius: 60, backgroundColor: colors.gold },
  mic: { width: 84, height: 84, borderRadius: 42, backgroundColor: colors.ink, alignItems: 'center', justifyContent: 'center' },
  transcript: { textAlign: 'center', minHeight: 60, paddingHorizontal: 8 },
  input: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.line,
    borderRadius: radius.md,
    paddingHorizontal: 14,
    paddingVertical: 11,
    fontSize: 16,
    color: colors.text,
    marginTop: 8,
  },
  draftHead: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' },
  question: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: colors.goldSoft,
    borderRadius: radius.md,
    padding: 12,
    marginTop: 14,
  },
  fieldLabel: { marginTop: 16, marginBottom: 2, color: colors.inkSoft },
  destination: {
    flexDirection: 'row',
    alignItems: 'center',
    marginTop: 12,
    padding: 10,
    borderRadius: radius.md,
    backgroundColor: colors.goldSoft,
  },
  listening: {
    flexDirection: 'row',
    alignItems: 'center',
    marginTop: 12,
    padding: 10,
    borderRadius: radius.md,
    backgroundColor: colors.ink,
  },
  teamBox: { marginTop: 12, padding: 10, borderRadius: radius.md, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line },
  warn: { marginTop: 10, padding: 10, borderRadius: radius.md, backgroundColor: colors.warnSoft, rowGap: 2 },
  rowWrap: { flexDirection: 'row', flexWrap: 'wrap', marginTop: 6 },
  targetBox: { marginTop: 14, padding: 14, backgroundColor: colors.surface, borderRadius: radius.md, borderWidth: 1, borderColor: colors.line, flexDirection: 'row', flexWrap: 'wrap' },
});
