/**
 * Phone: the home is a dashboard. Greeting, "next up", stat tiles, reports he can open and download, today's
 * items with progress, what is coming up this week, the latest messages, the latest updates, the latest answer.
 * The orb floats in the bottom-right corner and opens the talk layer.
 */
import * as Haptics from 'expo-haptics';
import { LinearGradient } from 'expo-linear-gradient';
import { useRouter } from 'expo-router';
import React, { useEffect, useRef, useState } from 'react';
import { Pressable, RefreshControl, ScrollView, StyleSheet, View } from 'react-native';
import { SafeAreaView, useSafeAreaInsets } from 'react-native-safe-area-context';

import { useAlertEngine } from '@/lib/alertEngine';
import { unlock } from '@/lib/appLock';
import { useConversation } from '@/lib/conversation';
import { greeting, relativeAgo, timeOf } from '@/lib/format';
import { addressOf, mdWord } from '@/lib/names';
import { presenceLabel } from '@/lib/presence';
import { colors, fonts, gradients, kindIcon, radius, shadow, tone } from '@/lib/theme';
import type { MessageBrief, ServerAlert, SessionUser, TaskItem } from '@/lib/types';
import { useDashboard } from '@/lib/useDashboard';
import { useWalkie } from '@/lib/walkie';

import { AssistantStage, CAN_BLUR } from './AssistantStage';
import { DataCard } from './DataCard';
import { Drawer } from './Drawer';
import { navItems } from './nav';
import { WaitingCard } from './AlertBanner';
import { Orb } from './Orb';
import { TaskRow } from './TaskRow';
import { TaskSheet } from './TaskSheet';
import { Icon, IconButton, Txt } from './ui';

const DOT: Record<string, string> = { present: colors.success, away: colors.faint, facedown: colors.inkSoft, meeting: colors.gold, quiet: colors.inkSoft };
const ORB_CORNER = 84; // the small orb in the corner (its halo makes it look a little smaller than this)

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

/** One of the latest messages on the home screen: who, the words, when, and whether it was seen. */
function MessageRow({ m, last, onPress }: { m: MessageBrief; last: boolean; onPress: () => void }) {
  const unseen = m.box === 'inbox' && !m.seen;
  const seenWord = m.box === 'inbox'
    ? (m.seen ? 'Seen' : 'New')
    : m.seen ? 'Seen' : m.seen_count ? `Seen by ${m.seen_count} of ${m.to_count}` : 'Not seen yet';
  return (
    <Pressable onPress={onPress} style={({ pressed }) => [s.msgRow, !last && s.rowLine, pressed && { opacity: 0.8 }]}>
      <View style={[s.msgIcon, { backgroundColor: unseen ? colors.accentSoft : colors.surfaceAlt }]}>
        <Icon name={m.box === 'inbox' ? 'mail-outline' : 'send-outline'} size={15} color={unseen ? colors.accent : colors.inkSoft} />
      </View>
      <View style={{ flex: 1 }}>
        <View style={{ flexDirection: 'row', alignItems: 'center' }}>
          <Txt variant="small" numberOfLines={1} style={{ flex: 1, color: colors.inkSoft, fontWeight: '600' }}>
            {m.box === 'inbox' ? `From ${m.who}` : `To ${m.who}`}
          </Txt>
          <Txt variant="caption" style={{ marginLeft: 8 }}>{relativeAgo(Date.parse(m.at))}</Txt>
        </View>
        <Txt variant="body" numberOfLines={2} style={[{ marginTop: 1 }, unseen && { fontWeight: '600' }]}>{m.text}</Txt>
        <Txt variant="caption" style={{ marginTop: 2, color: unseen ? colors.accent : m.seen ? colors.success : colors.warn }}>{seenWord}</Txt>
      </View>
    </Pressable>
  );
}

/** One of the latest updates (news, decisions, nudges) on the home screen. */
function UpdateRow({ a, last, onPress }: { a: ServerAlert; last: boolean; onPress: () => void }) {
  const icon = a.kind === 'REQUEST_DECIDED' ? 'checkmark-circle-outline' : a.kind === 'STAFF_UPDATE' ? 'briefcase-outline'
    : a.kind === 'TASK_ASSIGNED' ? 'clipboard-outline' : 'information-circle-outline';
  return (
    <Pressable onPress={onPress} style={({ pressed }) => [s.msgRow, !last && s.rowLine, pressed && { opacity: 0.8 }]}>
      <View style={[s.msgIcon, { backgroundColor: colors.surfaceAlt }]}>
        <Icon name={icon} size={15} color={colors.inkSoft} />
      </View>
      <View style={{ flex: 1 }}>
        <View style={{ flexDirection: 'row', alignItems: 'center' }}>
          <Txt variant="small" numberOfLines={1} style={{ flex: 1, color: colors.inkSoft, fontWeight: '600' }}>{a.title}</Txt>
          <Txt variant="caption" style={{ marginLeft: 8 }}>{relativeAgo(Date.parse(a.created_at))}</Txt>
        </View>
        {a.body ? <Txt variant="body" numberOfLines={2} style={{ marginTop: 1 }}>{a.body}</Txt> : null}
      </View>
    </Pressable>
  );
}

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
  const walkie = useWalkie();
  const { data, reload, error: loadError } = useDashboard();
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
  const address = addressOf(user);
  const paName = data?.pa_name || 'your PA';
  const md = mdWord(data);
  const presence = engine.presence.state;
  const items = navItems(user, data, engine);
  // "Need your OK": requests waiting, and approvals asked of him (open, on his list).
  const approvals = data
    ? [...data.overdue, ...data.today, ...data.upcoming, ...data.anytime].filter((t) => t.kind === 'APPROVAL' && t.status !== 'COMPLETED' && t.status !== 'CANCELLED')
    : [];
  const pending = (data?.requests.filter((r) => r.state === 'PENDING').length ?? 0) + approvals.length;
  const next = data ? nextTimed([...data.today, ...data.overdue], now) : null;
  // Today means today: late items from earlier days are a separate chip, not rows that crowd out the day.
  const todayList = data ? data.today.filter((t) => t.status !== 'COMPLETED').slice(0, 6) : [];
  const lateCount = data?.overdue.length ?? 0;
  // The rest of the week, the latest messages (received and sent) and the latest updates, each a short strip with
  // "See all"; the full lists are one tap away.
  const comingUp = data ? data.upcoming.filter((t) => t.status !== 'COMPLETED').slice(0, 4) : [];
  const messages = (data?.messages ?? []).slice(0, 3);
  const unread = data?.messages_unseen ?? 0;
  const updates = engine.news.slice(0, 3);
  // A task he asked to open by voice ("pehla wala kholo"): its sheet comes up just as if he had tapped it.
  const voiceItem = conv.itemToOpen && data
    ? [...data.overdue, ...data.today, ...data.upcoming, ...data.anytime, ...(data.pa_tasks ?? []), ...(data.mg_tasks ?? []), ...(data.given_tasks ?? []), ...(data.team_tasks ?? []), ...(data.md_tasks ?? []), ...data.delegated]
        .find((t) => t.id === conv.itemToOpen) ?? null
    : null;

  const select = (key: string) => {
    setMenu(false);
    if (key === 'settings') router.push('/settings');
    else if (key === 'briefing') router.push('/briefing');
    else if (key === 'held') engine.setHeldVisible(true);
    else if (key === 'messages') report('messages', 'Messages');  // the kept record: received and sent, with Seen
    else if (key === 'private') {
      // The personal list sits behind the fingerprint (when the lock is on).
      unlock('Personal list').then((ok) => ok && router.push({ pathname: '/lists/[section]', params: { section: key } })).catch(() => undefined);
    } else router.push({ pathname: '/lists/[section]', params: { section: key } });
  };
  const tick = () => Haptics.selectionAsync().catch(() => undefined); // a small tap he can feel
  // While JARVIS is up, the page behind goes soft (a real blur on Android 12 and later).
  const soft = conv.talkOpen && CAN_BLUR ? s.blurred : null;
  // The orb in the corner: a tap opens JARVIS the chat way (the orb docked above the type box, the conversation
  // above it) and starts listening at once. "Hey JARVIS" still brings the big orb up in the middle.
  const talk = () => {
    tick();
    conv.openTalk('dock');
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
        { kind: 'performance', label: 'Team performance', icon: 'podium-outline' },
        { kind: 'people', label: 'My team', icon: 'people-outline' },
        { kind: 'done', label: 'Completed', icon: 'checkmark-done-outline' },
        { kind: 'late', label: 'Late items', icon: 'alert-circle-outline' },
        { kind: 'messages', label: 'Messages', icon: 'chatbubbles-outline' },
        { kind: 'pa', label: `With ${paName}`, icon: 'person-outline' },
      ]
    : [
        { kind: 'today', label: 'My day', icon: 'today-outline' },
        { kind: 'pa', label: 'My tasks', icon: 'list-outline' },
        { kind: 'busy', label: `When ${md} is busy`, icon: 'time-outline' },
        { kind: 'performance', label: 'Staff performance', icon: 'podium-outline' },
        { kind: 'people', label: 'Staff list', icon: 'people-outline' },
        { kind: 'done', label: 'Completed', icon: 'checkmark-done-outline' },
        { kind: 'requests', label: `Waiting for ${md}`, icon: 'hand-left-outline' },
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

        {/* What is waiting for him right now: in the page, hiding nothing (see AlertBanner.tsx) */}
        <WaitingCard />

        {/* Next up */}
        <Pressable onPress={() => (next ? setOpenTask(next.item) : !data && loadError ? reload(true) : talk())} style={s.heroWrap}>
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
                <Txt variant="title" style={{ color: colors.white, marginTop: 10 }}>
                  {data ? 'Nothing timed left today' : loadError ? 'Can’t reach the JARVIS server' : 'Loading your day…'}
                </Txt>
                <Txt variant="small" style={{ color: 'rgba(255,255,255,0.85)', marginTop: 4 }}>
                  {!data && loadError
                    ? `${loadError} Check the internet connection, then tap here to try again.`
                    : 'Say “Hey JARVIS” and ask for the plan, or give a task.'}
                </Txt>
              </>
            )}
          </LinearGradient>
        </Pressable>

        {data && loadError ? (
          <Pressable onPress={() => reload(true)} style={s.offline}>
            <Txt variant="small" style={{ color: colors.warn }}>Not up to date: can’t reach the server. Tap to try again.</Txt>
          </Pressable>
        ) : null}

        {/* Stat tiles */}
        <View style={s.tiles}>
          <Tile icon="today-outline" label="Today" value={data?.today.length ?? '–'} toneKey="accent" onPress={() => select('today')} />
          <Tile icon="alert-circle-outline" label="Late" value={data?.overdue.length ?? '–'} toneKey={data?.overdue.length ? 'danger' : 'muted'} onPress={() => select('late')} />
          <Tile icon="checkmark-done-outline" label="Done today" value={data?.completed_today ?? '–'} toneKey="success" onPress={() => report('done', 'Completed')} />
          {pending ? (
            // A request from before work went straight onto the MD's list, still waiting: shown until it is settled.
            <Tile icon="hand-left-outline" label={isMD ? 'Need your OK' : `Waiting for ${md}`} value={pending} toneKey="warn" onPress={() => select('requests')} />
          ) : (
            <Tile icon={unread ? 'mail-unread-outline' : 'chatbubbles-outline'} label={unread ? 'Unread messages' : 'Messages'} value={data ? unread : '–'} toneKey={unread ? 'accent' : 'muted'} onPress={() => select('messages')} />
          )}
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
          <View style={{ flexDirection: 'row', alignItems: 'center' }}>
            <Txt variant="label">Today</Txt>
            {lateCount ? (
              <Pressable onPress={() => select('late')} hitSlop={8} style={s.lateChip} accessibilityLabel={`${lateCount} late from before`}>
                <Icon name="alert-circle-outline" size={12} color={colors.danger} />
                <Txt variant="caption" style={{ color: colors.danger, fontWeight: '600', marginLeft: 4 }}>{lateCount} late</Txt>
              </Pressable>
            ) : null}
          </View>
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

        {/* Coming up: the rest of the week */}
        <View style={s.sectionHead}>
          <View style={{ flexDirection: 'row', alignItems: 'center' }}>
            <Txt variant="label">Coming up</Txt>
            {data?.upcoming.length ? <Txt variant="caption" style={{ marginLeft: 8 }}>{data.upcoming.length} this week</Txt> : null}
          </View>
          <Pressable onPress={() => select('upcoming')} hitSlop={8}>
            <Txt variant="caption" style={{ color: colors.accent, fontWeight: '600' }}>See all</Txt>
          </Pressable>
        </View>
        {comingUp.length ? (
          <View style={s.listCard}>
            {comingUp.map((t, i) => (
              <TaskRow key={`${t.scope}-${t.id}`} item={t} onPress={() => setOpenTask(t)} showStaffStatus={t.assignee?.type !== 'MD'} flat last={i === comingUp.length - 1} />
            ))}
          </View>
        ) : (
          <View style={s.empty}>
            <Icon name="calendar-outline" size={20} color={colors.faint} />
            <Txt variant="small" style={{ textAlign: 'center', marginTop: 6 }}>{data ? 'Nothing more this week.' : 'Loading…'}</Txt>
          </View>
        )}

        {/* Messages: the latest received and sent, kept for good */}
        <View style={s.sectionHead}>
          <View style={{ flexDirection: 'row', alignItems: 'center' }}>
            <Txt variant="label">Messages</Txt>
            {unread ? (
              <View style={s.countChip}>
                <Txt variant="caption" style={{ color: colors.accent, fontWeight: '600' }}>{unread} unread</Txt>
              </View>
            ) : null}
          </View>
          <Pressable onPress={() => select('messages')} hitSlop={8}>
            <Txt variant="caption" style={{ color: colors.accent, fontWeight: '600' }}>See all</Txt>
          </Pressable>
        </View>
        {messages.length ? (
          <View style={s.listCard}>
            {messages.map((m, i) => <MessageRow key={`${m.box}-${m.id}`} m={m} last={i === messages.length - 1} onPress={() => select('messages')} />)}
          </View>
        ) : (
          <View style={s.empty}>
            <Icon name="chatbubbles-outline" size={20} color={colors.faint} />
            <Txt variant="small" style={{ textAlign: 'center', marginTop: 6 }}>{data ? 'No messages yet. Say “Mansi ko bolo…” to send one.' : 'Loading…'}</Txt>
          </View>
        )}

        {/* Updates: small news, decisions and nudges */}
        <View style={s.sectionHead}>
          <View style={{ flexDirection: 'row', alignItems: 'center' }}>
            <Txt variant="label">Updates</Txt>
            {engine.news.length ? (
              <View style={s.countChip}>
                <Txt variant="caption" style={{ color: colors.accent, fontWeight: '600' }}>{engine.news.length} new</Txt>
              </View>
            ) : null}
          </View>
          <Pressable onPress={() => select('updates')} hitSlop={8}>
            <Txt variant="caption" style={{ color: colors.accent, fontWeight: '600' }}>See all</Txt>
          </Pressable>
        </View>
        {updates.length ? (
          <View style={s.listCard}>
            {updates.map((a, i) => <UpdateRow key={a.id} a={a} last={i === updates.length - 1} onPress={() => select('updates')} />)}
          </View>
        ) : (
          <View style={s.empty}>
            <Icon name="newspaper-outline" size={20} color={colors.faint} />
            <Txt variant="small" style={{ textAlign: 'center', marginTop: 6 }}>Nothing new. Decisions, progress and nudges collect here.</Txt>
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

      {/* The same colour-changing orb, small, in the corner: tap to talk (and type, in the same place).
          Saying "Hey JARVIS" brings the big orb up in the middle of the screen. */}
      {conv.talkOpen ? null : (
        <View style={[s.cornerOrb, { bottom: insets.bottom + 10 }]} pointerEvents="box-none">
          <Orb state={conv.orb} size={engine.current ? Math.round(ORB_CORNER * 1.22) : ORB_CORNER} level={conv.level} onPress={talk} shift />
        </View>
      )}
      {/* The walkie-talkie, bottom-left, the orb's twin: tap = pick who; hold = talk to the last people at once. */}
      {!conv.talkOpen && walkie.enabled ? (
        <View style={[s.cornerWalkie, { bottom: insets.bottom + 18 }]} pointerEvents="box-none">
          <Pressable
            onPress={() => { tick(); router.push('/walkie'); }}
            onLongPress={() => { tick(); router.push(walkie.lastTo.length ? '/walkie?quick=1' : '/walkie'); }}
            delayLongPress={250}
            accessibilityLabel="Walkie-talkie"
            style={({ pressed }) => [s.walkieBtn, walkie.state === 'listening' && { backgroundColor: colors.success }, pressed && { transform: [{ scale: 0.94 }] }]}
          >
            <Icon name={walkie.state === 'listening' ? 'volume-high' : 'radio'} size={24} color={colors.white} />
            <View style={[s.walkieDot, { backgroundColor: !walkie.supported ? colors.faint : walkie.connected ? colors.success : colors.warn }]} />
          </Pressable>
        </View>
      ) : null}

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
  offline: { marginTop: 12, paddingVertical: 8, paddingHorizontal: 12, borderRadius: 10, backgroundColor: colors.warnSoft },
  top: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingHorizontal: 10, paddingTop: 2 },
  badge: { position: 'absolute', top: 8, right: 8, width: 8, height: 8, borderRadius: 4, backgroundColor: colors.accent },
  pill: { flexDirection: 'row', alignItems: 'center', backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, borderRadius: 999, paddingHorizontal: 12, paddingVertical: 6 },
  dot: { width: 8, height: 8, borderRadius: 4, marginRight: 7 },
  body: { paddingHorizontal: 20, paddingTop: 8 },
  blurred: { filter: [{ blur: 14 }] },
  cornerOrb: { position: 'absolute', right: 6, width: ORB_CORNER, height: ORB_CORNER },
  cornerWalkie: { position: 'absolute', left: 16 },
  walkieBtn: { width: 56, height: 56, borderRadius: 28, backgroundColor: colors.accentDeep, alignItems: 'center', justifyContent: 'center', ...shadow, shadowOpacity: 0.25, elevation: 8 },
  walkieDot: { position: 'absolute', top: 3, right: 3, width: 11, height: 11, borderRadius: 6, borderWidth: 2, borderColor: colors.bg },
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
  lateChip: { flexDirection: 'row', alignItems: 'center', marginLeft: 10, paddingHorizontal: 8, paddingVertical: 3, borderRadius: 999, backgroundColor: colors.dangerSoft },
  report: { flexDirection: 'row', alignItems: 'center', backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, borderRadius: 999, paddingVertical: 11, paddingHorizontal: 16 },
  listCard: { backgroundColor: colors.surface, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line, paddingHorizontal: 4, ...shadow },
  empty: { alignItems: 'center', paddingVertical: 22, paddingHorizontal: 16, borderRadius: radius.lg, borderWidth: 1, borderStyle: 'dashed', borderColor: colors.line },
  countChip: { marginLeft: 10, paddingHorizontal: 8, paddingVertical: 3, borderRadius: 999, backgroundColor: colors.accentSoft },
  msgRow: { flexDirection: 'row', alignItems: 'flex-start', paddingVertical: 12, paddingHorizontal: 10 },
  rowLine: { borderBottomWidth: 1, borderBottomColor: colors.line },
  msgIcon: { width: 30, height: 30, borderRadius: 15, alignItems: 'center', justifyContent: 'center', marginRight: 10, marginTop: 2 },
});
