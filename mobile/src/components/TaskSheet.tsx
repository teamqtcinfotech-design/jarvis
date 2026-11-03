import React, { useState } from 'react';
import { Modal, Pressable, StyleSheet, TextInput, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { ApiError, api } from '@/lib/api';
import { dataChanged } from '@/lib/bus';
import { dayLabel, timeOf } from '@/lib/format';
import { useSession } from '@/lib/session';
import { say } from '@/lib/speech';
import { colors, kindLabel, radius } from '@/lib/theme';
import type { TaskItem } from '@/lib/types';

import { Button, PriorityTag, Tag, Txt } from './ui';

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
  const insets = useSafeAreaInsets();
  const [busy, setBusy] = useState('');
  const [message, setMessage] = useState('');
  const [holdReason, setHoldReason] = useState('');
  const [askHold, setAskHold] = useState(false);
  if (!item) return null;

  const isMD = user?.role === 'MD';
  const isStaff = item.assignee?.type === 'STAFF';
  const isPaTask = item.assignee?.type === 'PA';
  const paStatus = (status: string, note = '') =>
    api(`/pa-tasks/${item.id}/status/`, { method: 'POST', body: { status, expected_version: item.version, note } });
  const base = item.scope === 'private' ? `/private-tasks/${item.id}/` : `/shared-tasks/${item.id}/`;
  const address = isMD ? 'Sir' : user?.name ?? '';

  const run = async (label: string, fn: () => Promise<{ status: number }>, spokenOk: string) => {
    setBusy(label);
    setMessage('');
    try {
      const res = await fn();
      const proposed = res.status === 202;
      say(proposed ? `${address}, sent to Sir for approval.` : spokenOk, { interrupt: true });
      dataChanged.emit();
      if (proposed) setMessage('Sent to MD for approval. The task stays as it is until MD accepts.');
      else onClose();
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : 'Could not reach the server.');
      dataChanged.emit();
    } finally {
      setBusy('');
    }
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
        <View style={{ flexDirection: 'row', alignItems: 'center', flexWrap: 'wrap' }}>
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
            <Txt variant="small">
              Given by Sir to <Txt variant="heading" style={{ fontSize: 13 }}>{item.assignee.staff_name}</Txt> · {item.status_label}
            </Txt>
            {item.hold_reason ? <Txt variant="small" style={{ marginTop: 2 }}>On hold: {item.hold_reason}</Txt> : null}
          </View>
        ) : item.created_by ? (
          <Txt variant="small" style={{ marginTop: 8 }}>Requested by {item.created_by}</Txt>
        ) : null}
        {message ? <Txt variant="small" style={{ marginTop: 12, color: colors.warn }}>{message}</Txt> : null}

        <View style={s.actions}>
          {isPaTask && !isMD ? (
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
                    onPress={() => run('hold', () => paStatus('BLOCKED', holdReason.trim()), `On hold. I've told Sir, ${address}.`)}
                    style={{ marginTop: 8 }}
                  />
                </>
              ) : (
                <>
                  <Button
                    title="Done"
                    icon="checkmark-done"
                    loading={busy === 'done'}
                    onPress={() => run('done', () => paStatus('COMPLETED'), `Done. I've told Sir, ${address}.`)}
                  />
                  <View style={{ flexDirection: 'row', marginTop: 8 }}>
                    <Button
                      title={item.status === 'IN_PROGRESS' ? 'Working…' : 'Start'}
                      kind="secondary"
                      compact
                      disabled={item.status === 'IN_PROGRESS'}
                      loading={busy === 'start'}
                      onPress={() => run('start', () => paStatus('IN_PROGRESS'), `Started, ${address}.`)}
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
                onPress={() => run('done', () => api(`${base}complete/`, { method: 'POST', body: { expected_version: item.version } }), `Marked done, ${address}.`)}
              />
              <View style={{ flexDirection: 'row', marginTop: 8 }}>
                <Button
                  title={isMD ? 'Move to tomorrow' : 'Propose tomorrow'}
                  kind="secondary"
                  compact
                  loading={busy === 'move'}
                  onPress={() => run('move', () => api(base, { method: 'PATCH', body: shiftDay(item, 1) }), `Moved to tomorrow, ${address}.`)}
                  style={{ flex: 1, marginRight: 8 }}
                />
                <Button
                  title={isMD ? 'Cancel task' : 'Propose cancel'}
                  kind="danger"
                  compact
                  loading={busy === 'cancel'}
                  onPress={() => run('cancel', () => api(base, { method: 'PATCH', body: { expected_version: item.version, status: 'CANCELLED' } }), `Cancelled, ${address}.`)}
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
