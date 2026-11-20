/**
 * "Share to JARVIS" (Nov 2026): a photo or file sent from another app lands here. The person picks the open task it
 * belongs to, or names a new task for it, and the file is uploaded to that task. Nothing is sent anywhere until they
 * choose.
 */
import React, { useEffect, useMemo, useState } from 'react';
import { AppState, Modal, Pressable, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { ApiError, api, apiUpload, newIdempotencyKey } from '@/lib/api';
import { dataChanged } from '@/lib/bus';
import { useSession } from '@/lib/session';
import { say } from '@/lib/speech';
import { colors, radius } from '@/lib/theme';
import type { TaskItem } from '@/lib/types';
import { useDashboard } from '@/lib/useDashboard';
import { type SharedIn, wakeConsumeShare, wakeSupported } from '@/lib/wake';

import { TaskRow } from './TaskRow';
import { Button, Icon, Txt } from './ui';

export function ShareIntake() {
  const { user } = useSession();
  const { data: dash } = useDashboard();
  const insets = useSafeAreaInsets();
  const [share, setShare] = useState<SharedIn | null>(null);
  const [title, setTitle] = useState('');
  const [busy, setBusy] = useState('');
  const [message, setMessage] = useState('');

  useEffect(() => {
    if (!wakeSupported) return;
    const check = () => {
      const s = wakeConsumeShare();
      if (s) {
        setShare(s);
        setTitle(s.text.slice(0, 120));
        setMessage('');
      }
    };
    const kick = setTimeout(check, 500);
    const sub = AppState.addEventListener('change', (st) => {
      if (st === 'active') setTimeout(check, 300);
    });
    return () => {
      clearTimeout(kick);
      sub.remove();
    };
  }, []);

  const candidates = useMemo(() => {
    if (!dash) return [] as TaskItem[];
    const seen = new Set<string>();
    const out: TaskItem[] = [];
    for (const t of [...dash.overdue, ...dash.today, ...dash.upcoming, ...dash.anytime, ...(dash.given_tasks ?? [])]) {
      if (t.status === 'COMPLETED' || t.status === 'CANCELLED' || seen.has(t.id)) continue;
      seen.add(t.id);
      out.push(t);
    }
    return out.slice(0, 12);
  }, [dash]);

  if (!share || !user) return null;
  const isPhoto = share.mime.startsWith('image/');
  const file = { uri: share.uri, name: share.name || (isPhoto ? 'photo.jpg' : 'file'), type: share.mime || 'application/octet-stream' };

  const attachTo = async (task: TaskItem) => {
    setBusy(task.id);
    setMessage('');
    try {
      await apiUpload(`/${task.scope === 'private' ? 'private-tasks' : 'shared-tasks'}/${task.id}/files/`, file);
      dataChanged.emit();
      say(`Added to ${task.title}.`, { interrupt: true });
      setShare(null);
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : 'Could not upload. Check the connection.');
    } finally {
      setBusy('');
    }
  };

  const newTask = async () => {
    const t = title.trim();
    if (!t) return;
    setBusy('new');
    setMessage('');
    try {
      // The speaker's own item: personal for the MD, their own work for anyone else (the same rule as by voice).
      const path = user.role === 'MD' ? '/private-tasks/' : '/pa-tasks/self/';
      const r = await api<TaskItem>(path, { method: 'POST', body: { title: t }, idempotencyKey: newIdempotencyKey() });
      const made = r.data;
      await apiUpload(`/${made.scope === 'private' ? 'private-tasks' : 'shared-tasks'}/${made.id}/files/`, file);
      dataChanged.emit();
      say(`Saved: ${t}, with the ${isPhoto ? 'photo' : 'file'}.`, { interrupt: true });
      setShare(null);
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : 'Could not save. Check the connection.');
    } finally {
      setBusy('');
    }
  };

  return (
    <Modal visible transparent animationType="slide" onRequestClose={() => setShare(null)}>
      <Pressable style={s.backdrop} onPress={() => setShare(null)} />
      <View style={[s.sheet, { paddingBottom: insets.bottom + 16 }]}>
        <View style={s.grabber} />
        <View style={{ flexDirection: 'row', alignItems: 'center' }}>
          <Icon name={isPhoto ? 'image-outline' : 'document-attach-outline'} size={20} color={colors.accent} />
          <Txt variant="title" style={{ marginLeft: 8, flex: 1 }} numberOfLines={1}>{isPhoto ? 'A photo for JARVIS' : share.name}</Txt>
          <Pressable onPress={() => setShare(null)} hitSlop={10} accessibilityLabel="Close"><Icon name="close" size={20} color={colors.inkSoft} /></Pressable>
        </View>
        <Txt variant="small" style={{ marginTop: 4, color: colors.inkSoft }}>Which task does it belong to?</Txt>
        <View style={s.newRow}>
          <TextInput value={title} onChangeText={setTitle} placeholder="New task for it, e.g. Signboard repair" placeholderTextColor={colors.faint}
            style={[s.input, { flex: 1 }]} maxLength={200} />
          <Button title="New task" compact loading={busy === 'new'} disabled={!title.trim() || !!busy} onPress={newTask} style={{ marginLeft: 8 }} />
        </View>
        {candidates.length ? <Txt variant="label" style={{ marginTop: 12, marginBottom: 6 }}>Or add to an open task</Txt> : null}
        <ScrollView style={{ maxHeight: 300 }}>
          {candidates.map((t, i) => (
            <TaskRow key={t.id} item={t} flat last={i === candidates.length - 1} onPress={busy ? undefined : () => attachTo(t)} />
          ))}
        </ScrollView>
        {message ? <Txt variant="small" style={{ marginTop: 8, color: colors.warn }}>{message}</Txt> : null}
      </View>
    </Modal>
  );
}

const s = StyleSheet.create({
  backdrop: { flex: 1, backgroundColor: colors.overlay },
  sheet: { backgroundColor: colors.bg, borderTopLeftRadius: radius.xl, borderTopRightRadius: radius.xl, paddingHorizontal: 20, paddingTop: 10 },
  grabber: { alignSelf: 'center', width: 44, height: 5, borderRadius: 3, backgroundColor: colors.line, marginBottom: 14 },
  newRow: { flexDirection: 'row', alignItems: 'center', marginTop: 12 },
  input: { backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, borderRadius: radius.md, paddingHorizontal: 14, paddingVertical: 11, fontSize: 16, color: colors.text },
});
