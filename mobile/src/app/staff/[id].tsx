/**
 * One staff member: who they are, their progress at a glance, every task TaskPanel holds for them (open first), and
 * "give a task" right here. Progress is theirs to move in TaskPanel (start, hold, done); this screen shows it as it
 * stands and refreshes while it is open.
 */
import { Redirect, useLocalSearchParams, useRouter } from 'expo-router';
import React, { useEffect, useRef, useState } from 'react';
import { Pressable, RefreshControl, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { Button, Chip, Empty, Icon, IconButton, PriorityTag, Tag, Txt } from '@/components/ui';
import { ApiError, api, newIdempotencyKey } from '@/lib/api';
import { dataChanged } from '@/lib/bus';
import { relativeAgo } from '@/lib/format';
import { useSession } from '@/lib/session';
import { initials, leaveLabel } from '@/lib/staff';
import { colors, fonts, radius } from '@/lib/theme';
import type { CategoryOption, StaffCard, StaffTask } from '@/lib/types';

const STATUS_TONE: Record<StaffTask['status'], { fg: string; bg: string }> = {
  todo: { fg: colors.muted, bg: colors.surfaceAlt },
  working: { fg: colors.info, bg: colors.infoSoft },
  waiting: { fg: colors.warn, bg: colors.warnSoft },
  done: { fg: colors.success, bg: colors.successSoft },
};
const DUE_OPTIONS: { key: string; label: string; days: number; time: string }[] = [
  { key: 'eod', label: 'Today 6:30 PM', days: 0, time: '18:30' },
  { key: 'tmrw', label: 'Tomorrow', days: 1, time: '18:30' },
  { key: '3d', label: 'In 3 days', days: 3, time: '18:30' },
  { key: 'week', label: 'Next week', days: 7, time: '18:30' },
];
const dayPlus = (days: number) => {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
};

export default function PersonScreen() {
  const { user } = useSession();
  if (!user) return <Redirect href="/login" />;
  return <PersonBody />;
}

function PersonBody() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const router = useRouter();
  const [card, setCard] = useState<StaffCard | null>(null);
  const [error, setError] = useState('');
  const [updatedAt, setUpdatedAt] = useState(0);
  const [open, setOpen] = useState<string | null>(null);
  const [giving, setGiving] = useState(false);
  const [categories, setCategories] = useState<CategoryOption[]>([]);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = async () => {
    try {
      const r = await api<StaffCard>(`/staff/${id}/tasks/`);
      setCard(r.data);
      setUpdatedAt(Date.now());
      setError('');
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not load this person’s work.');
    }
  };
  useEffect(() => {
    const kick = setTimeout(load, 0);
    api<{ categories: CategoryOption[] }>('/staff/').then((r) => setCategories(r.data.categories)).catch(() => undefined);
    timer.current = setInterval(load, 30_000); // progress moves in TaskPanel; the screen keeps up while open
    return () => {
      clearTimeout(kick);
      if (timer.current) clearInterval(timer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per person
  }, [id]);

  const back = () => (router.canGoBack() ? router.back() : router.replace('/staff'));
  const st = card?.staff;
  const sum = card?.summary;
  const openTasks = card?.tasks.filter((t) => t.status !== 'done') ?? [];
  const doneTasks = card?.tasks.filter((t) => t.status === 'done') ?? [];

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }}>
      <View style={s.top}>
        <IconButton icon="chevron-back" label="Back" onPress={back} />
        <Txt variant="title" style={{ flex: 1, textAlign: 'center' }} numberOfLines={1}>{st?.name ?? 'Staff'}</Txt>
        <IconButton icon="refresh-outline" label="Refresh" onPress={load} />
      </View>
      <ScrollView contentContainerStyle={{ padding: 20, paddingBottom: 80 }} refreshControl={<RefreshControl refreshing={false} onRefresh={load} tintColor={colors.accent} />} keyboardShouldPersistTaps="handled">
        {error ? <Txt variant="small" style={{ color: colors.danger, marginBottom: 10 }}>{error}</Txt> : null}
        {!card && !error ? <Txt variant="small" style={{ textAlign: 'center', marginTop: 30 }}>Loading…</Txt> : null}
        {st ? (
          <View style={s.profile}>
            <View style={s.avatar}><Txt style={{ color: colors.accent, fontFamily: fonts.semibold, fontSize: 20 }}>{initials(st.name)}</Txt></View>
            <View style={{ flex: 1 }}>
              <Txt variant="title" numberOfLines={2}>{st.name}</Txt>
              <Txt variant="small" style={{ marginTop: 2 }}>{[st.department, st.level, st.role].filter(Boolean).join(' · ')}</Txt>
              {st.on_leave ? <View style={{ marginTop: 6, alignSelf: 'flex-start' }}><Tag label={leaveLabel(st)} icon="airplane-outline" color={colors.warn} bg={colors.warnSoft} /></View> : null}
            </View>
          </View>
        ) : null}
        {sum ? (
          <View style={s.chips}>
            <Progress label="Open" value={sum.open} fg={colors.accent} bg={colors.accentSoft} />
            <Progress label="Working" value={sum.working} fg={colors.info} bg={colors.infoSoft} />
            <Progress label="On hold" value={sum.waiting} fg={colors.warn} bg={colors.warnSoft} />
            <Progress label="Done" value={sum.done} fg={colors.success} bg={colors.successSoft} />
            <Progress label="Late" value={sum.overdue} fg={colors.danger} bg={colors.dangerSoft} />
          </View>
        ) : null}

        {st ? (
          giving ? (
            <AssignForm staffId={st.id} name={st.name} department={st.department} onLeave={st.on_leave} categories={categories}
              onDone={() => { setGiving(false); load(); dataChanged.emit(); }} onCancel={() => setGiving(false)} />
          ) : (
            <Button title={`Give ${st.name.split(' ')[0]} a task`} icon="add-circle-outline" onPress={() => setGiving(true)} style={{ marginBottom: 18 }} />
          )
        ) : null}

        {card ? (
          <>
            <View style={s.sectionHead}>
              <Txt variant="label">Open</Txt>
              <Txt variant="caption">{openTasks.length}</Txt>
            </View>
            {openTasks.length ? openTasks.map((t) => <TaskLine key={t.id} task={t} open={open === t.id} onPress={() => setOpen(open === t.id ? null : t.id)} />)
              : <Empty text={card.live ? 'Nothing open in the staff panel right now.' : 'Nothing given through JARVIS yet.'} />}
            {doneTasks.length ? (
              <>
                <View style={s.sectionHead}>
                  <Txt variant="label">Done</Txt>
                  <Txt variant="caption">{doneTasks.length}</Txt>
                </View>
                {doneTasks.map((t) => <TaskLine key={t.id} task={t} open={open === t.id} onPress={() => setOpen(open === t.id ? null : t.id)} />)}
              </>
            ) : null}
            <Txt variant="caption" style={{ textAlign: 'center', marginTop: 20 }}>
              {card.live ? 'As the staff panel holds it' : 'What JARVIS gave them'} · updated {relativeAgo(updatedAt)}
            </Txt>
          </>
        ) : null}
      </ScrollView>
    </SafeAreaView>
  );
}

function Progress({ label, value, fg, bg }: { label: string; value: number; fg: string; bg: string }) {
  return (
    <View style={[s.chip, { backgroundColor: value ? bg : colors.surfaceAlt }]}>
      <Txt style={{ color: value ? fg : colors.faint, fontFamily: fonts.semibold, fontSize: 16, lineHeight: 20 }}>{String(value)}</Txt>
      <Txt variant="caption" style={{ color: value ? fg : colors.faint }}>{label}</Txt>
    </View>
  );
}

function TaskLine({ task, open, onPress }: { task: StaffTask; open: boolean; onPress: () => void }) {
  const tone = task.overdue ? { fg: colors.danger, bg: colors.dangerSoft } : STATUS_TONE[task.status];
  return (
    <Pressable onPress={onPress} style={({ pressed }) => [s.task, pressed && { opacity: 0.85 }]}>
      <View style={[s.bar, { backgroundColor: tone.fg }]} />
      <View style={{ flex: 1 }}>
        <Txt variant="heading" numberOfLines={open ? undefined : 2}>{task.title}</Txt>
        <View style={s.meta}>
          <Tag label={task.status_label} color={tone.fg} bg={tone.bg} />
          {task.priority && task.priority !== 'NORMAL' ? <PriorityTag priority={String(task.priority)} /> : null}
          {task.due_label ? <Txt variant="caption" style={{ marginLeft: 6 }}>{task.status === 'done' ? 'was due ' : 'due '}{task.due_label}</Txt> : null}
        </View>
        {task.hold_reason ? <Txt variant="caption" style={{ color: colors.warn, marginTop: 4 }}>On hold: {task.hold_reason}</Txt> : null}
        {open ? (
          <View style={{ marginTop: 8 }}>
            {task.detail ? <Txt variant="small" style={{ marginBottom: 6 }}>{task.detail}</Txt> : null}
            <Txt variant="caption">{[task.category ? `Category ${task.category}` : '', task.sender ? `from ${task.sender}` : '', task.id].filter(Boolean).join(' · ')}</Txt>
            {task.created_at ? <Txt variant="caption">Given {relativeAgo(Date.parse(task.created_at))}{task.updated_at ? ` · last change ${relativeAgo(Date.parse(task.updated_at))}` : ''}</Txt> : null}
            {task.completed_at ? <Txt variant="caption" style={{ color: colors.success }}>Completed {relativeAgo(Date.parse(task.completed_at))}</Txt> : null}
          </View>
        ) : null}
      </View>
      <Icon name={open ? 'chevron-up' : 'chevron-down'} size={16} color={colors.faint} />
    </Pressable>
  );
}

function AssignForm({ staffId, name, department, onLeave, categories, onDone, onCancel }: {
  staffId: number; name: string; department: string; onLeave: boolean; categories: CategoryOption[]; onDone: () => void; onCancel: () => void;
}) {
  const [title, setTitle] = useState('');
  const [priority, setPriority] = useState<'NORMAL' | 'HIGH' | 'URGENT'>('NORMAL');
  const [due, setDue] = useState('tmrw');
  const [cat, setCat] = useState('other');
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [error, setError] = useState('');
  // Only the categories that may go to this person: the ones of their department, and "Other" for everyone.
  const usable = categories.filter((c) => !c.department || c.department === department);
  const keyRef = useRef(newIdempotencyKey());

  const give = async () => {
    const clean = title.trim();
    if (!clean) {
      setError('Say what the task is.');
      return;
    }
    const d = DUE_OPTIONS.find((o) => o.key === due) ?? DUE_OPTIONS[1];
    setBusy(true);
    setError('');
    try {
      const r = await api<{ assignee?: { staff_name?: string } }>('/delegations/', {
        method: 'POST', idempotencyKey: keyRef.current, timeoutMs: 30000,
        body: { title: clean, priority, due_date: dayPlus(d.days), due_time: d.time, staff_id: staffId, category_key: cat, description: clean },
      });
      setNote(r.status === 202 ? `Sent to ${name}; waiting for the staff panel to confirm.` : `Given to ${name}. They have been notified.`);
      setTimeout(onDone, 900);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not give the task.');
      keyRef.current = newIdempotencyKey(); // a fresh try is a fresh command
    } finally {
      setBusy(false);
    }
  };

  return (
    <View style={s.form}>
      <Txt variant="label" style={{ marginBottom: 8 }}>Give {name} a task</Txt>
      {onLeave ? <Txt variant="caption" style={{ color: colors.warn, marginBottom: 8 }}>{name} is on leave; the staff panel will refuse it until they are back.</Txt> : null}
      <TextInput value={title} onChangeText={setTitle} placeholder="What needs doing" placeholderTextColor={colors.faint} style={s.input} multiline autoFocus />
      <Txt variant="caption" style={s.formLabel}>Priority</Txt>
      <View style={s.chipRow}>
        {(['NORMAL', 'HIGH', 'URGENT'] as const).map((p) => <Chip key={p} label={p === 'NORMAL' ? 'Normal' : p === 'HIGH' ? 'High' : 'Urgent'} selected={priority === p} onPress={() => setPriority(p)} color={p === 'URGENT' ? colors.danger : p === 'HIGH' ? colors.warn : colors.ink} />)}
      </View>
      <Txt variant="caption" style={s.formLabel}>Due</Txt>
      <View style={s.chipRow}>{DUE_OPTIONS.map((o) => <Chip key={o.key} label={o.label} selected={due === o.key} onPress={() => setDue(o.key)} />)}</View>
      {usable.length > 1 ? (
        <>
          <Txt variant="caption" style={s.formLabel}>Category</Txt>
          <View style={s.chipRow}>{usable.map((c) => <Chip key={c.key} label={c.name} selected={cat === c.key} onPress={() => setCat(c.key)} />)}</View>
        </>
      ) : null}
      {error ? <Txt variant="small" style={{ color: colors.danger, marginTop: 8 }}>{error}</Txt> : null}
      {note ? <Txt variant="small" style={{ color: colors.success, marginTop: 8 }}>{note}</Txt> : null}
      <View style={{ flexDirection: 'row', justifyContent: 'flex-end', marginTop: 12 }}>
        <Button title="Cancel" kind="ghost" compact onPress={onCancel} />
        <Button title="Give task" icon="send-outline" compact loading={busy} onPress={give} style={{ marginLeft: 8 }} />
      </View>
    </View>
  );
}

const s = StyleSheet.create({
  top: { flexDirection: 'row', alignItems: 'center', paddingHorizontal: 12, paddingTop: 6 },
  profile: { flexDirection: 'row', alignItems: 'center', marginBottom: 14 },
  avatar: { width: 56, height: 56, borderRadius: 28, backgroundColor: colors.accentSoft, alignItems: 'center', justifyContent: 'center', marginRight: 14 },
  chips: { flexDirection: 'row', gap: 8, marginBottom: 16 },
  chip: { flex: 1, borderRadius: radius.md, paddingVertical: 8, alignItems: 'center' },
  sectionHead: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginTop: 8, marginBottom: 8 },
  task: { flexDirection: 'row', alignItems: 'flex-start', backgroundColor: colors.surface, borderRadius: radius.md, borderWidth: 1, borderColor: colors.line, padding: 12, marginBottom: 8 },
  bar: { width: 4, borderRadius: 2, alignSelf: 'stretch', marginRight: 10 },
  meta: { flexDirection: 'row', alignItems: 'center', flexWrap: 'wrap', gap: 6, marginTop: 6 },
  form: { backgroundColor: colors.surface, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.accent, padding: 14, marginBottom: 18 },
  formLabel: { marginTop: 10, marginBottom: 6 },
  input: { minHeight: 48, borderWidth: 1, borderColor: colors.line, borderRadius: radius.md, paddingHorizontal: 12, paddingVertical: 10, color: colors.text, fontFamily: fonts.regular, fontSize: 15, textAlignVertical: 'top' },
  chipRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 6 },
});
