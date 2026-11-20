import React, { useEffect, useState } from 'react';
import { Alert, Modal, Pressable, ScrollView, StyleSheet, TextInput, View, useWindowDimensions } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { ApiError, api, newIdempotencyKey } from '@/lib/api';
import { dataChanged } from '@/lib/bus';
import { dayLabel, isoDate, timeOf } from '@/lib/format';
import { twin } from '@/lib/lang';
import { addressHiOf, addressOf, mdWord, mdWordHi } from '@/lib/names';
import { useSession } from '@/lib/session';
import { say } from '@/lib/speech';
import { colors, kindLabel, radius } from '@/lib/theme';
import type { HistoryStep, TaskItem } from '@/lib/types';
import { useDashboard } from '@/lib/useDashboard';

import { TaskFiles } from './TaskFiles';
import { Button, Chip, Icon, PriorityTag, Tag, Txt } from './ui';

/** "Today 10:42 AM" for a moment the server sent. */
const momentLabel = (iso: string) => `${dayLabel(isoDate(new Date(iso)))} ${timeOf(iso)}`;

/**
 * A work item's history, as the server recorded it: who gave it, when it was seen, the time promised, moves, who
 * else got it, done. The last few steps show; the rest are one tap away.
 */
function History({ taskId, version }: { taskId: string; version: number }) {
  const [steps, setSteps] = useState<HistoryStep[] | null>(null);
  const [all, setAll] = useState(false);
  useEffect(() => {
    let alive = true;
    api<{ results: HistoryStep[] }>(`/shared-tasks/${taskId}/history/`)
      .then((r) => alive && setSteps(r.data.results))
      .catch(() => alive && setSteps([]));
    return () => {
      alive = false;
    };
  }, [taskId, version]);
  if (!steps || steps.length === 0) return null;
  const shown = all ? steps : steps.slice(-3);
  return (
    <View style={s.history}>
      <Pressable onPress={() => setAll((v) => !v)} disabled={steps.length <= 3} style={s.historyHead} hitSlop={8}>
        <Txt variant="label">History</Txt>
        {steps.length > 3 ? <Txt variant="caption">{all ? 'Show less' : `All ${steps.length} steps`}</Txt> : null}
      </Pressable>
      <ScrollView style={{ maxHeight: all ? 220 : undefined }} nestedScrollEnabled>
        {shown.map((st, i) => (
          <View key={`${st.at}-${i}`} style={s.step}>
            <View style={[s.dot, i === shown.length - 1 && { backgroundColor: colors.gold }]} />
            <View style={{ flex: 1 }}>
              <Txt variant="small">{st.text}</Txt>
              <Txt variant="caption">{st.at_label}</Txt>
            </View>
          </View>
        ))}
      </ScrollView>
    </View>
  );
}

function shiftDay(item: TaskItem, days: number) {
  const base = item.due_date ? new Date(`${item.due_date}T00:00:00`) : new Date();
  base.setDate(base.getDate() + days);
  const due_date = `${base.getFullYear()}-${String(base.getMonth() + 1).padStart(2, '0')}-${String(base.getDate()).padStart(2, '0')}`;
  const body: Record<string, unknown> = { expected_version: item.version, due_date };
  const start = item.scheduled_start ?? item.due_at;
  if (start) {
    const d = new Date(start);
    body.due_time = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  }
  return body;
}

export function TaskSheet({ item, onClose }: { item: TaskItem | null; onClose: () => void }) {
  const { user } = useSession();
  const { data: dash } = useDashboard();
  const md = mdWord(dash);
  const insets = useSafeAreaInsets();
  const { height: screenHeight } = useWindowDimensions();
  const [busy, setBusy] = useState('');
  const [message, setMessage] = useState('');
  const [holdReason, setHoldReason] = useState('');
  const [askHold, setAskHold] = useState(false);
  // Nov 2026: an update to send, a step to add, and a counter that refreshes the history after either.
  const [noteText, setNoteText] = useState('');
  const [newStep, setNewStep] = useState('');
  const [addingStep, setAddingStep] = useState(false);
  const [refreshed, setRefreshed] = useState(0);
  // The section picker: the work sections, loaded when it opens; a new one is made by typing its name.
  const [pickSection, setPickSection] = useState(false);
  const [sectionsList, setSectionsList] = useState<{ id: number; name: string; kind: string; colour: string }[] | null>(null);
  const [newSection, setNewSection] = useState('');
  if (!item) return null;

  const isMD = user?.role === 'MD';
  const isStaff = item.assignee?.type === 'STAFF';
  // A task on a team member's list. The one whose list it is moves it (status); the MD, or whoever gave it, can
  // change or cancel it. Their own personal items are theirs to change as they like.
  const myId = dash?.team?.find((t) => t.you)?.id;
  const isPaTask = item.assignee?.type === 'USER' || item.assignee?.type === 'PA' || item.assignee?.type === 'MG';
  const isMine = !isMD && isPaTask && myId != null && item.assignee?.user_id === myId;
  const ownPaTask = isPaTask && ['SELF', 'PA_SELF', 'MG_SELF'].includes(item.origin ?? ''); // their own: no one is told each step
  const gaveIt = myId != null && item.created_by_id === myId;
  const direct = isMD || gaveIt || item.scope === 'private'; // changes it straight away (anyone else proposes to the MD)
  const giver = item.created_by || md;
  // Work on the MD's own list that someone on the team gave him: it rings until he sees it, and he may decline it
  // (the giver is told) instead of doing it.
  const givenToMD = item.assignee?.type === 'MD' && (item.origin === 'GIVEN' || item.origin === 'MEET') && item.created_by_id != null && !gaveIt;
  const toldSir = ownPaTask ? '' : ` I've told ${giver},`;
  const paStatus = (status: string, note = '') =>
    api(`/pa-tasks/${item.id}/status/`, { method: 'POST', body: { status, expected_version: item.version, note } });
  const base = item.scope === 'private' ? `/private-tasks/${item.id}/` : `/shared-tasks/${item.id}/`;
  const address = addressOf(user);
  const addressHi = addressHiOf(user);
  const mdHi = mdWordHi(dash);
  const closed = item.status === 'COMPLETED' || item.status === 'CANCELLED';
  // The task's people: whoever has it, whoever gave it, the MD; a personal item is its owner's alone.
  const ofTask = item.scope === 'private' || isMD || isMine || gaveIt;
  const canTick = !closed && ofTask && !isStaff;
  const canNote = item.scope === 'shared' && !isStaff && (isMD || isMine || gaveIt);
  const isApproval = item.kind === 'APPROVAL';
  const canDecide = isApproval && !closed && (isMD || (myId != null && item.assignee?.user_id === myId));
  const steps = item.steps ?? [];

  const run = async (label: string, fn: () => Promise<{ status: number }>, spokenOk: string, spokenOkHi = '') => {
    setBusy(label);
    setMessage('');
    try {
      const res = await fn();
      const proposed = res.status === 202;
      const t = proposed ? twin(`${address}, sent to ${md} for approval.`, `${addressHi}, ${mdHi} को मंज़ूरी के लिए भेज दिया।`) : twin(spokenOk, spokenOkHi);
      say(t.text, { interrupt: true, lang: t.lang, fallback: t.fallback });
      dataChanged.emit();
      if (proposed) setMessage(`Sent to ${md} for approval. The task stays as it is until ${md} accepts.`);
      else onClose();
    } catch (e) {
      // Changed on the other phone meanwhile (409): the lists refresh; the buttons work again with the new version.
      if (e instanceof ApiError && e.status === 409) setMessage('This task changed a moment ago. Refreshed, please try again.');
      else setMessage(e instanceof ApiError ? e.message : 'Could not reach the server.');
      dataChanged.emit();
    } finally {
      setBusy('');
    }
  };

  // Sections: whoever gave it, whoever does it, or the MD may move it ("put this in Partners" by voice does the same).
  const canMove = item.scope === 'shared' && !closed && !isStaff && (isMD || gaveIt || isMine);
  const openPicker = async () => {
    setPickSection((v) => !v);
    if (sectionsList) return;
    try {
      const r = await api<{ results: { id: number; name: string; kind: string; colour: string }[] }>('/sections/');
      setSectionsList(r.data.results.filter((sct) => sct.kind === 'WORK'));
    } catch {
      setSectionsList([]);
    }
  };
  const moveTo = (name: string) =>
    run('section', () => api(`/shared-tasks/${item.id}/section/`, { method: 'POST', body: { section: name } }),
      `Moved to ${name}, ${address}.`, `${name} में रख दिया, ${addressHi}।`);

  // ---- Nov 2026: checklist, updates, approvals, repeats (each stays on the sheet; the lists refresh behind it)
  const quiet = async (label: string, fn: () => Promise<unknown>, okMessage = '') => {
    setBusy(label);
    setMessage('');
    try {
      await fn();
      dataChanged.emit();
      setRefreshed((n) => n + 1);
      if (okMessage) setMessage(okMessage);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setMessage('This task changed a moment ago. Refreshed, please try again.');
      else setMessage(e instanceof ApiError ? e.message : 'Could not reach the server.');
      dataChanged.emit();
    } finally {
      setBusy('');
    }
  };
  const tick = (index: number, done: boolean) =>
    quiet(`step:${index}`, async () => {
      const r = await api<{ all_done: boolean }>(`${base}steps/`, { method: 'POST', body: { index, done } });
      if (r.data.all_done && !closed && (isMine || item.scope === 'private' || isMD || gaveIt)) {
        Alert.alert('Every step is done', 'Mark the task done too?', [
          { text: 'Not yet', style: 'cancel' },
          { text: 'Mark done', onPress: () => run('done', () => (isMine && !isMD ? paStatus('COMPLETED') : api(`${base}complete/`, { method: 'POST', body: { expected_version: item.version + 1 } })),
            `Done, ${address}.`, `हो गया, ${addressHi}।`) },
        ]);
      }
    });
  const addStep = () => {
    const text = newStep.trim();
    if (!text) return;
    quiet('step:add', () => api(`${base}steps/`, { method: 'POST', body: { steps: [...steps, { text, done: false }] } }));
    setNewStep('');
    setAddingStep(false);
  };
  const sendNote = () => {
    const text = noteText.trim();
    if (!text) return;
    quiet('note', async () => {
      const r = await api<{ told: string[] }>(`${base}notes/`, { method: 'POST', body: { text }, idempotencyKey: newIdempotencyKey() });
      setNoteText('');
      const told = r.data.told?.length ? r.data.told.join(', ') : '';
      const t = twin(told ? `Noted, ${address}. I've told ${told}.` : `Noted, ${address}.`, told ? `नोट कर लिया, ${addressHi}। ${told} को बता दिया।` : `नोट कर लिया, ${addressHi}।`);
      say(t.text, { interrupt: true, lang: t.lang, fallback: t.fallback });
    });
  };
  const decide = (approve: boolean) =>
    run('approve', () => api(`${base}approve/`, { method: 'POST', body: { approve }, idempotencyKey: newIdempotencyKey() }),
      approve ? `Approved, ${address}. I've told ${giver}.` : `Declined, ${address}. I've told ${giver}.`,
      approve ? `मंज़ूर कर दिया, ${addressHi}। ${giver} को बता दिया।` : `मंज़ूर नहीं किया, ${addressHi}। ${giver} को बता दिया।`);
  const stopRepeat = () =>
    Alert.alert('Stop repeating?', 'This one stays; nothing more is made from it.', [
      { text: 'Keep repeating', style: 'cancel' },
      { text: 'Stop', style: 'destructive', onPress: () => run('repeat', () => api(base, { method: 'PATCH', body: { expected_version: item.version, repeat: '' } }),
        `Okay ${address}, it won't repeat any more.`, `ठीक है ${addressHi}, अब ये दोहराया नहीं जाएगा।`) },
    ]);

  // A cancel is undone from the Cancelled list, but it still asks first. For the MD, work someone gave him is
  // "declined": the same step, and the giver hears it in those words.
  const declining = isMD && givenToMD;
  const confirmCancel = () => {
    Alert.alert(
      declining ? 'Decline this task?' : direct ? 'Cancel this task?' : 'Propose cancelling this task?',
      declining ? `${giver} will be told you declined it. It goes to the Cancelled list, where you can restore it.`
        : direct ? 'It goes to the Cancelled list, where you can restore it.' : `${md} will be asked to agree.`,
      [
        { text: 'Keep it', style: 'cancel' },
        {
          text: declining ? 'Decline' : direct ? 'Cancel task' : 'Propose',
          style: 'destructive',
          onPress: () => run('cancel', () => api(base, { method: 'PATCH', body: { expected_version: item.version, status: 'CANCELLED' } }),
            declining ? `Declined, ${address}. I've told ${giver}.` : `Cancelled, ${address}.`,
            declining ? `मना कर दिया, ${addressHi}। ${giver} को बता दिया।` : `कैंसल कर दिया, ${addressHi}।`),
        },
      ],
    );
  };

  const when = item.scheduled_start
    ? `${dayLabel(item.scheduled_start.slice(0, 10))} · ${timeOf(item.scheduled_start)} – ${timeOf(item.scheduled_end)}`
    : item.due_label || 'No deadline';
  const rescheduled =
    (item.original_due_at && item.due_at && item.original_due_at !== item.due_at) ||
    (item.original_due_date && item.due_date && item.original_due_date !== item.due_date);

  return (
    <Modal visible transparent animationType="slide" onRequestClose={onClose}>
      <Pressable style={s.backdrop} onPress={onClose} />
      <View style={[s.sheet, { paddingBottom: insets.bottom + 16 }]}>
        <View style={s.grabber} />
        {/* always one obvious way out, within thumb reach */}
        <Pressable onPress={onClose} accessibilityLabel="Close" hitSlop={10} style={({ pressed }) => [s.closeBtn, pressed && { opacity: 0.6 }]}>
          <Icon name="close" size={20} color={colors.inkSoft} />
        </Pressable>
        <View style={{ flexDirection: 'row', alignItems: 'center', flexWrap: 'wrap', paddingRight: 48 }}>
          <PriorityTag priority={item.priority} />
          <Tag label={kindLabel[item.kind]} />
          {item.scope === 'private' ? <Tag label="Private · only you" icon="lock-closed" color={colors.inkSoft} /> : null}
          {item.overdue ? <Tag label="Overdue" color={colors.danger} bg={colors.dangerSoft} /> : null}
          {item.repeat_label ? <Tag label={item.repeat_label} icon="repeat-outline" color={colors.info} bg={colors.infoSoft} /> : null}
        </View>
        <ScrollView style={{ maxHeight: screenHeight * 0.58 }} keyboardShouldPersistTaps="handled" nestedScrollEnabled>
        <Txt variant="title" style={{ marginTop: 10 }}>{item.title}</Txt>
        {isApproval ? (
          <Txt variant="title" style={{ marginTop: 4, color: colors.gold }}>{item.amount_label || 'Approval'}</Txt>
        ) : null}
        <Txt variant="body" style={{ marginTop: 6, color: colors.inkSoft }}>{when}</Txt>
        {rescheduled ? (
          <Txt variant="small" style={{ marginTop: 2 }}>
            Originally due {item.original_due_at ? `${dayLabel(item.original_due_at.slice(0, 10))} ${timeOf(item.original_due_at)}` : dayLabel(item.original_due_date ?? '')}
          </Txt>
        ) : null}
        {item.notes ? <Txt variant="body" style={{ marginTop: 10 }}>{item.notes}</Txt> : null}
        {item.chain?.length ? (
          // Whose hands it passed through, once it was handed on: everyone sees where it came from.
          <View style={s.chainRow}>
            <Icon name="git-branch-outline" size={14} color={colors.inkSoft} />
            <Txt variant="small" style={{ marginLeft: 6, color: colors.inkSoft }}>{item.chain.join('  →  ')}</Txt>
          </View>
        ) : null}
        {item.repeat_label && !closed ? (
          <View style={s.chainRow}>
            <Icon name="repeat-outline" size={14} color={colors.info} />
            <Txt variant="small" style={{ marginLeft: 6, flex: 1 }}>
              Repeats {item.repeat_label}{item.repeat_until ? ` until ${dayLabel(item.repeat_until)}` : ''}. The next one is made when this is done.
            </Txt>
            {direct ? <Pressable onPress={stopRepeat} hitSlop={8}><Txt variant="caption" style={{ color: colors.danger }}>Stop</Txt></Pressable> : null}
          </View>
        ) : null}
        {isApproval && canDecide ? (
          <View style={[s.box, { borderColor: colors.gold }]}>
            <Txt variant="small">
              <Txt variant="heading" style={{ fontSize: 13 }}>{giver}</Txt> asks your approval{item.amount_label ? ` for ${item.amount_label}` : ''}: {item.title}.
            </Txt>
            <View style={{ flexDirection: 'row', marginTop: 10 }}>
              <Button title="Decline" kind="danger" compact loading={busy === 'approve'} onPress={() => decide(false)} style={{ flex: 1, marginRight: 8 }} />
              <Button title="Approve" icon="checkmark" compact loading={busy === 'approve'} onPress={() => decide(true)} style={{ flex: 1 }} />
            </View>
            <Txt variant="caption" style={{ marginTop: 6 }}>{giver} hears your decision at once.</Txt>
          </View>
        ) : isApproval ? (
          <Txt variant="small" style={{ marginTop: 8, color: colors.inkSoft }}>
            {closed ? (item.status === 'COMPLETED' ? 'Approved' : 'Declined') : `Waiting for ${isMine ? 'you' : item.assignee?.staff_name || md}`}
            {gaveIt ? ' · asked by you' : item.created_by ? ` · asked by ${item.created_by}` : ''}
          </Txt>
        ) : null}
        {steps.length || canTick ? (
          <View style={s.checklist}>
            <View style={s.historyHead}>
              <Txt variant="label">Checklist{steps.length ? ` · ${steps.filter((st) => st.done).length} of ${steps.length}` : ''}</Txt>
              {canTick ? (
                <Pressable onPress={() => setAddingStep((v) => !v)} hitSlop={8}>
                  <Txt variant="caption" style={{ color: colors.accent }}>{addingStep ? 'Cancel' : '+ Add step'}</Txt>
                </Pressable>
              ) : null}
            </View>
            {steps.map((st, i) => (
              <Pressable key={`${st.text}-${i}`} onPress={canTick ? () => tick(i, !st.done) : undefined} disabled={!canTick || busy === `step:${i}`} style={s.stepRow}>
                <Icon name={st.done ? 'checkbox' : 'square-outline'} size={20} color={st.done ? colors.success : colors.inkSoft} />
                <Txt variant="body" style={[{ marginLeft: 10, flex: 1 }, st.done && s.done]}>{st.text}</Txt>
              </Pressable>
            ))}
            {addingStep ? (
              <View style={{ flexDirection: 'row', alignItems: 'center', marginTop: 6 }}>
                <TextInput value={newStep} onChangeText={setNewStep} placeholder="A step, e.g. Book the hotel" placeholderTextColor={colors.faint}
                  style={[s.input, { flex: 1 }]} autoFocus maxLength={200} onSubmitEditing={addStep} returnKeyType="done" />
                <Button title="Add" compact disabled={!newStep.trim() || busy === 'step:add'} loading={busy === 'step:add'} onPress={addStep} style={{ marginLeft: 8 }} />
              </View>
            ) : null}
            {!steps.length && canTick && !addingStep ? <Txt variant="caption">Break it into steps and tick them off; the other side sees the progress.</Txt> : null}
          </View>
        ) : null}
        {isStaff ? (
          <View style={s.box}>
            <Txt variant="small">Assigned to <Txt variant="heading" style={{ fontSize: 13 }}>{item.assignee.staff_name}</Txt> ({item.assignee.department}) by {item.created_by}</Txt>
            <Txt variant="small" style={{ marginTop: 4 }}>
              TaskPanel: {item.remote_task_id ? `${item.remote_task_id} · ` : ''}{item.status_label || 'Assigned'}
            </Txt>
            {item.hold_reason ? <Txt variant="small" style={{ marginTop: 2 }}>On hold: {item.hold_reason}</Txt> : null}
            {item.sync_error ? <Txt variant="small" style={{ marginTop: 2, color: colors.danger }}>{item.sync_error}</Txt> : null}
            {item.description && item.description !== item.title ? (
              <Txt variant="small" style={{ marginTop: 6, fontStyle: 'italic' }}>“{item.description}”</Txt>
            ) : null}
            <Txt variant="caption" style={{ marginTop: 6 }}>Only {item.assignee.staff_name} can start, hold or complete this task in TaskPanel.</Txt>
          </View>
        ) : isPaTask ? (
          <View style={s.box}>
            {ownPaTask ? (
              <Txt variant="small">
                {isMD ? `${item.assignee.staff_name}'s own task` : 'Your own task'} · {item.status_label}
              </Txt>
            ) : (
              <Txt variant="small">
                {item.origin === 'MEET' || item.origin === 'MD_MEET_PA' ? 'Meeting set by ' : 'Given by '}{gaveIt ? 'you' : giver} to{' '}
                <Txt variant="heading" style={{ fontSize: 13 }}>{isMine ? 'you' : item.assignee.staff_name}</Txt> · {item.status_label}
              </Txt>
            )}
            {!ownPaTask && !closed ? (
              // Ring until seen: has the person seen it, and by when did they say they would do it.
              <Txt variant="small" style={{ marginTop: 2, color: item.seen_at ? colors.inkSoft : colors.warn }}>
                {item.seen_at ? `Seen ${momentLabel(item.seen_at)}` : 'Not seen yet'}
                {item.promise_label ? ` · will do it by ${item.promise_label}` : ''}
              </Txt>
            ) : null}
            {item.hold_reason ? <Txt variant="small" style={{ marginTop: 2 }}>On hold: {item.hold_reason}</Txt> : null}
          </View>
        ) : givenToMD ? (
          <View style={s.box}>
            <Txt variant="small">
              {item.origin === 'MEET' ? 'Meeting set by ' : 'Given to '}{isMD ? (item.origin === 'MEET' ? giver : 'you') : md}{item.origin === 'MEET' ? ' with ' : ' by '}
              <Txt variant="heading" style={{ fontSize: 13 }}>{item.origin === 'MEET' ? (isMD ? 'you' : md) : giver}</Txt>
              {item.status_label ? ` · ${item.status_label}` : ''}
            </Txt>
            {!closed ? (
              <Txt variant="small" style={{ marginTop: 2, color: item.seen_at ? colors.inkSoft : colors.warn }}>
                {item.seen_at ? `Seen ${momentLabel(item.seen_at)}` : isMD ? 'Not seen yet · ringing until you tap Seen' : `${md} has not seen it yet`}
                {item.promise_label ? ` · will do it by ${item.promise_label}` : ''}
              </Txt>
            ) : null}
            {isMD && !closed ? <Txt variant="caption" style={{ marginTop: 4 }}>Not for you? Decline below and {giver} is told.</Txt> : null}
          </View>
        ) : item.created_by ? (
          <Txt variant="small" style={{ marginTop: 8 }}>Requested by {item.created_by}</Txt>
        ) : null}
        {item.scope === 'shared' && !isStaff ? (
          <View style={s.sectionRow}>
            <Pressable onPress={canMove ? openPicker : undefined} style={[s.sectionChip, { borderColor: item.section_colour || colors.line }]}>
              <Icon name="folder-outline" size={14} color={item.section_colour || colors.inkSoft} />
              <Txt variant="small" style={{ marginLeft: 6, color: item.section ? colors.text : colors.inkSoft }}>{item.section || 'No section'}</Txt>
              {canMove ? <Icon name={pickSection ? 'chevron-up' : 'chevron-down'} size={14} color={colors.faint} /> : null}
            </Pressable>
          </View>
        ) : null}
        {pickSection ? (
          <View style={s.picker}>
            <Txt variant="label">Move to</Txt>
            <View style={s.chips}>
              {(sectionsList ?? []).filter((sct) => sct.name !== item.section).map((sct) => (
                <Chip key={sct.id} label={sct.name} icon="folder-outline" onPress={() => moveTo(sct.name)} />
              ))}
              {sectionsList === null ? <Txt variant="caption">Loading…</Txt> : null}
              {sectionsList && !sectionsList.filter((sct) => sct.name !== item.section).length ? <Txt variant="caption">No other section yet. Type one below.</Txt> : null}
            </View>
            <View style={{ flexDirection: 'row', alignItems: 'center', marginTop: 8 }}>
              <TextInput value={newSection} onChangeText={setNewSection} placeholder="New section, e.g. Franchise" placeholderTextColor={colors.faint} style={[s.input, { flex: 1 }]} autoCapitalize="words" maxLength={60} />
              <Button title="Move" compact disabled={!newSection.trim() || busy === 'section'} loading={busy === 'section'} onPress={() => moveTo(newSection.trim())} style={{ marginLeft: 8 }} />
            </View>
          </View>
        ) : null}
        {canNote && !closed ? (
          // A short update for the other side ("bank band tha, kal subah karunga"): into the history, and they hear it.
          <View style={{ flexDirection: 'row', alignItems: 'center', marginTop: 12 }}>
            <TextInput value={noteText} onChangeText={setNoteText} placeholder={gaveIt || isMD ? `An update for ${isMine ? giver : item.assignee?.staff_name || 'them'}…` : `An update for ${giver}…`}
              placeholderTextColor={colors.faint} style={[s.input, { flex: 1 }]} maxLength={1000} onSubmitEditing={sendNote} returnKeyType="send" />
            <Button title="Send" compact icon="send-outline" disabled={!noteText.trim() || busy === 'note'} loading={busy === 'note'} onPress={sendNote} style={{ marginLeft: 8 }} />
          </View>
        ) : null}
        {!isStaff ? <TaskFiles scope={item.scope} taskId={item.id} canAdd={ofTask && !closed} initialCount={item.files_count} /> : null}
        {item.scope === 'shared' ? <History taskId={item.id} version={item.version + refreshed} /> : null}
        {message ? <Txt variant="small" style={{ marginTop: 12, color: colors.warn }}>{message}</Txt> : null}
        </ScrollView>

        <View style={s.actions}>
          {closed && (item.scope === 'private' || direct || ownPaTask) ? (
            <Button
              title={item.status === 'CANCELLED' ? 'Restore' : 'Reopen'}
              icon="refresh-outline"
              loading={busy === 'restore'}
              onPress={() => run('restore',
                () => (ownPaTask && !isMD ? paStatus('OPEN') : api(base, { method: 'PATCH', body: { expected_version: item.version, status: 'OPEN' } })),
                `${item.status === 'CANCELLED' ? 'Restored' : 'Reopened'}, ${address}.`, `${item.status === 'CANCELLED' ? 'वापस ले आया' : 'फिर से खोल दिया'}, ${addressHi}।`)}
            />
          ) : isApproval && !closed ? (
            // An approval is decided above (Approve / Decline), or waited for: no Done / Move here.
            canDecide ? null : (
              <Button title={declining ? 'Withdraw' : direct ? 'Cancel request' : 'Close'} kind={direct ? 'danger' : 'ghost'} compact
                onPress={direct ? confirmCancel : onClose} />
            )
          ) : isMine ? (
            <>
              {askHold ? (
                <>
                  <TextInput
                    value={holdReason}
                    onChangeText={setHoldReason}
                    placeholder="Why is it on hold?"
                    placeholderTextColor={colors.faint}
                    style={s.input}
                    autoFocus
                  />
                  <Button
                    title="Put on hold"
                    kind="danger"
                    loading={busy === 'hold'}
                    disabled={!holdReason.trim()}
                    onPress={() => run('hold', () => paStatus('BLOCKED', holdReason.trim()), `On hold.${toldSir} ${address}.`, `होल्ड पर रख दिया${ownPaTask ? '' : `, ${mdHi} को बता दिया`}, ${addressHi}।`)}
                    style={{ marginTop: 8 }}
                  />
                </>
              ) : (
                <>
                  <Button
                    title="Done"
                    icon="checkmark-done"
                    loading={busy === 'done'}
                    onPress={() => run('done', () => paStatus('COMPLETED'), `Done.${toldSir} ${address}.`, `हो गया${ownPaTask ? '' : `, ${mdHi} को बता दिया`}, ${addressHi}।`)}
                  />
                  <View style={{ flexDirection: 'row', marginTop: 8 }}>
                    <Button
                      title={item.status === 'IN_PROGRESS' ? 'Working…' : 'Start'}
                      kind="secondary"
                      compact
                      disabled={item.status === 'IN_PROGRESS'}
                      loading={busy === 'start'}
                      onPress={() => run('start', () => paStatus('IN_PROGRESS'), `Started, ${address}.`, `शुरू कर दिया, ${addressHi}।`)}
                      style={{ flex: 1, marginRight: 8 }}
                    />
                    <Button title="Hold" kind="danger" compact onPress={() => setAskHold(true)} style={{ flex: 1 }} />
                  </View>
                </>
              )}
            </>
          ) : isStaff ? (
            <Button
              title={item.sync_state === 'PENDING' ? 'Check again with TaskPanel' : 'Refresh status'}
              kind="secondary"
              icon="refresh"
              loading={busy === 'refresh'}
              onPress={() => run('refresh', () => api(`/delegations/${item.id}/check/`, { method: 'POST' }), 'Status refreshed.')}
            />
          ) : (
            <>
              <Button
                title={direct ? 'Mark done' : 'Propose: mark done'}
                icon="checkmark-done"
                loading={busy === 'done'}
                onPress={() => run('done', () => api(`${base}complete/`, { method: 'POST', body: { expected_version: item.version } }), `Marked done, ${address}.`, `पूरा हुआ मार्क कर दिया, ${addressHi}।`)}
              />
              <View style={{ flexDirection: 'row', marginTop: 8 }}>
                <Button
                  title={direct ? 'Move to tomorrow' : 'Propose tomorrow'}
                  kind="secondary"
                  compact
                  loading={busy === 'move'}
                  onPress={() => run('move', () => api(base, { method: 'PATCH', body: shiftDay(item, 1) }), `Moved to tomorrow, ${address}.`, `कल पर ले गया, ${addressHi}।`)}
                  style={{ flex: 1, marginRight: 8 }}
                />
                <Button
                  title={declining ? 'Decline' : direct ? 'Cancel task' : 'Propose cancel'}
                  kind="danger"
                  compact
                  loading={busy === 'cancel'}
                  onPress={confirmCancel}
                  style={{ flex: 1 }}
                />
              </View>
            </>
          )}
          <Button title="Close" kind="ghost" onPress={onClose} style={{ marginTop: 6 }} />
        </View>
      </View>
    </Modal>
  );
}

const s = StyleSheet.create({
  backdrop: { flex: 1, backgroundColor: colors.overlay },
  sheet: { backgroundColor: colors.bg, borderTopLeftRadius: radius.xl, borderTopRightRadius: radius.xl, paddingHorizontal: 20, paddingTop: 10 },
  grabber: { alignSelf: 'center', width: 44, height: 5, borderRadius: 3, backgroundColor: colors.line, marginBottom: 14 },
  closeBtn: { position: 'absolute', top: 14, right: 14, zIndex: 2, width: 40, height: 40, borderRadius: 20, backgroundColor: colors.surfaceAlt, alignItems: 'center', justifyContent: 'center' },
  box: { marginTop: 12, padding: 12, borderRadius: radius.md, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line },
  sectionRow: { flexDirection: 'row', marginTop: 10 },
  sectionChip: { flexDirection: 'row', alignItems: 'center', borderWidth: 1, borderRadius: 999, paddingVertical: 5, paddingHorizontal: 10, backgroundColor: colors.surface },
  picker: { marginTop: 10, padding: 12, borderRadius: radius.md, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line },
  chips: { flexDirection: 'row', flexWrap: 'wrap', marginTop: 6, gap: 6 },
  history: { marginTop: 12 },
  historyHead: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 },
  step: { flexDirection: 'row', alignItems: 'flex-start', paddingVertical: 4 },
  dot: { width: 8, height: 8, borderRadius: 4, backgroundColor: colors.line, marginTop: 6, marginRight: 10 },
  chainRow: { flexDirection: 'row', alignItems: 'center', marginTop: 8 },
  checklist: { marginTop: 12, padding: 12, borderRadius: radius.md, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line },
  stepRow: { flexDirection: 'row', alignItems: 'center', paddingVertical: 6 },
  done: { textDecorationLine: 'line-through', color: colors.muted },
  actions: { marginTop: 20 },
  input: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.line,
    borderRadius: radius.md,
    paddingHorizontal: 14,
    paddingVertical: 11,
    fontSize: 16,
    color: colors.text,
  },
});
