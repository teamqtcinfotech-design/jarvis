import { Redirect, useRouter } from 'expo-router';
import React, { useState } from 'react';
import { ActivityIndicator, Pressable, RefreshControl, ScrollView, StyleSheet, View } from 'react-native';
import { SafeAreaView, useSafeAreaInsets } from 'react-native-safe-area-context';

import { PresenceBar } from '@/components/PresenceBar';
import { RequestCard } from '@/components/RequestCard';
import { TaskRow } from '@/components/TaskRow';
import { TaskSheet } from '@/components/TaskSheet';
import { Empty, Icon, IconButton, SectionHeader, Tag, Txt } from '@/components/ui';
import { VoiceSheet } from '@/components/VoiceSheet';
import { greeting, relativeAgo } from '@/lib/format';
import { useSession } from '@/lib/session';
import { colors, radius, shadow } from '@/lib/theme';
import type { Dashboard, SessionUser, TaskItem } from '@/lib/types';
import { useDashboard } from '@/lib/useDashboard';

function Stat({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <View style={s.stat}>
      <Txt style={[s.statValue, tone ? { color: tone } : null]}>{value}</Txt>
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

function MdBody({ data, onOpen }: { data: Dashboard; onOpen: (t: TaskItem) => void }) {
  const pending = data.requests.filter((r) => r.state === 'PENDING');
  const delegatedOpen = data.delegated.filter((d) => d.status !== 'COMPLETED' && d.status !== 'CANCELLED');
  const delegatedDone = data.delegated.filter((d) => d.status === 'COMPLETED').slice(0, 3);
  return (
    <>
      {pending.length ? (
        <>
          <SectionHeader title="Awaiting your decision" count={pending.length} />
          {pending.map((r) => <RequestCard key={r.id} request={r} mode="md" />)}
        </>
      ) : null}
      <TaskSection title="Overdue" items={data.overdue} onOpen={onOpen} />
      <TaskSection title="Today" items={data.today} onOpen={onOpen} empty="Nothing scheduled for today." />
      <TaskSection title="This week" items={data.upcoming} onOpen={onOpen} />
      <TaskSection title="Anytime" items={data.anytime.slice(0, 8)} onOpen={onOpen} />
      <SectionHeader title="Given to your PA" count={(data.pa_tasks ?? []).filter((t) => t.status !== 'COMPLETED').length} />
      {(data.pa_tasks ?? []).length
        ? (data.pa_tasks ?? []).map((t) => <TaskRow key={t.id} item={t} onPress={() => onOpen(t)} showStaffStatus />)
        : <Empty text="Say “PA ko bolo…” or “Priya ko bolo…” to give your PA a task." />}
      <SectionHeader title="Delegated to staff" count={delegatedOpen.length} action={<Tag label={data.taskpanel.label} icon="git-network-outline" />} />
      {!data.taskpanel.linked ? <Empty text="Your account is not linked to TaskPanel yet, so staff tasks cannot be sent." /> : null}
      {delegatedOpen.length ? delegatedOpen.map((t) => <TaskRow key={t.id} item={t} onPress={() => onOpen(t)} showStaffStatus />) : <Empty text="Say “Ramesh Kumar ko bolo…” to assign work to staff through TaskPanel." />}
      {delegatedDone.map((t) => <TaskRow key={t.id} item={t} onPress={() => onOpen(t)} showStaffStatus />)}
    </>
  );
}

function PaBody({ data, onOpen }: { data: Dashboard; onOpen: (t: TaskItem) => void }) {
  const busy = data.md_busy_today ?? [];
  const mdTasks = data.md_tasks ?? [];
  const delegatedOpen = data.delegated.filter((d) => d.status !== 'COMPLETED' && d.status !== 'CANCELLED');
  const fromSir = data.overdue.length + data.today.length + data.upcoming.length + data.anytime.length;
  return (
    <>
      <SectionHeader title="From Sir" count={fromSir} />
      {!fromSir ? <Empty text="No tasks from Sir right now." /> : null}
      <TaskSection title="Overdue" items={data.overdue} onOpen={onOpen} />
      <TaskSection title="Today" items={data.today} onOpen={onOpen} />
      <TaskSection title="This week" items={data.upcoming} onOpen={onOpen} />
      <TaskSection title="Anytime" items={data.anytime} onOpen={onOpen} />
      <SectionHeader title="MD availability today" />
      <View style={s.busyCard}>
        {busy.length ? (
          busy.map((w) => <Tag key={w.start} label={w.label} icon="time-outline" color={colors.ink} bg={colors.goldSoft} />)
        ) : (
          <Txt variant="small">No busy blocks recorded for today.</Txt>
        )}
        <Txt variant="caption" style={{ marginTop: 8, width: '100%' }}>Details of MD’s personal schedule are private.</Txt>
      </View>
      <SectionHeader title="Your requests to MD" count={data.requests.filter((r) => r.state === 'PENDING').length} />
      {data.requests.length ? data.requests.slice(0, 8).map((r) => <RequestCard key={r.id} request={r} mode="pa" />) : <Empty text="Say “Sir ke liye kal 3 baje…” to send MD a task." />}
      <TaskSection title="MD tasks from your requests" items={mdTasks} onOpen={onOpen} />
      <SectionHeader title="Delegated to staff" count={delegatedOpen.length} action={<Tag label={data.taskpanel.label} icon="git-network-outline" />} />
      {data.delegated.length ? data.delegated.slice(0, 20).map((t) => <TaskRow key={t.id} item={t} onPress={() => onOpen(t)} showStaffStatus />) : <Empty text="Assign work to staff by voice or text." />}
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
  const [voiceMode, setVoiceMode] = useState<'voice' | 'type' | null>(null);
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
            <Txt variant="label">{data?.date_label ?? ' '}</Txt>
            <Txt variant="display" style={{ marginTop: 4 }}>
              {greeting()}, {isMD ? 'Sir' : user.name}
            </Txt>
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
            <View style={s.stats}>
              <Stat label="Today" value={data.today.length} />
              <Stat label="Overdue" value={data.overdue.length} tone={data.overdue.length ? colors.danger : undefined} />
              <Stat label={isMD ? 'To decide' : 'Pending'} value={pendingCount} tone={pendingCount ? colors.gold : undefined} />
              <Stat label="Delegated" value={data.delegated.filter((d) => d.status !== 'COMPLETED' && d.status !== 'CANCELLED').length} />
            </View>
            {isMD ? <MdBody data={data} onOpen={setOpenTask} /> : <PaBody data={data} onOpen={setOpenTask} />}
            <Txt variant="caption" style={{ textAlign: 'center', marginTop: 20 }}>Updated {relativeAgo(updatedAt)}</Txt>
          </>
        ) : null}
      </ScrollView>

      <View style={[s.dock, { paddingBottom: insets.bottom + 14 }]}>
        <Pressable style={s.dockSide} onPress={() => setVoiceMode('type')} accessibilityLabel="Type a command">
          <Icon name="create-outline" size={22} color={colors.ink} />
          <Txt variant="caption" style={s.dockLabel}>Type</Txt>
        </Pressable>
        <Pressable style={({ pressed }) => [s.mic, pressed && { transform: [{ scale: 0.96 }] }]} onPress={() => setVoiceMode('voice')} accessibilityLabel="Speak to JARVIS">
          <Icon name="mic" size={30} color={colors.white} />
        </Pressable>
        <Pressable style={s.dockSide} onPress={() => router.push('/briefing')} accessibilityLabel="Briefing">
          <Icon name="sunny-outline" size={22} color={colors.ink} />
          <Txt variant="caption" style={s.dockLabel}>Briefing</Txt>
        </Pressable>
      </View>

      {voiceMode ? <VoiceSheet startMode={voiceMode} onClose={() => setVoiceMode(null)} /> : null}
      {openTask ? <TaskSheet item={openTask} onClose={() => setOpenTask(null)} /> : null}
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  header: { flexDirection: 'row', alignItems: 'flex-start' },
  stats: {
    flexDirection: 'row',
    backgroundColor: colors.ink,
    borderRadius: radius.lg,
    paddingVertical: 16,
    marginTop: 16,
    ...shadow,
  },
  stat: { flex: 1, alignItems: 'center' },
  statValue: { fontFamily: 'serif', fontSize: 26, color: colors.white },
  error: { flexDirection: 'row', alignItems: 'center', backgroundColor: colors.dangerSoft, borderRadius: radius.md, padding: 12, marginTop: 12 },
  busyCard: { flexDirection: 'row', flexWrap: 'wrap', rowGap: 6, backgroundColor: colors.surface, borderRadius: radius.md, padding: 12, borderWidth: 1, borderColor: colors.line },
  dock: {
    position: 'absolute',
    left: 0,
    right: 0,
    bottom: 0,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-around',
    paddingTop: 12,
    backgroundColor: 'rgba(246,243,236,0.97)',
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: colors.line,
  },
  dockSide: { alignItems: 'center', width: 80 },
  dockLabel: { color: colors.ink, marginTop: 2 },
  mic: {
    width: 72,
    height: 72,
    borderRadius: 36,
    backgroundColor: colors.ink,
    alignItems: 'center',
    justifyContent: 'center',
    borderWidth: 3,
    borderColor: colors.gold,
    marginTop: -28,
    ...shadow,
    shadowOpacity: 0.25,
  },
});
