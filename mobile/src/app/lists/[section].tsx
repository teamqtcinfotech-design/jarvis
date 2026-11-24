import { Redirect, useLocalSearchParams, useRouter } from 'expo-router';
import React, { useEffect, useState } from 'react';
import { Pressable, RefreshControl, ScrollView, StyleSheet, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { RequestCard } from '@/components/RequestCard';
import { TaskRow } from '@/components/TaskRow';
import { TaskSheet } from '@/components/TaskSheet';
import { Button, Card, Empty, Icon, IconButton, Tag, Txt } from '@/components/ui';
import { useAlertEngine } from '@/lib/alertEngine';
import { ApiError, api } from '@/lib/api';
import { dataChanged } from '@/lib/bus';
import { relativeAgo } from '@/lib/format';
import { useLayout } from '@/lib/layout';
import { mdHe, mdWord } from '@/lib/names';
import { useSession } from '@/lib/session';
import { colors, radius } from '@/lib/theme';
import type { Dashboard, DataView, ServerAlert, TaskItem, ViewRow } from '@/lib/types';
import { useDashboard } from '@/lib/useDashboard';

interface Section {
  title: string;
  empty: string;
  staff?: boolean;
  items: (d: Dashboard) => TaskItem[];
}

const all = (d: Dashboard) => [...d.overdue, ...d.today, ...d.upcoming, ...d.anytime];
const isMeeting = (t: TaskItem) => t.kind === 'MEETING' || t.kind === 'CALL';

const SECTIONS: Record<string, Section> = {
  today: { title: 'Today', empty: 'Nothing planned for today.', items: (d) => d.today },
  // Tasks and meetings apart, today and ahead (the home screen's four cards).
  today_tasks: { title: 'Today’s tasks', empty: 'No tasks today.', items: (d) => d.today.filter((t) => !isMeeting(t)) },
  today_meetings: { title: 'Today’s meetings', empty: 'No meetings or calls today.', items: (d) => d.today.filter(isMeeting) },
  late: { title: 'Late', empty: 'Nothing is late. Well done.', items: (d) => d.overdue },
  upcoming: { title: 'Coming up', empty: 'Nothing coming up.', items: (d) => d.upcoming },
  upcoming_tasks: { title: 'Coming up · tasks', empty: 'No tasks ahead.', items: (d) => d.upcoming.filter((t) => !isMeeting(t)) },
  upcoming_meetings: { title: 'Coming up · meetings', empty: 'No meetings or calls ahead.', items: (d) => d.upcoming.filter(isMeeting) },
  nodate: { title: 'No date yet', empty: 'No undated tasks.', items: (d) => d.anytime },
  private: { title: 'Personal', empty: 'No personal items. Only you would see them.', items: (d) => all(d).filter((t) => t.scope === 'private') },
  pa: { title: 'Given to your PA', empty: 'Say “PA ko bolo…” to give your PA a task.', staff: true, items: (d) => d.pa_tasks ?? [] },
  md_tasks: { title: 'Tasks you gave the MD', empty: 'None yet. Say “Sir ko bolo…” to put something on the MD’s list.', staff: true, items: (d) => d.md_tasks ?? [] },
  mg: { title: 'Given to the manager', empty: 'Say “Mansi ko bolo…” to give the manager a task.', staff: true, items: (d) => d.mg_tasks ?? [] },
  given: { title: 'Given by you', empty: 'Nothing given yet. Say “Sunil ko bolo…” to give someone work.', staff: true, items: (d) => d.given_tasks ?? [] },
  team: { title: 'Team’s work', empty: 'Nobody on the team has open work.', staff: true, items: (d) => d.team_tasks ?? [] },
  staff: { title: 'Given to staff', empty: 'Say “Ramesh ko bolo…” to give work to someone in the team.', staff: true, items: (d) => d.delegated },
};

/** His own lists come in parts (his rule, 24 Nov 2026): personal items, work others gave him, his own official
 *  work, and then what he gave others that falls in the same window. */
const GROUPED = new Set(['today', 'today_tasks', 'today_meetings', 'late', 'upcoming', 'upcoming_tasks', 'upcoming_meetings', 'nodate']);
const localIso = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
function groupsOf(section: string, items: TaskItem[], d: Dashboard): { title: string; items: TaskItem[]; staff?: boolean }[] {
  const today = localIso(new Date());
  const inWindow = (t: TaskItem) =>
    section.startsWith('today') ? t.due_date === today && !t.overdue
      : section === 'late' ? t.overdue
        : section.startsWith('upcoming') ? !!t.due_date && t.due_date > today
          : !t.due_date && !t.due_at;
  const ofKind = (t: TaskItem) => (section.endsWith('_tasks') ? !isMeeting(t) : section.endsWith('_meetings') ? isMeeting(t) : true);
  const fromOthers = (t: TaskItem) => t.scope !== 'private' && (t.origin ?? 'SELF') !== 'SELF';
  return [
    { title: 'Personal', items: items.filter((t) => t.scope === 'private') },
    { title: 'For you', items: items.filter(fromOthers) },
    { title: 'Your own work', items: items.filter((t) => t.scope !== 'private' && !fromOthers(t)) },
    { title: 'Given by you', items: (d.given_tasks ?? []).filter((t) => t.status !== 'COMPLETED' && t.status !== 'CANCELLED' && inWindow(t) && ofKind(t)), staff: true },
  ].filter((g) => g.items.length);
}

/** Signed out (by hand, or the login expired) while a list is open: go to the login page before the screen below
 *  runs, because the dashboard it depends on is gone the moment the user is. */
export default function ListScreen() {
  const { user } = useSession();
  if (!user) return <Redirect href="/login" />;
  return <ListBody user={user} />;
}

/** Small news, decisions and nudges from the server: read here, and counted as seen once looked at. */
function UpdatesList() {
  const engine = useAlertEngine();
  const items = engine.news;
  // Low-priority news is seen by being looked at; anything with a question stays until it is answered.
  useEffect(() => {
    const quiet = items.filter((a) => a.priority === 'LOW' && !a.question).map((a) => a.id);
    if (quiet.length) engine.markRead(quiet).catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once, when the screen opens
  }, []);
  if (!items.length) return <Empty text="Nothing new. Decisions, staff progress and nudges collect here." />;
  const when = (a: ServerAlert) => relativeAgo(Date.parse(a.created_at));
  return (
    <>
      {items.map((a) => (
        <Card key={a.id} style={s.update}>
          <View style={{ flexDirection: 'row', alignItems: 'center' }}>
            <Icon name={a.kind === 'REQUEST_DECIDED' ? 'checkmark-circle-outline' : a.kind === 'STAFF_UPDATE' ? 'briefcase-outline' : 'information-circle-outline'} size={16} color={colors.accent} />
            <Txt variant="label" style={{ marginLeft: 8, flex: 1 }}>{a.title}</Txt>
            <Txt variant="caption">{when(a)}</Txt>
          </View>
          {a.body ? <Txt variant="small" style={{ marginTop: 6 }}>{a.body}</Txt> : null}
          {a.question?.kind === 'approve' ? (
            // An approval still waiting: decided from here as well as from the banner.
            <View style={{ flexDirection: 'row', justifyContent: 'flex-end', marginTop: 8 }}>
              <Button title="Decline" kind="danger" compact onPress={() => engine.answerAlert(a, false).catch(() => undefined)} />
              <Button title="Approve" icon="checkmark" compact onPress={() => engine.answerAlert(a, true).catch(() => undefined)} style={{ marginLeft: 6 }} />
            </View>
          ) : a.ring && !a.seen_at ? (
            // Still ringing until Seen: the Seen is here too.
            <View style={{ flexDirection: 'row', justifyContent: 'flex-end', marginTop: 8 }}>
              <Button title={a.seen_label || 'Seen'} icon="eye-outline" compact onPress={() => engine.seenAlert(a).catch(() => undefined)} />
            </View>
          ) : (
            <Pressable onPress={() => engine.markRead([a.id]).catch(() => undefined)} hitSlop={8} style={{ alignSelf: 'flex-end', marginTop: 8 }}>
              <Txt variant="caption" style={{ color: colors.accent, fontWeight: '600' }}>Got it</Txt>
            </Pressable>
          )}
        </Card>
      ))}
      {items.length > 1 ? (
        <Button title="Mark all as seen" kind="ghost" compact onPress={() => engine.markRead(items.map((a) => a.id)).catch(() => undefined)} style={{ alignSelf: 'center', marginTop: 6 }} />
      ) : null}
    </>
  );
}

/** What was cancelled in the last 30 days; any one comes back with a tap. */
function CancelledList() {
  const [view, setView] = useState<DataView | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const load = () => api<DataView>('/views/cancelled/').then((r) => setView(r.data)).catch(() => setError('Could not load the list.'));
  useEffect(() => {
    const kick = setTimeout(load, 0);
    return () => clearTimeout(kick);
  }, []);
  const restore = async (row: ViewRow) => {
    setBusy(row.id);
    setError('');
    try {
      const path = row.scope === 'private' ? `/private-tasks/${row.id}/` : `/shared-tasks/${row.id}/`;
      await api(path, { method: 'PATCH', body: { expected_version: row.version ?? 1, status: 'OPEN' } });
      dataChanged.emit();
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not restore it.');
    } finally {
      setBusy('');
    }
  };
  if (!view) return <Txt variant="small" style={{ textAlign: 'center', marginTop: 30 }}>{error || 'Loading…'}</Txt>;
  if (!view.rows.length) return <Empty text="Nothing cancelled in the last 30 days." />;
  return (
    <>
      {error ? <Txt variant="small" style={{ color: colors.danger, marginBottom: 8 }}>{error}</Txt> : null}
      {view.rows.map((row) => (
        <Card key={`${row.scope}-${row.id}`} style={s.update}>
          <Txt variant="heading" numberOfLines={2}>{row.title}</Txt>
          <Txt variant="small" style={{ marginTop: 2 }}>{[row.day, row.who].filter(Boolean).join(' · ')}{row.private ? ' · private' : ''}</Txt>
          <Button title="Restore" kind="secondary" compact icon="refresh-outline" loading={busy === row.id} onPress={() => restore(row)} style={{ alignSelf: 'flex-end', marginTop: 8 }} />
        </Card>
      ))}
    </>
  );
}

function ListBody({ user }: { user: NonNullable<ReturnType<typeof useSession>['user']> }) {
  const { section = 'today' } = useLocalSearchParams<{ section: string }>();
  const router = useRouter();
  const { data, error, updatedAt, reload } = useDashboard();
  const { isTablet } = useLayout();
  const [openTask, setOpenTask] = useState<TaskItem | null>(null);
  const isMD = user.role === 'MD';

  const back = () => (router.canGoBack() ? router.back() : router.replace('/'));
  const md = mdWord(data);
  let title = SECTIONS[section]?.title ?? 'Tasks';
  if (section === 'requests') title = isMD ? 'Need your OK' : `Waiting for ${md}`;
  if (section === 'md_tasks') title = user.role === 'PA' || user.role === 'MG' ? `${md}’s official tasks` : `${md}’s tasks from you`;
  if (section === 'busy') title = `When ${md} is busy today`;
  if (section === 'updates') title = 'Updates';
  if (section === 'cancelled') title = 'Cancelled';

  let body: React.ReactNode = null;
  if (section === 'updates') {
    body = <UpdatesList />;
  } else if (section === 'cancelled') {
    body = <CancelledList />;
  } else if (!data) {
    body = <Txt variant="small" style={{ textAlign: 'center', marginTop: 30 }}>{error || 'Loading…'}</Txt>;
  } else if (section === 'requests') {
    const list = isMD ? data.requests.filter((r) => r.state === 'PENDING') : data.requests.slice(0, 20);
    // Approvals asked of him sit here too (open, on his list): tap one for Approve / Decline.
    const approvals = [...data.overdue, ...data.today, ...data.upcoming, ...data.anytime]
      .filter((t) => t.kind === 'APPROVAL' && t.status !== 'COMPLETED' && t.status !== 'CANCELLED');
    body = list.length || approvals.length
      ? (
        <>
          {approvals.map((t) => <TaskRow key={`${t.scope}-${t.id}`} item={t} onPress={() => setOpenTask(t)} />)}
          {list.map((r) => <RequestCard key={r.id} request={r} mode={isMD ? 'md' : 'pa'} />)}
        </>
      )
      : <Empty text={isMD ? 'Nothing waiting for your decision. Work given to you goes straight onto your list now.' : `Nothing waiting. Work for ${md} goes straight onto ${md}’s list now.`} />;
  } else if (section === 'busy') {
    const busy = data.md_busy_today ?? [];
    body = (
      <View style={s.busyCard}>
        {busy.length ? busy.map((w) => <Tag key={w.start} label={w.label} icon="time-outline" color={colors.ink} bg={colors.goldSoft} />) : <Txt variant="small">No busy times today.</Txt>}
        <Txt variant="caption" style={{ marginTop: 8, width: '100%' }}>{md}’s personal plans are private. You only see when {mdHe(data)} is busy.</Txt>
      </View>
    );
  } else {
    const sec = SECTIONS[section] ?? SECTIONS.today;
    const items = sec.items(data);
    // The PA's real name in the hint, not a typed-in one.
    const empty = section === 'pa' && data.pa_name ? `Say “${data.pa_name} ko bolo…” to give ${data.pa_name} a task.` : sec.empty;
    const groups = GROUPED.has(section) ? groupsOf(section, items, data) : [];
    body = groups.length
      ? groups.map((g) => (
          <View key={g.title}>
            <View style={s.groupHead}>
              <Txt variant="label">{g.title.toUpperCase()}</Txt>
              <Txt variant="caption" style={{ marginLeft: 8 }}>{g.items.length}</Txt>
            </View>
            {g.items.map((t) => <TaskRow key={`${t.scope}-${t.id}`} item={t} onPress={() => setOpenTask(t)} showStaffStatus={g.staff} />)}
          </View>
        ))
      : items.length
        ? items.map((t) => <TaskRow key={`${t.scope}-${t.id}`} item={t} onPress={() => setOpenTask(t)} showStaffStatus={sec.staff} />)
        : <Empty text={empty} />;
  }

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }}>
      <View style={s.top}>
        <IconButton icon="chevron-back" label="Back" onPress={back} />
        <Txt variant="title" style={{ flex: 1, textAlign: 'center', marginRight: 40 }}>{title}</Txt>
      </View>
      <ScrollView
        contentContainerStyle={[{ padding: 20, paddingBottom: 60 }, isTablet && { maxWidth: 820, width: '100%', alignSelf: 'center' }]}
        refreshControl={<RefreshControl refreshing={false} onRefresh={() => reload(true)} tintColor={colors.gold} />}
      >
        {body}
        {data ? <Txt variant="caption" style={{ textAlign: 'center', marginTop: 20 }}>Updated {relativeAgo(updatedAt)} · pull down to refresh</Txt> : null}
      </ScrollView>
      {openTask ? <TaskSheet item={openTask} onClose={() => setOpenTask(null)} /> : null}
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  top: { flexDirection: 'row', alignItems: 'center', paddingHorizontal: 12, paddingTop: 6 },
  groupHead: { flexDirection: 'row', alignItems: 'baseline', marginTop: 14, marginBottom: 6 },
  busyCard: { flexDirection: 'row', flexWrap: 'wrap', rowGap: 6, backgroundColor: colors.surface, borderRadius: 12, padding: 12, borderWidth: 1, borderColor: colors.line },
  update: { marginBottom: 10, borderRadius: radius.md },
});
