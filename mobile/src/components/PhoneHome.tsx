/**
 * Phone: the home is a dashboard. Greeting, "next up", stat tiles, reports he can open and download, today's
 * items with progress, the latest answer. The orb floats in the bottom-right corner and opens the talk layer.
 */
import * as Haptics from 'expo-haptics';
import { LinearGradient } from 'expo-linear-gradient';
import { useRouter } from 'expo-router';
import React, { useEffect, useRef, useState } from 'react';
import { Pressable, RefreshControl, ScrollView, StyleSheet, View } from 'react-native';
import { SafeAreaView, useSafeAreaInsets } from 'react-native-safe-area-context';

import { useAlertEngine } from '@/lib/alertEngine';
import { useConversation } from '@/lib/conversation';
import { greeting, timeOf } from '@/lib/format';
import { presenceLabel } from '@/lib/presence';
import { colors, fonts, gradients, kindIcon, radius, shadow, tone } from '@/lib/theme';
import type { SessionUser, TaskItem } from '@/lib/types';
import { useDashboard } from '@/lib/useDashboard';

import { AssistantStage, CAN_BLUR } from './AssistantStage';
import { DataCard } from './DataCard';
import { Drawer } from './Drawer';
import { navItems } from './nav';
import { TaskRow } from './TaskRow';
import { TaskSheet } from './TaskSheet';
import { Icon, IconButton, Txt } from './ui';

const DOT: Record<string, string> = { present: colors.success, away: colors.faint, facedown: colors.inkSoft, meeting: colors.gold, quiet: colors.inkSoft };

function nextTimed(items: TaskItem[], now: number): { item: TaskItem; start: number } | null {
  const timed = items
    .map((t) => ({ item: t, start: Date.parse(t.scheduled_start ?? t.due_at ?? '') }))
    .filter((x) => !Number.isNaN(x.start) && x.start >= now - 5 * 60_000 && x.item.status !== 'COMPLETED' && x.item.status !== 'CANCELLED')
    .sort((a, b) => a.start - b.start);
  return timed[0] ?? null;
}

const inWords = (ms: number) => {
  const m = Math.round(ms / 60_000);
  if (m <= 0) return 'now';
  if (m < 60) return `in ${m} min`;
  const h = Math.floor(m / 60);
  return `in ${h} h${m % 60 ? ` ${m % 60} min` : ''}`;
};

function Tile({ icon, label, value, toneKey, onPress }: { icon: string; label: string; value: number | string; toneKey: string; onPress: () => void }) {
  const c = tone[toneKey] ?? tone.muted;
  return (
    <Pressable onPress={onPress} style={({ pressed }) => [s.tile, pressed && { opacity: 0.85, transform: [{ scale: 0.98 }] }]}>
      <View style={s.tileTop}>
        <View style={[s.tileIcon, { backgroundColor: c.bg }]}>
          <Icon name={icon} size={16} color={c.fg} />
        </View>
        <Icon name="chevron-forward" size={14} color={colors.faint} />
      </View>
      <Txt style={s.tileValue}>{String(value)}</Txt>
      <Txt variant="caption" style={{ color: colors.muted }}>{label}</Txt>
    </Pressable>
  );
}

export function PhoneHome({ user }: { user: SessionUser }) {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const conv = useConversation();
  const engine = useAlertEngine();
  const { data, reload } = useDashboard();
  const [menu, setMenu] = useState(false);
  const [openTask, setOpenTask] = useState<TaskItem | null>(null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(t);
  }, []);

  // An answer that arrives while the talk sheet is closed must not sit unseen at the bottom of the page:
  // the page moves to it.
  const pageRef = useRef<React.ComponentRef<typeof ScrollView>>(null);
  const answerY = useRef(0);
  const sheetOpen = useRef(false);
  useEffect(() => {
    sheetOpen.current = conv.talkOpen;
  }, [conv.talkOpen]);
  const newestAnswer = conv.answers[0]?.id;
  useEffect(() => {
    if (!newestAnswer || sheetOpen.current) return undefined;
    const t = setTimeout(() => pageRef.current?.scrollTo({ y: Math.max(0, answerY.current - 8), animated: true }), 200);
    return () => clearTimeout(t);
  }, [newestAnswer]);

  const isMD = user.role === 'MD';
  const address = isMD ? 'Sir' : user.name;
  const presence = engine.presence.state;
  const items = navItems(user, data, engine);
  const pending = data?.requests.filter((r) => r.state === 'PENDING').length ?? 0;
  const next = data ? nextTimed([...data.today, ...data.overdue], now) : null;
  const todayList = data ? [...data.overdue, ...data.today].filter((t) => t.status !== 'COMPLETED').slice(0, 6) : [];
  // A task he asked to open by voice ("pehla wala kholo"): its sheet comes up just as if he had tapped it.
  const voiceItem = conv.itemToOpen && data
    ? [...data.overdue, ...data.today, ...data.upcoming, ...data.anytime, ...(data.pa_tasks ?? []), ...(data.md_tasks ?? []), ...data.delegated]
        .find((t) => t.id === conv.itemToOpen) ?? null
    : null;

  const select = (key: string) => {
    setMenu(false);
    if (key === 'settings') router.push('/settings');
    else if (key === 'briefing') router.push('/briefing');
    else if (key === 'held') engine.setHeldVisible(true);
    else router.push({ pathname: '/lists/[section]', params: { section: key } });
  };
  const tick = () => Haptics.selectionAsync().catch(() => undefined); // a small tap he can feel
  // While JARVIS is up, the page behind goes soft (a real blur on Android 12 and later).
  const soft = conv.talkOpen && CAN_BLUR ? s.blurred : null;
  const write = () => {
    tick();
    conv.openTalk('type');
  };
  const talk = () => {
    tick();
    conv.openTalk();
    if (conv.orb === 'idle') conv.micPress();
  };
  // A report opens right in front of him (the sheet shows it fitted to the screen), not somewhere down the page.
  const report = (kind: string, label: string) => {
    tick();
    conv.showView(kind, label);
    conv.openTalk();
  };
  const reports = isMD
    ? [
        { kind: 'today', label: "Today's plan", icon: 'today-outline' },
        { kind: 'week', label: 'This week', icon: 'bar-chart-outline' },
        { kind: 'performance', label: 'Staff performance', icon: 'podium-outline' },
        { kind: 'people', label: 'My staff', icon: 'people-outline' },
        { kind: 'done', label: 'Completed', icon: 'checkmark-done-outline' },
        { kind: 'late', label: 'Late items', icon: 'alert-circle-outline' },
        { kind: 'pa', label: 'With Mansi', icon: 'person-outline' },
      ]
    : [
        { kind: 'today', label: 'My day', icon: 'today-outline' },
        { kind: 'pa', label: 'My tasks', icon: 'list-outline' },
        { kind: 'busy', label: 'When Sir is busy', icon: 'time-outline' },
        { kind: 'performance', label: 'Staff performance', icon: 'podium-outline' },
        { kind: 'people', label: 'Staff list', icon: 'people-outline' },
        { kind: 'done', label: 'Completed', icon: 'checkmark-done-outline' },
        { kind: 'requests', label: 'Waiting for Sir', icon: 'hand-left-outline' },
      ];

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }} edges={['top']}>
      <View style={[s.top, soft]}>
        <View>
          <IconButton icon="menu-outline" label="Menu" onPress={() => setMenu(true)} />
          {pending + engine.held.length > 0 ? <View style={s.badge} /> : null}
        </View>
        <View style={s.pill}>
          <View style={[s.dot, { backgroundColor: DOT[presence] }]} />
          <Txt variant="caption" style={{ color: colors.inkSoft, fontWeight: '500' }}>{presenceLabel[presence].title}</Txt>
        </View>
        <IconButton icon="settings-outline" label="Settings" onPress={() => router.push('/settings')} />
      </View>

      <ScrollView
        ref={pageRef}
        style={soft}
        contentContainerStyle={[s.body, { paddingBottom: insets.bottom + 96 }]}
        refreshControl={<RefreshControl refreshing={false} onRefresh={() => reload(true)} tintColor={colors.accent} />}
        showsVerticalScrollIndicator={false}
      >
        <Txt variant="small" style={{ color: colors.muted, fontWeight: '500' }}>{greeting()},</Txt>
        <Txt variant="display">{address}</Txt>
        <Txt variant="caption" style={{ marginTop: 2 }}>{data?.date_label ?? ''}</Txt>

        {/* Next up */}
        <Pressable onPress={() => (next ? setOpenTask(next.item) : talk())} style={s.heroWrap}>
          <LinearGradient colors={[...gradients.orb]} start={{ x: 0, y: 0 }} end={{ x: 1, y: 1 }} style={s.hero}>
            <View style={s.heroTop}>
              <View style={s.heroIcon}>
                <Icon name={next ? kindIcon[next.item.kind] ?? 'time-outline' : 'sparkles-outline'} size={16} color={colors.white} />
              </View>
              <Txt variant="label" style={{ color: 'rgba(255,255,255,0.85)' }}>{next ? 'Next up' : 'Right now'}</Txt>
              {next ? <Txt variant="caption" style={s.heroWhen}>{timeOf(next.item.scheduled_start ?? next.item.due_at)}</Txt> : null}
            </View>
            {next ? (
              <>
                <Txt variant="title" numberOfLines={2} style={{ color: colors.white, marginTop: 10 }}>{next.item.title}</Txt>
                <Txt variant="small" style={{ color: 'rgba(255,255,255,0.85)', marginTop: 4 }}>
                  {inWords(next.start - now)}{next.item.scope === 'private' ? ' · private' : ''}
                  {next.item.priority === 'URGENT' ? ' · urgent' : ''}
                </Txt>
              </>
            ) : (
              <>
                <Txt variant="title" style={{ color: colors.white, marginTop: 10 }}>{data ? 'Nothing timed left today' : 'Loading your day…'}</Txt>
                <Txt variant="small" style={{ color: 'rgba(255,255,255,0.85)', marginTop: 4 }}>Say “Hey JARVIS” and ask for the plan, or give a task.</Txt>
              </>
            )}
          </LinearGradient>
        </Pressable>

        {/* Stat tiles */}
        <View style={s.tiles}>
          <Tile icon="today-outline" label="Today" value={data?.today.length ?? '–'} toneKey="accent" onPress={() => select('today')} />
          <Tile icon="alert-circle-outline" label="Late" value={data?.overdue.length ?? '–'} toneKey={data?.overdue.length ? 'danger' : 'muted'} onPress={() => select('late')} />
          <Tile icon="checkmark-done-outline" label="Done today" value={data?.completed_today ?? '–'} toneKey="success" onPress={() => report('done', 'Completed')} />
          <Tile icon="hand-left-outline" label={isMD ? 'Need your OK' : 'Waiting for Sir'} value={pending} toneKey={pending ? 'warn' : 'muted'} onPress={() => select('requests')} />
        </View>

        {/* Reports */}
        <View style={s.sectionHead}>
          <Txt variant="label">Reports</Txt>
          <Txt variant="caption">Tap to open</Txt>
        </View>
        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ columnGap: 8, paddingRight: 20 }} style={{ marginHorizontal: -20, paddingLeft: 20 }}>
          {reports.map((r) => (
            <Pressable key={r.kind} onPress={() => report(r.kind, r.label)} style={({ pressed }) => [s.report, pressed && { opacity: 0.8 }]}>
              <Icon name={r.icon} size={16} color={colors.accent} />
              <Txt variant="small" style={{ color: colors.text, marginLeft: 8, fontWeight: '500' }}>{r.label}</Txt>
            </Pressable>
          ))}
        </ScrollView>

        {/* Today */}
        <View style={s.sectionHead}>
          <Txt variant="label">Today</Txt>
          <Pressable onPress={() => select('today')} hitSlop={8}>
            <Txt variant="caption" style={{ color: colors.accent, fontWeight: '600' }}>See all</Txt>
          </Pressable>
        </View>
        {todayList.length ? (
          <View style={s.listCard}>
            {todayList.map((t, i) => (
              <TaskRow key={`${t.scope}-${t.id}`} item={t} onPress={() => setOpenTask(t)} showStaffStatus={t.assignee?.type !== 'MD'} flat last={i === todayList.length - 1} />
            ))}
          </View>
        ) : (
          <View style={s.empty}>
            <Icon name="sunny-outline" size={20} color={colors.faint} />
            <Txt variant="small" style={{ textAlign: 'center', marginTop: 6 }}>{data ? 'Nothing planned for today. Say “kal 11 baje…” to add something.' : 'Loading…'}</Txt>
          </View>
        )}

        {/* Latest answer */}
        {conv.answers.length ? (
          <View onLayout={(e) => { answerY.current = e.nativeEvent.layout.y; }}>
            <View style={s.sectionHead}>
              <Txt variant="label">{conv.answers[0].question}</Txt>
              <Pressable onPress={conv.clearAnswers} hitSlop={8}>
                <Txt variant="caption" style={{ color: colors.muted }}>Clear</Txt>
              </Pressable>
            </View>
            <DataCard view={conv.answers[0].view} extraText={conv.answers[0].text} onRemove={() => conv.removeAnswer(conv.answers[0].id)} />
          </View>
        ) : null}
      </ScrollView>

      {/* No orb sitting on the page. One small pencil in the corner: tap to type, hold to talk.
          Saying "Hey JARVIS" brings the orb up in the middle of the screen. */}
      {conv.talkOpen ? null : (
        <Pressable
          onPress={write}
          onLongPress={talk}
          accessibilityLabel="Type to JARVIS (hold to talk)"
          style={({ pressed }) => [s.pencil, { bottom: insets.bottom + 18 }, pressed && { transform: [{ scale: 0.93 }] }]}
        >
          <Icon name="create-outline" size={23} color={colors.white} />
        </Pressable>
      )}

      <Drawer
        open={menu}
        onClose={() => setMenu(false)}
        title={isMD ? 'Managing Director' : user.name}
        subtitle={data?.date_label ?? ''}
        items={items}
        onSelect={select}
        meetingMode={engine.presence.inMeeting}
        onMeetingMode={engine.setMeetingMode}
        presence={presenceLabel[presence].detail}
        meetingFromDiary={engine.presence.diaryMeeting && !engine.meetingMode}
      />
      <AssistantStage />
      {openTask ?? voiceItem ? (
        <TaskSheet item={openTask ?? voiceItem} onClose={() => { setOpenTask(null); conv.clearItemToOpen(); }} />
      ) : null}
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  top: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingHorizontal: 10, paddingTop: 2 },
  badge: { position: 'absolute', top: 8, right: 8, width: 8, height: 8, borderRadius: 4, backgroundColor: colors.accent },
  pill: { flexDirection: 'row', alignItems: 'center', backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, borderRadius: 999, paddingHorizontal: 12, paddingVertical: 6 },
  dot: { width: 8, height: 8, borderRadius: 4, marginRight: 7 },
  body: { paddingHorizontal: 20, paddingTop: 8 },
  blurred: { filter: [{ blur: 14 }] },
  pencil: {
    position: 'absolute', right: 18, width: 54, height: 54, borderRadius: 27, backgroundColor: colors.ink,
    alignItems: 'center', justifyContent: 'center', ...shadow, shadowOpacity: 0.28, elevation: 6,
  },
  heroWrap: { marginTop: 18, borderRadius: radius.lg, ...shadow, shadowOpacity: 0.18, shadowColor: colors.accentDeep },
  hero: { borderRadius: radius.lg, padding: 18 },
  heroTop: { flexDirection: 'row', alignItems: 'center' },
  heroIcon: { width: 28, height: 28, borderRadius: 14, backgroundColor: 'rgba(255,255,255,0.2)', alignItems: 'center', justifyContent: 'center', marginRight: 8 },
  heroWhen: { marginLeft: 'auto', color: colors.white, fontFamily: fonts.semibold, fontSize: 13 },
  tiles: { flexDirection: 'row', flexWrap: 'wrap', marginTop: 14, gap: 10 },
  tile: { width: '48%', flexGrow: 1, backgroundColor: colors.surface, borderRadius: radius.md, borderWidth: 1, borderColor: colors.line, padding: 14 },
  tileTop: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 },
  tileIcon: { width: 30, height: 30, borderRadius: 15, alignItems: 'center', justifyContent: 'center' },
  tileValue: { fontSize: 24, fontWeight: '700', color: colors.ink, lineHeight: 30 },
  sectionHead: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginTop: 24, marginBottom: 10 },
  report: { flexDirection: 'row', alignItems: 'center', backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, borderRadius: 999, paddingVertical: 11, paddingHorizontal: 16 },
  listCard: { backgroundColor: colors.surface, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line, paddingHorizontal: 4, ...shadow },
  empty: { alignItems: 'center', paddingVertical: 22, paddingHorizontal: 16, borderRadius: radius.lg, borderWidth: 1, borderStyle: 'dashed', borderColor: colors.line },
});
