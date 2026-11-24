/**
 * Phone: the home is the day. Greeting and date; four round counts (today's tasks, today's meetings, messages,
 * alerts), each carrying what is waiting of its kind and opening it on a tap; up to three blue information banners
 * (a meeting about to start, overdue work in free time, the next timed item); then, below the fold, the reports and
 * the latest answer. Staff and the company panel live in the side menu.
 * The orb floats in the bottom-right corner and opens the talk layer.
 */
import * as Haptics from 'expo-haptics';
import { LinearGradient } from 'expo-linear-gradient';
import { useRouter } from 'expo-router';
import React, { useEffect, useRef, useState } from 'react';
import { Pressable, RefreshControl, ScrollView, StyleSheet, View } from 'react-native';
import { SafeAreaView, useSafeAreaInsets } from 'react-native-safe-area-context';

import { type AlertGroup, chipOf, type EngineItem, pileOf, useAlertEngine } from '@/lib/alertEngine';
import { unlock } from '@/lib/appLock';
import { useConversation } from '@/lib/conversation';
import { greeting, relativeAgo, timeOf } from '@/lib/format';
import { addressOf, mdWord } from '@/lib/names';
import { presenceLabel } from '@/lib/presence';
import { colors, fonts, gradients, kindIcon, radius, shadow } from '@/lib/theme';
import type { Kind, Priority, SessionUser, TaskItem } from '@/lib/types';
import { useDashboard } from '@/lib/useDashboard';
import { useWalkie } from '@/lib/walkie';

import { alertLabel } from './AlertBanner';
import { AssistantStage, CAN_BLUR } from './AssistantStage';
import { DataCard } from './DataCard';
import { Drawer } from './Drawer';
import { navItems } from './nav';
import { Orb } from './Orb';
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

/** One information banner (his home screen, 24 Nov 2026): what it is, when, one or two lines, one tap. */
type Banner = { key: string; icon: string; label: string; when: string; lines: string[]; sub: string; go: () => void };
/** Work that can be fitted into free time (never a meeting or a call) and its usual minutes when none is estimated. */
const USUAL: Partial<Record<Kind, number>> = { TASK: 30, PAYMENT: 10, FOLLOW_UP: 15 };
const RANK: Record<Priority, number> = { URGENT: 0, HIGH: 1, NORMAL: 2, LOW: 3 };
const CHIP_ICON: Record<AlertGroup | 'other', string> = {
  task: 'checkbox-outline', meeting: 'people-outline', message: 'chatbubble-ellipses-outline', other: 'notifications-outline',
  approval: 'notifications-outline', request: 'notifications-outline', update: 'notifications-outline',
};

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
  // Free right now? Nothing timed is running, and the next timed item is at least half an hour away (or there is none).
  const freeTill = (() => {
    if (!data) return null;
    const spans = data.today
      .filter((t) => t.status !== 'COMPLETED' && (t.scheduled_start ?? t.due_at))
      .map((t) => {
        const start = Date.parse(t.scheduled_start ?? t.due_at!);
        const end = t.scheduled_end ? Date.parse(t.scheduled_end) : start + 30 * 60_000;
        return { start, end };
      });
    if (spans.some((w) => w.start <= now && now < w.end)) return null;
    const coming = spans.filter((w) => w.start > now).sort((a, b) => a.start - b.start)[0];
    if (coming && coming.start - now < 30 * 60_000) return null;
    return { until: coming ? new Date(coming.start).toISOString() : null };
  })();
  // Today means today: late items from earlier days are a banner, not rows that crowd out the day.
  const isMeeting = (t: TaskItem) => t.kind === 'MEETING' || t.kind === 'CALL';
  const todayOpen = data ? data.today.filter((t) => t.status !== 'COMPLETED') : [];
  const todayMeetings = todayOpen.filter(isMeeting);
  const lateCount = data?.overdue.length ?? 0;
  const unread = data?.messages_unseen ?? 0;
  // What is waiting, by chip: task alerts under Tasks, meeting alerts under Meetings, messages under Messages, the
  // rest (approvals, requests, updates, reminders) under Alerts. The held items count too: the sheet shows them.
  const pile = pileOf(engine.waiting, engine.held);
  const waitingBy = (g: AlertGroup | 'other') => pile.filter((w) => chipOf(w) === g).length;
  const openPile = (g: AlertGroup | 'other', item?: EngineItem) => {
    if (item && engine.waiting.some((w) => w.key === item.key)) engine.focus(item);
    engine.setAllGroup(g);
    engine.setAllVisible(true);
  };
  // The third banner turns every 10 seconds through what is waiting, the latest first. The item on show is the
  // engine's spotlight, so "isko hata do" / "this one, later" by voice means it.
  const [turn, setTurn] = useState(0);
  useEffect(() => {
    if (pile.length < 2) return undefined;
    const t = setInterval(() => setTurn((n) => n + 1), 10_000);
    return () => clearInterval(t);
  }, [pile.length]);
  const shown = pile.length ? pile[turn % pile.length] : null;
  const setSpotlight = engine.setSpotlight;
  useEffect(() => {
    setSpotlight(shown);
  }, [setSpotlight, shown]);
  useEffect(() => () => setSpotlight(null), [setSpotlight]);
  // Free time analysed into named work (his ask): the server's suggestion when it is waiting (a yes places it), else
  // the same fit done here: late first, then due today without a time, then undated; urgent first; each takes its
  // estimated minutes (the kind's usual when none, two hours at most) until the stretch is used.
  const served = pile.find((i) => i.question?.kind === 'place') ?? null;
  const plan: { title: string; minutes: number }[] = (() => {
    if (!data) return [];
    if (served?.question?.plan?.length) return served.question.plan.map((p) => ({ title: p.title, minutes: p.minutes }));
    if (!freeTill) return [];
    const end = freeTill.until ? Date.parse(freeTill.until) : now + 4 * 60 * 60_000;
    const cands = [...data.overdue, ...data.today.filter((t) => !t.due_at || Date.parse(t.due_at) <= now), ...data.anytime]
      .filter((t) => USUAL[t.kind] !== undefined && t.status !== 'COMPLETED' && t.status !== 'CANCELLED' && !(t.scheduled_start && Date.parse(t.scheduled_start) >= now))
      .map((t, i) => ({ t, i, group: t.overdue ? 0 : t.due_date ? 1 : 2 }))
      .sort((a, b) => a.group - b.group || RANK[a.t.priority] - RANK[b.t.priority] || a.i - b.i);
    const out: { title: string; minutes: number }[] = [];
    let cursor = now;
    for (const { t } of cands) {
      const minutes = Math.min(t.estimated_minutes || USUAL[t.kind] || 30, 120);
      if (cursor + minutes * 60_000 > end) continue;
      out.push({ title: t.title, minutes });
      cursor += minutes * 60_000;
    }
    return out;
  })();
  const freeUntil = freeTill?.until ?? served?.question?.until ?? null;
  // The next meeting or call ahead (another day), for when nothing timed is left today.
  const ahead = data
    ? data.upcoming.filter((t) => isMeeting(t) && t.status !== 'COMPLETED' && t.status !== 'CANCELLED')
        .sort((a, b) => (a.due_at ?? a.due_date ?? '').localeCompare(b.due_at ?? b.due_date ?? ''))[0] ?? null
    : null;
  // A task he asked to open by voice ("pehla wala kholo"): its sheet comes up just as if he had tapped it.
  const voiceItem = conv.itemToOpen && data
    ? [...data.overdue, ...data.today, ...data.upcoming, ...data.anytime, ...(data.pa_tasks ?? []), ...(data.mg_tasks ?? []), ...(data.given_tasks ?? []), ...(data.team_tasks ?? []), ...(data.md_tasks ?? []), ...data.delegated]
        .find((t) => t.id === conv.itemToOpen) ?? null
    : null;

  const select = (key: string) => {
    setMenu(false);
    if (key === 'settings') router.push('/settings');
    else if (key === 'briefing') router.push('/briefing');
    else if (key === 'staff_panel') router.push('/staff');
    else if (key === 'company_panel') router.push('/panel');
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

        {/* Four counts at a glance (his layout, 24 Nov 2026): today's tasks and meetings, unread messages, alerts
            waiting. A red badge = that many alerts of that kind waiting (the same rows the sheet then shows); a tap
            opens them (or the day's list). */}
        <View style={s.chips}>
          {([
            { key: 'task', label: 'Tasks', n: todayOpen.filter((t) => !isMeeting(t)).length, badge: waitingBy('task'), tone: colors.accent, list: 'today_tasks' },
            { key: 'meeting', label: 'Meetings', n: todayMeetings.length, badge: waitingBy('meeting'), tone: colors.gold, list: 'today_meetings' },
            { key: 'message', label: 'Messages', n: Math.max(unread, waitingBy('message')), badge: 0, tone: colors.info, list: 'messages' },
            { key: 'other', label: 'Alerts', n: waitingBy('other'), badge: 0, tone: waitingBy('other') ? colors.warn : colors.muted, list: 'updates' },
          ] as { key: AlertGroup | 'other'; label: string; n: number; badge: number; tone: string; list: string }[]).map((c) => (
            <Pressable
              key={c.key}
              onPress={() => { tick(); if (waitingBy(c.key)) openPile(c.key); else select(c.list); }}
              style={s.chip}
              accessibilityLabel={`${c.label}: ${c.n}${c.badge ? `, ${c.badge} waiting` : ''}`}
            >
              <View style={[s.chipRing, { borderColor: c.tone }]}>
                <Txt style={[s.chipN, { color: c.tone }]}>{c.n}</Txt>
                {c.badge ? (
                  <View style={s.chipBadge}>
                    <Txt style={s.chipBadgeN}>{c.badge}</Txt>
                  </View>
                ) : null}
              </View>
              <Txt variant="caption" style={{ marginTop: 4 }}>{c.label}</Txt>
            </Pressable>
          ))}
        </View>

        {/* Three information banners, three fixed slots (his layout): the free time analysed into the task or two
            that fit now (else the most urgent late item by name); the next meeting (today's next timed item, else
            the next meeting ahead; a meeting about to start jumps to the top); and what is waiting, one item at a
            time, turning every 10 seconds. Nothing here repeats a chip's number. */}
        {(() => {
          const soon = !!next && next.start - now <= 5 * 60_000;
          let work: Banner | null = null;
          if (plan.length) {
            const two = plan.slice(0, 2);
            work = { key: 'free', icon: 'sparkles-outline', label: 'Free time', when: freeUntil ? `till ${timeOf(freeUntil)}` : 'rest of the day',
              lines: two.map((p) => p.title),
              sub: `${two.map((p) => p.minutes).join(' + ')} min · ${two.length === 1 ? 'fits now' : 'both fit now'}${plan.length > 2 ? `, ${plan.length - 2} more after` : ''} · tap to open`,
              go: () => (served ? openPile(chipOf(served), served) : select('late')) };
          } else if (lateCount) {
            const top = [...(data?.overdue ?? [])].sort((a, b) => RANK[a.priority] - RANK[b.priority])[0];
            if (top) {
              work = { key: 'late', icon: 'alert-circle-outline', label: 'Overdue', when: `${lateCount} late`, lines: [top.title],
                sub: `Due ${top.due_label}${lateCount > 1 ? ` · ${lateCount - 1} more` : ''} · I'll suggest a time when you're free`, go: () => setOpenTask(top) };
            }
          }
          let meet: Banner | null = null;
          if (next) {
            meet = { key: 'next', icon: kindIcon[next.item.kind] ?? 'time-outline', label: soon ? 'Starting now' : 'Next up', when: timeOf(next.item.scheduled_start ?? next.item.due_at),
              lines: [next.item.title], sub: `${inWords(next.start - now)}${next.item.scope === 'private' ? ' · private' : ''}${next.item.priority === 'URGENT' ? ' · urgent' : ''}`,
              go: () => setOpenTask(next.item) };
          } else if (ahead) {
            meet = { key: 'ahead', icon: kindIcon[ahead.kind] ?? 'people-outline', label: 'Next meeting', when: ahead.due_label, lines: [ahead.title],
              sub: `${ahead.kind === 'CALL' ? 'Call' : 'Meeting'}${ahead.assignee.type !== 'MD' && ahead.assignee.staff_name ? ` with ${ahead.assignee.staff_name}` : ''} · nothing timed left today`, go: () => setOpenTask(ahead) };
          }
          let spot: Banner | null = null;
          if (shown) {
            const at = pile.findIndex((p) => p.key === shown.key) + 1;
            spot = { key: `spot-${shown.key}`, icon: CHIP_ICON[chipOf(shown)], label: `${alertLabel(shown)}${shown.from ? ` · ${shown.from}` : ''}`, when: relativeAgo(shown.fireAt),
              lines: [shown.concealed ? 'Private reminder' : shown.title],
              sub: `${at} of ${pile.length} waiting${pile.length > 1 ? ' · the next in 10 s' : ''} · tap to open, or say “isko …”`, go: () => openPile(chipOf(shown), shown) };
          }
          const banners = (soon ? [meet, work, spot] : [work, meet, spot]).filter((b): b is Banner => !!b);
          if (!banners.length) {
            banners.push({ key: 'now', icon: 'sparkles-outline', label: 'Right now', when: '',
              lines: [data ? 'Nothing timed left today' : loadError ? 'Can’t reach the JARVIS server' : 'Loading your day…'],
              sub: !data && loadError ? `${loadError} Check the internet connection, then tap here to try again.` : 'Say “Hey JARVIS” and ask for the plan, or give a task.',
              go: () => (!data && loadError ? reload(true) : talk()) });
          }
          return banners.map((b) => (
            <Pressable key={b.key} onPress={b.go} style={s.heroWrap}>
              <LinearGradient colors={[...gradients.orb]} start={{ x: 0, y: 0 }} end={{ x: 1, y: 1 }} style={s.hero}>
                <View style={s.heroTop}>
                  <View style={s.heroIcon}>
                    <Icon name={b.icon} size={16} color={colors.white} />
                  </View>
                  <Txt variant="label" numberOfLines={1} style={{ color: 'rgba(255,255,255,0.85)', flexShrink: 1 }}>{b.label}</Txt>
                  {b.when ? <Txt variant="caption" style={s.heroWhen}>{b.when}</Txt> : null}
                </View>
                {b.lines.map((l, i) => (
                  <Txt key={i} variant="title" numberOfLines={1} style={{ color: colors.white, marginTop: i ? 2 : 8 }}>{l}</Txt>
                ))}
                <Txt variant="small" numberOfLines={2} style={{ color: 'rgba(255,255,255,0.85)', marginTop: 4 }}>{b.sub}</Txt>
              </LinearGradient>
            </Pressable>
          ));
        })()}

        {data && loadError ? (
          <Pressable onPress={() => reload(true)} style={s.offline}>
            <Txt variant="small" style={{ color: colors.warn }}>Not up to date: can’t reach the server. Tap to try again.</Txt>
          </Pressable>
        ) : null}

        {/* Reports (below the fold; the day itself is the chips and the banners above) */}
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
  chips: { flexDirection: 'row', justifyContent: 'space-between', marginTop: 14, paddingHorizontal: 6 },
  chip: { alignItems: 'center', width: 72 },
  chipRing: { width: 56, height: 56, borderRadius: 28, borderWidth: 2, alignItems: 'center', justifyContent: 'center', backgroundColor: colors.surface },
  chipN: { fontSize: 20, fontFamily: fonts.semibold, lineHeight: 24 },
  chipBadge: { position: 'absolute', top: -5, right: -6, minWidth: 20, height: 20, borderRadius: 10, backgroundColor: colors.danger, alignItems: 'center', justifyContent: 'center', paddingHorizontal: 5 },
  chipBadgeN: { color: colors.white, fontSize: 11, fontFamily: fonts.semibold, lineHeight: 14 },
  heroWrap: { marginTop: 18, borderRadius: radius.lg, ...shadow, shadowOpacity: 0.18, shadowColor: colors.accentDeep },
  hero: { borderRadius: radius.lg, padding: 18 },
  heroTop: { flexDirection: 'row', alignItems: 'center' },
  heroIcon: { width: 28, height: 28, borderRadius: 14, backgroundColor: 'rgba(255,255,255,0.2)', alignItems: 'center', justifyContent: 'center', marginRight: 8 },
  heroWhen: { marginLeft: 'auto', paddingLeft: 8, color: colors.white, fontFamily: fonts.semibold, fontSize: 13 },
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
