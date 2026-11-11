import React, { useState } from 'react';
import { Alert, Modal, Pressable, StyleSheet, TextInput, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { ApiError, api } from '@/lib/api';
import { dataChanged } from '@/lib/bus';
import { dayLabel, timeOf } from '@/lib/format';
import { twin } from '@/lib/lang';
import { addressHiOf, addressOf, mdWord, mdWordHi } from '@/lib/names';
import { useSession } from '@/lib/session';
import { say } from '@/lib/speech';
import { colors, kindLabel, radius } from '@/lib/theme';
import type { TaskItem } from '@/lib/types';
import { useDashboard } from '@/lib/useDashboard';

import { Button, Icon, PriorityTag, Tag, Txt } from './ui';

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
  const [busy, setBusy] = useState('');
  const [message, setMessage] = useState('');
  const [holdReason, setHoldReason] = useState('');
  const [askHold, setAskHold] = useState(false);
  if (!item) return null;

  const isMD = user?.role === 'MD';
  const isStaff = item.assignee?.type === 'STAFF';
  const isPaTask = item.assignee?.type === 'PA';
  const ownPaTask = isPaTask && item.origin === 'PA_SELF'; // the PA's own task: the MD can see it but is not told about each step
  const toldSir = ownPaTask ? '' : ` I've told ${md},`;
  const paStatus = (status: string, note = '') =>
    api(`/pa-tasks/${item.id}/status/`, { method: 'POST', body: { status, expected_version: item.version, note } });
  const base = item.scope === 'private' ? `/private-tasks/${item.id}/` : `/shared-tasks/${item.id}/`;
  const address = addressOf(user);
  const addressHi = addressHiOf(user);
  const mdHi = mdWordHi(dash);
  const closed = item.status === 'COMPLETED' || item.status === 'CANCELLED';

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

  // A cancel is undone from the Cancelled list, but it still asks first.
  const confirmCancel = () => {
    Alert.alert(
      isMD ? 'Cancel this task?' : 'Propose cancelling this task?',
      isMD ? 'It goes to the Cancelled list, where you can restore it.' : `${md} will be asked to agree.`,
      [
        { text: 'Keep it', style: 'cancel' },
        {
          text: isMD ? 'Cancel task' : 'Propose',
          style: 'destructive',
          onPress: () => run('cancel', () => api(base, { method: 'PATCH', body: { expected_version: item.version, status: 'CANCELLED' } }),
            `Cancelled, ${address}.`, `कैंसल कर दिया, ${addressHi}।`),
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
        </View>
        <Txt variant="title" style={{ marginTop: 10 }}>{item.title}</Txt>
        <Txt variant="body" style={{ marginTop: 6, color: colors.inkSoft }}>{when}</Txt>
        {rescheduled ? (
          <Txt variant="small" style={{ marginTop: 2 }}>
            Originally due {item.original_due_at ? `${dayLabel(item.original_due_at.slice(0, 10))} ${timeOf(item.original_due_at)}` : dayLabel(item.original_due_date ?? '')}
          </Txt>
        ) : null}
        {item.notes ? <Txt variant="body" style={{ marginTop: 10 }}>{item.notes}</Txt> : null}
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
                Given by {md} to <Txt variant="heading" style={{ fontSize: 13 }}>{item.assignee.staff_name}</Txt> · {item.status_label}
              </Txt>
            )}
            {item.hold_reason ? <Txt variant="small" style={{ marginTop: 2 }}>On hold: {item.hold_reason}</Txt> : null}
          </View>
        ) : item.created_by ? (
          <Txt variant="small" style={{ marginTop: 8 }}>Requested by {item.created_by}</Txt>
        ) : null}
        {message ? <Txt variant="small" style={{ marginTop: 12, color: colors.warn }}>{message}</Txt> : null}

        <View style={s.actions}>
          {closed && (item.scope === 'private' || isMD || ownPaTask) ? (
            <Button
              title={item.status === 'CANCELLED' ? 'Restore' : 'Reopen'}
              icon="refresh-outline"
              loading={busy === 'restore'}
              onPress={() => run('restore',
                () => (ownPaTask && !isMD ? paStatus('OPEN') : api(base, { method: 'PATCH', body: { expected_version: item.version, status: 'OPEN' } })),
                `${item.status === 'CANCELLED' ? 'Restored' : 'Reopened'}, ${address}.`, `${item.status === 'CANCELLED' ? 'वापस ले आया' : 'फिर से खोल दिया'}, ${addressHi}।`)}
            />
          ) : isPaTask && !isMD ? (
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
                title={isMD ? 'Mark done' : 'Propose: mark done'}
                icon="checkmark-done"
                loading={busy === 'done'}
                onPress={() => run('done', () => api(`${base}complete/`, { method: 'POST', body: { expected_version: item.version } }), `Marked done, ${address}.`, `पूरा हुआ मार्क कर दिया, ${addressHi}।`)}
              />
              <View style={{ flexDirection: 'row', marginTop: 8 }}>
                <Button
                  title={isMD ? 'Move to tomorrow' : 'Propose tomorrow'}
                  kind="secondary"
                  compact
                  loading={busy === 'move'}
                  onPress={() => run('move', () => api(base, { method: 'PATCH', body: shiftDay(item, 1) }), `Moved to tomorrow, ${address}.`, `कल पर ले गया, ${addressHi}।`)}
                  style={{ flex: 1, marginRight: 8 }}
                />
                <Button
                  title={isMD ? 'Cancel task' : 'Propose cancel'}
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
