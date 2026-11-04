import { Redirect, useLocalSearchParams, useRouter } from 'expo-router';
import React, { useState } from 'react';
import { RefreshControl, ScrollView, StyleSheet, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { RequestCard } from '@/components/RequestCard';
import { TaskRow } from '@/components/TaskRow';
import { TaskSheet } from '@/components/TaskSheet';
import { Empty, IconButton, Tag, Txt } from '@/components/ui';
import { relativeAgo } from '@/lib/format';
import { useSession } from '@/lib/session';
import { colors } from '@/lib/theme';
import type { Dashboard, TaskItem } from '@/lib/types';
import { useDashboard } from '@/lib/useDashboard';

interface Section {
  title: string;
  empty: string;
  staff?: boolean;
  items: (d: Dashboard) => TaskItem[];
}

const all = (d: Dashboard) => [...d.overdue, ...d.today, ...d.upcoming, ...d.anytime];

const SECTIONS: Record<string, Section> = {
  today: { title: 'Today', empty: 'Nothing planned for today.', items: (d) => d.today },
  late: { title: 'Late', empty: 'Nothing is late. Well done.', items: (d) => d.overdue },
  upcoming: { title: 'Coming up', empty: 'Nothing coming up this week.', items: (d) => d.upcoming },
  nodate: { title: 'No date yet', empty: 'No undated tasks.', items: (d) => d.anytime },
  private: { title: 'Private reminders', empty: 'No private reminders.', items: (d) => all(d).filter((t) => t.scope === 'private') },
  pa: { title: 'Given to your PA', empty: 'Say “Mansi ko bolo…” to give your PA a task.', staff: true, items: (d) => d.pa_tasks ?? [] },
  md_tasks: { title: 'Sir’s tasks from your requests', empty: 'None yet.', items: (d) => d.md_tasks ?? [] },
  staff: { title: 'Given to staff', empty: 'Say “Ramesh ko bolo…” to give work to someone in the team.', staff: true, items: (d) => d.delegated },
};

export default function ListScreen() {
  const { section = 'today' } = useLocalSearchParams<{ section: string }>();
  const { user } = useSession();
  const router = useRouter();
  const { data, error, updatedAt, reload } = useDashboard();
  const [openTask, setOpenTask] = useState<TaskItem | null>(null);
  if (!user) return <Redirect href="/login" />;
  const isMD = user.role === 'MD';

  const back = () => (router.canGoBack() ? router.back() : router.replace('/'));
  let title = SECTIONS[section]?.title ?? 'Tasks';
  if (section === 'requests') title = isMD ? 'Need your OK' : 'Waiting for Sir';
  if (section === 'busy') title = 'When Sir is busy today';

  let body: React.ReactNode = null;
  if (!data) {
    body = <Txt variant="small" style={{ textAlign: 'center', marginTop: 30 }}>{error || 'Loading…'}</Txt>;
  } else if (section === 'requests') {
    const list = isMD ? data.requests.filter((r) => r.state === 'PENDING') : data.requests.slice(0, 20);
    body = list.length
      ? list.map((r) => <RequestCard key={r.id} request={r} mode={isMD ? 'md' : 'pa'} />)
      : <Empty text={isMD ? 'Nothing waiting for your decision.' : 'Say “Sir ke liye kal 3 baje…” to send Sir a task.'} />;
  } else if (section === 'busy') {
    const busy = data.md_busy_today ?? [];
    body = (
      <View style={s.busyCard}>
        {busy.length ? busy.map((w) => <Tag key={w.start} label={w.label} icon="time-outline" color={colors.ink} bg={colors.goldSoft} />) : <Txt variant="small">No busy times today.</Txt>}
        <Txt variant="caption" style={{ marginTop: 8, width: '100%' }}>Sir’s personal plans are private. You only see when he is busy.</Txt>
      </View>
    );
  } else {
    const sec = SECTIONS[section] ?? SECTIONS.today;
    const items = sec.items(data);
    body = items.length
      ? items.map((t) => <TaskRow key={`${t.scope}-${t.id}`} item={t} onPress={() => setOpenTask(t)} showStaffStatus={sec.staff} />)
      : <Empty text={sec.empty} />;
  }

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }}>
      <View style={s.top}>
        <IconButton icon="chevron-back" label="Back" onPress={back} />
        <Txt variant="title" style={{ flex: 1, textAlign: 'center', marginRight: 40 }}>{title}</Txt>
      </View>
      <ScrollView
        contentContainerStyle={{ padding: 20, paddingBottom: 60 }}
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
  busyCard: { flexDirection: 'row', flexWrap: 'wrap', rowGap: 6, backgroundColor: colors.surface, borderRadius: 12, padding: 12, borderWidth: 1, borderColor: colors.line },
});
