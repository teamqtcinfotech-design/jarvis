import { Redirect, useRouter } from 'expo-router';
import React, { useState } from 'react';
import { ActivityIndicator, Pressable, RefreshControl, ScrollView, StyleSheet, View } from 'react-native';
import { SafeAreaView, useSafeAreaInsets } from 'react-native-safe-area-context';

import { PresenceBar } from '@/components/PresenceBar';
import { RequestCard } from '@/components/RequestCard';
import { TaskRow } from '@/components/TaskRow';
import { TaskSheet } from '@/components/TaskSheet';
import { Empty, Icon, IconButton, SectionHeader, Tag, Txt } from '@/components/ui';
import { greeting, relativeAgo } from '@/lib/format';
import { useSession } from '@/lib/session';
import { colors, fonts, radius, shadow } from '@/lib/theme';
import type { Dashboard, SessionUser, TaskItem } from '@/lib/types';
import { useDashboard } from '@/lib/useDashboard';

function Tile({ label, value, tone, icon }: { label: string; value: number; tone?: string; icon: string }) {
  return (
    <View style={s.tile}>
      <Icon name={icon} size={16} color={tone ?? colors.muted} />
      <Txt style={[s.tileValue, tone && value ? { color: tone } : null]}>{value}</Txt>
      <Txt variant="caption" style={{ color: colors.muted }}>{label}</Txt>
    </View>
  );
}

function TaskSection({ title, items, onOpen, empty, showStaffStatus }: { title: string; items: TaskItem[]; onOpen: (t: TaskItem) => void; empty?: string; showStaffStatus?: boolean }) {
  if (!items.length && !empty) return null;
  return (
    <>
      <SectionHeader title={title} count={items.length} />
      {items.length ? items.map((t) => <TaskRow key={`${t.scope}-${t.id}`} item={t} onPress={() => onOpen(t)} showStaffStatus={showStaffStatus} />) : <Empty text={empty!} />}
    </>
  );
}

const openOnly = (items: TaskItem[]) => items.filter((d) => d.status !== 'COMPLETED' && d.status !== 'CANCELLED');

function MdBody({ data, onOpen }: { data: Dashboard; onOpen: (t: TaskItem) => void }) {
  const pending = data.requests.filter((r) => r.state === 'PENDING');
  const staffOpen = openOnly(data.delegated);
  const staffDone = data.delegated.filter((d) => d.status === 'COMPLETED').slice(0, 3);
  const paTasks = data.pa_tasks ?? [];
  return (
    <>
      {pending.length ? (
        <>
          <SectionHeader title="Need your OK" count={pending.length} />
          {pending.map((r) => <RequestCard key={r.id} request={r} mode="md" />)}
        </>
      ) : null}
      <TaskSection title="Late" items={data.overdue} onOpen={onOpen} />
      <TaskSection title="Today" items={data.today} onOpen={onOpen} empty="Nothing planned for today." />
      <TaskSection title="Coming up" items={data.upcoming} onOpen={onOpen} />
      <TaskSection title="No date" items={data.anytime.slice(0, 8)} onOpen={onOpen} />
      <SectionHeader title="Given to your PA" count={openOnly(paTasks).length} />
      {paTasks.length
        ? paTasks.map((t) => <TaskRow key={t.id} item={t} onPress={() => onOpen(t)} showStaffStatus />)
        : <Empty text="Say “PA ko bolo…” to give your PA a task." />}
      <SectionHeader title="Given to staff" count={staffOpen.length} action={<Tag label={data.taskpanel.label} icon="briefcase-outline" />} />
      {!data.taskpanel.linked ? <Empty text="Your account is not connected to the staff task app yet, so staff tasks can't be sent." /> : null}
      {staffOpen.length
        ? staffOpen.map((t) => <TaskRow key={t.id} item={t} onPress={() => onOpen(t)} showStaffStatus />)
        : <Empty text="Say “Ramesh ko bolo…” to give work to someone in the team." />}
      {staffDone.map((t) => <TaskRow key={t.id} item={t} onPress={() => onOpen(t)} showStaffStatus />)}
    </>
  );
}

function PaBody({ data, onOpen }: { data: Dashboard; onOpen: (t: TaskItem) => void }) {
  const busy = data.md_busy_today ?? [];
  const mdTasks = data.md_tasks ?? [];
  const staffOpen = openOnly(data.delegated);
  const fromSir = data.overdue.length + data.today.length + data.upcoming.length + data.anytime.length;
  return (
    <>
      <SectionHeader title="From Sir" count={fromSir} />
      {!fromSir ? <Empty text="No tasks from Sir right now." /> : null}
      <TaskSection title="Late" items={data.overdue} onOpen={onOpen} />
      <TaskSection title="Today" items={data.today} onOpen={onOpen} />
      <TaskSection title="Coming up" items={data.upcoming} onOpen={onOpen} />
      <TaskSection title="No date" items={data.anytime} onOpen={onOpen} />
      <SectionHeader title="When Sir is busy today" />
      <View style={s.busyCard}>
        {busy.length ? (
          busy.map((w) => <Tag key={w.start} label={w.label} icon="time-outline" color={colors.ink} bg={colors.goldSoft} />)
        ) : (
          <Txt variant="small">No busy times today.</Txt>
        )}
        <Txt variant="caption" style={{ marginTop: 8, width: '100%' }}>Sir’s personal plans are private. You only see when he is busy.</Txt>
      </View>
      <SectionHeader title="Your requests to Sir" count={data.requests.filter((r) => r.state === 'PENDING').length} />
      {data.requests.length ? data.requests.slice(0, 8).map((r) => <RequestCard key={r.id} request={r} mode="pa" />) : <Empty text="Say “Sir ke liye kal 3 baje…” to send Sir a task." />}
      <TaskSection title="Sir’s tasks from your requests" items={mdTasks} onOpen={onOpen} />
      <SectionHeader title="Given to staff" count={staffOpen.length} action={<Tag label={data.taskpanel.label} icon="briefcase-outline" />} />
      {data.delegated.length ? data.delegated.slice(0, 20).map((t) => <TaskRow key={t.id} item={t} onPress={() => onOpen(t)} showStaffStatus />) : <Empty text="Give work to the team by voice or text." />}
    </>
  );
}

export default function Home() {
  const { user, ready } = useSession();
  if (!ready) return null;
  if (!user) return <Redirect href="/login" />;
  return <HomeScreen user={user} />;
}

function HomeScreen({ user }: { user: SessionUser }) {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const { data, loading, error, updatedAt, reload } = useDashboard();
  const [openTask, setOpenTask] = useState<TaskItem | null>(null);

  const isMD = user.role === 'MD';
  const pendingCount = data?.requests.filter((r) => r.state === 'PENDING').length ?? 0;

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }} edges={['top']}>
      <ScrollView
        contentContainerStyle={{ padding: 20, paddingBottom: 150 }}
        refreshControl={<RefreshControl refreshing={false} onRefresh={() => reload(true)} tintColor={colors.gold} />}
      >
        <View style={s.header}>
          <View style={{ flex: 1 }}>
            <Txt variant="caption" style={{ color: colors.muted }}>{data?.date_label ?? ' '}</Txt>
            <Txt style={s.greeting}>{greeting()}, {isMD ? 'Sir' : user.name}</Txt>
          </View>
          <IconButton icon="settings-outline" label="Settings" onPress={() => router.push('/settings')} />
        </View>

        <PresenceBar />

        {loading && !data ? <ActivityIndicator color={colors.gold} style={{ marginTop: 40 }} /> : null}
        {error ? (
          <View style={s.error}>
            <Icon name="cloud-offline-outline" size={18} color={colors.danger} />
            <Txt variant="small" style={{ color: colors.danger, marginLeft: 8, flex: 1 }}>{error}</Txt>
          </View>
        ) : null}

        {data ? (
          <>
            <View style={s.tiles}>
              <Tile icon="today-outline" label="Today" value={data.today.length} />
              <Tile icon="alert-circle-outline" label="Late" value={data.overdue.length} tone={colors.danger} />
              <Tile icon="hand-left-outline" label={isMD ? 'Need your OK' : 'Waiting for Sir'} value={pendingCount} tone={colors.gold} />
              <Tile icon="briefcase-outline" label="With staff" value={openOnly(data.delegated).length} />
            </View>
            {isMD ? <MdBody data={data} onOpen={setOpenTask} /> : <PaBody data={data} onOpen={setOpenTask} />}
            <Txt variant="caption" style={{ textAlign: 'center', marginTop: 20 }}>Updated {relativeAgo(updatedAt)} · pull down to refresh</Txt>
          </>
        ) : null}
      </ScrollView>

      <View style={[s.dock, { paddingBottom: insets.bottom + 14 }]}>
        <Pressable style={s.round} onPress={() => router.push({ pathname: '/talk', params: { mode: 'type' } })} accessibilityLabel="Type to JARVIS">
          <Icon name="create-outline" size={21} color={colors.ink} />
        </Pressable>
        <Pressable
          style={({ pressed }) => [s.talk, pressed && { transform: [{ scale: 0.97 }] }]}
          onPress={() => router.push('/talk')}
          accessibilityLabel="Talk to JARVIS"
        >
          <Icon name="mic" size={22} color={colors.white} />
          <Txt style={s.talkText}>Talk to JARVIS</Txt>
        </Pressable>
        <Pressable style={s.round} onPress={() => router.push('/briefing')} accessibilityLabel="Briefing">
          <Icon name="sunny-outline" size={21} color={colors.ink} />
        </Pressable>
      </View>

      {openTask ? <TaskSheet item={openTask} onClose={() => setOpenTask(null)} /> : null}
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  header: { flexDirection: 'row', alignItems: 'flex-start' },
  greeting: { fontFamily: fonts.serif, fontSize: 28, lineHeight: 38, color: colors.ink, marginTop: 2 },
  tiles: { flexDirection: 'row', columnGap: 8, marginTop: 16 },
  tile: {
    flex: 1,
    backgroundColor: colors.surface,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.line,
    paddingVertical: 12,
    alignItems: 'center',
  },
  tileValue: { fontFamily: fonts.serif, fontSize: 24, lineHeight: 32, color: colors.ink, marginTop: 2 },
  error: { flexDirection: 'row', alignItems: 'center', backgroundColor: colors.dangerSoft, borderRadius: radius.md, padding: 12, marginTop: 12 },
  busyCard: { flexDirection: 'row', flexWrap: 'wrap', rowGap: 6, backgroundColor: colors.surface, borderRadius: radius.md, padding: 12, borderWidth: 1, borderColor: colors.line },
  dock: {
    position: 'absolute',
    left: 0,
    right: 0,
    bottom: 0,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    columnGap: 14,
    paddingTop: 12,
    backgroundColor: colors.bg,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: colors.line,
  },
  round: {
    width: 50,
    height: 50,
    borderRadius: 25,
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.line,
    alignItems: 'center',
    justifyContent: 'center',
  },
  talk: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: colors.ink,
    borderRadius: 999,
    height: 56,
    paddingHorizontal: 26,
    ...shadow,
    shadowOpacity: 0.18,
  },
  talkText: { color: colors.white, fontSize: 16, fontWeight: '600', marginLeft: 10 },
});
