/**
 * Phone: the home is the day (his layout, 25 Nov 2026). Greeting and date; one line of today's real numbers
 * (meetings, tasks, late), each opening its list; the Now card (a meeting about to start, else the free time with the
 * work that fits it, else what is next by the clock); the Waiting card (what is waiting for him, counted by kind,
 * with the latest one); then, below the fold, the reports and the latest answer. Staff and the company panel live in
 * the side menu. The orb floats in the bottom-right corner and opens the talk layer.
 */
import * as Haptics from 'expo-haptics';
import { LinearGradient } from 'expo-linear-gradient';
import { useRouter } from 'expo-router';
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { type GestureResponderEvent, PanResponder, type PanResponderGestureState, type PanResponderInstance, Pressable, RefreshControl, ScrollView, StyleSheet, View } from 'react-native';
import { SafeAreaView, useSafeAreaInsets } from 'react-native-safe-area-context';

import { type AlertGroup, describeWaiting, type EngineItem, GROUP_ORDER, GROUP_WORDS, groupOf, pileOf, useAlertEngine } from '@/lib/alertEngine';
import { unlock } from '@/lib/appLock';
import { useConversation } from '@/lib/conversation';
import { greeting, relativeAgo, timeOf } from '@/lib/format';
import { twin } from '@/lib/lang';
import { addressHiOf, addressOf, mdWord } from '@/lib/names';
import { presenceLabel } from '@/lib/presence';
import { say } from '@/lib/speech';
import { colors, fonts, gradients, kindIcon, radius, shadow } from '@/lib/theme';
import type { Kind, Priority, SessionUser, TaskItem } from '@/lib/types';
import { useDashboard } from '@/lib/useDashboard';
import { useWalkie } from '@/lib/walkie';

import { AssistantStage, CAN_BLUR } from './AssistantStage';
import { DataCard } from './DataCard';
import { Drawer } from './Drawer';
import { navItems } from './nav';
import { Orb } from './Orb';
import { TaskSheet } from './TaskSheet';
import { Icon, IconButton, Txt } from './ui';

const DOT: Record<string, string> = { present: colors.success, away: colors.faint, facedown: colors.inkSoft, meeting: colors.gold, quiet: colors.inkSoft };
const ORB_CORNER = 84; // the small orb in the corner (its halo makes it look a little smaller than this)

/** What is ahead by the clock: everything with a time, plus meetings and calls that have only a day (counted at the
 *  end of that day), earliest first. The Next up banner shows the first and swipes through the rest. A task with
 *  only a date is not "next up": it belongs to the day, not to a moment. */
function timedAhead(items: TaskItem[], now: number): { item: TaskItem; start: number }[] {
  const meeting = (t: TaskItem) => t.kind === 'MEETING' || t.kind === 'CALL';
  return items
    .filter((t) => t.status !== 'COMPLETED' && t.status !== 'CANCELLED' && (t.scheduled_start || t.due_at || (meeting(t) && t.due_date)))
    .map((t) => ({ item: t, start: Date.parse(t.scheduled_start ?? t.due_at ?? `${t.due_date}T23:59:59`) }))
    .filter((x) => !Number.isNaN(x.start) && x.start >= now - 5 * 60_000)
    .sort((a, b) => a.start - b.start);
}

/** A banner that moves by hand: a sideways drag (taken from the tap inside it, kept from the page's scroll). */
function makeSwipe(onDir: (dir: 1 | -1) => void): PanResponderInstance {
  const sideways = (_e: GestureResponderEvent, g: PanResponderGestureState) => Math.abs(g.dx) > 14 && Math.abs(g.dx) > Math.abs(g.dy) * 1.5;
  return PanResponder.create({
    onMoveShouldSetPanResponder: sideways,
    onMoveShouldSetPanResponderCapture: sideways,
    onPanResponderTerminationRequest: () => false,
    onPanResponderRelease: (_e, g) => {
      if (Math.abs(g.dx) >= 30) onDir(g.dx < 0 ? 1 : -1);
    },
  });
}

const inWords = (ms: number) => {
  const m = Math.round(ms / 60_000);
  if (m <= 0) return 'now';
  if (m < 60) return `in ${m} min`;
  const h = Math.floor(m / 60);
  return `in ${h} h${m % 60 ? ` ${m % 60} min` : ''}`;
};
const inWordsHi = (ms: number) => {
  const m = Math.round(ms / 60_000);
  if (m <= 0) return 'अभी';
  if (m < 60) return `${m} मिनट में`;
  const h = Math.floor(m / 60);
  return `${h} घंटे${m % 60 ? ` ${m % 60} मिनट` : ''} में`;
};
const localIso = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

/** The Now card (his home screen, 25 Nov 2026): what it is, when, one or two lines; a tap opens it, the speaker icon
 *  makes JARVIS say it; a button when there is one thing to do about it. Information only, never "say this". */
type Banner = { key: string; icon: string; label: string; when: string; lines: string[]; sub: string; go: () => void; speak: () => void;
  swipe?: PanResponderInstance; action?: { label: string; icon: string; onPress: () => void } };
/** Work that can be fitted into free time (never a meeting or a call) and its usual minutes when none is estimated. */
const USUAL: Partial<Record<Kind, number>> = { TASK: 30, PAYMENT: 10, FOLLOW_UP: 15 };
const RANK: Record<Priority, number> = { URGENT: 0, HIGH: 1, NORMAL: 2, LOW: 3 };

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
  const addressHi = addressHiOf(user);
  // One app-built line, spoken in the language JARVIS is using with him (the speaker icon on a banner).
  const speakTwin = (en: string, hi: string) => {
    const t = twin(en, hi);
    say(t.text, { interrupt: true, lang: t.lang, fallback: t.fallback });
  };
  const paName = data?.pa_name || 'your PA';
  const md = mdWord(data);
  const presence = engine.presence.state;
  const items = navItems(user, data, engine);
  // "Need your OK": requests waiting, and approvals asked of him (open, on his list).
  const approvals = data
    ? [...data.overdue, ...data.today, ...data.upcoming, ...data.anytime].filter((t) => t.kind === 'APPROVAL' && t.status !== 'COMPLETED' && t.status !== 'CANCELLED')
    : [];
  const pending = (data?.requests.filter((r) => r.state === 'PENDING').length ?? 0) + approvals.length;
  const ahead = data ? timedAhead([...data.overdue, ...data.today, ...data.upcoming], now) : [];
  const next = ahead[0] ?? null;
  // The Next up banner swipes through what is ahead; the index comes back to the first when the list changes.
  const [meetAt, setMeetAt] = useState(0);
  const meetSwipe = useMemo(() => makeSwipe((dir) => setMeetAt((n) => n + dir)), []);
  const shownMeet = ahead.length ? ahead[((meetAt % ahead.length) + ahead.length) % ahead.length] : null;
  const meetIdx = shownMeet ? ahead.indexOf(shownMeet) : 0;
  const midnight = (() => {
    const d = new Date(now);
    d.setHours(24, 0, 0, 0);
    return d.getTime();
  })();
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
  // Today's real numbers: meetings and tasks due today, and what is late from before (a number means what the word
  // says; what is waiting for him is the Waiting card, not these).
  const closed = (t: TaskItem) => t.status === 'COMPLETED' || t.status === 'CANCELLED';
  const isMeet = (t: TaskItem) => t.kind === 'MEETING' || t.kind === 'CALL';
  const lateCount = data?.overdue.length ?? 0;
  const todayMeetings = data ? data.today.filter((t) => !closed(t) && isMeet(t)).length : 0;
  const todayTasks = data ? data.today.filter((t) => !closed(t) && !isMeet(t)).length : 0;
  // What is waiting for him: one pile, the latest first (the same rows the sheet shows). The held items count too.
  const pile = pileOf(engine.waiting, engine.held);
  const openPile = (g: AlertGroup | 'other' | null, item?: EngineItem) => {
    if (item && engine.waiting.some((w) => w.key === item.key)) engine.focus(item);
    engine.setAllGroup(g);
    engine.setAllVisible(true);
  };
  // The latest waiting item is the one on the Waiting card: the engine's spotlight, so "isko hata do" / "this one,
  // later" by voice means it.
  const latest = pile[0] ?? null;
  const setSpotlight = engine.setSpotlight;
  useEffect(() => {
    setSpotlight(latest);
  }, [setSpotlight, latest]);
  useEffect(() => () => setSpotlight(null), [setSpotlight]);
  // Free time analysed into named work (his ask): the server's suggestion when it is waiting (a yes places it), else
  // the same fit done here: late first, then due today without a time, then undated; urgent first; each takes its
  // estimated minutes (the kind's usual when none, two hours at most) until the stretch is used.
  const served = pile.find((i) => i.question?.kind === 'place') ?? null;
  const plan: { title: string; minutes: number }[] = (() => {
    if (!data) return [];
    if (served?.question?.plan?.length) return served.question.plan.map((p) => ({ title: p.title, minutes: p.minutes }));
    if (!freeTill) return [];
    const end = freeTill.until ? Date.parse(freeTill.until) : midnight; // nothing timed ahead today: the day runs to midnight
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

        {/* Today's real numbers (his ask, 25 Nov 2026: a number means what its word says): meetings and tasks due
            today, and what is late from before. A tap opens that list. */}
        <View style={s.today}>
          <Txt variant="label" style={{ marginRight: 10 }}>Today</Txt>
          {[
            { n: todayMeetings, word: todayMeetings === 1 ? 'meeting' : 'meetings', list: 'today_meetings', tone: colors.ink },
            { n: todayTasks, word: todayTasks === 1 ? 'task' : 'tasks', list: 'today_tasks', tone: colors.ink },
            { n: lateCount, word: 'late', list: 'late', tone: lateCount ? colors.danger : colors.muted },
          ].map((c, i) => (
            <React.Fragment key={c.list}>
              {i ? <Txt style={s.todayDot}>·</Txt> : null}
              <Pressable onPress={() => { tick(); select(c.list); }} style={s.todayItem} accessibilityLabel={`${c.n} ${c.word} today`}>
                <Txt style={[s.todayN, { color: c.tone }]}>{c.n}</Txt>
                <Txt variant="small" style={{ color: c.tone === colors.muted ? colors.muted : colors.inkSoft, marginLeft: 5 }}>{c.word}</Txt>
              </Pressable>
            </React.Fragment>
          ))}
        </View>

        {/* The Now card, one card that changes with the moment: a meeting about to start; else the free time with
            the work that fits it (a button puts it in when the server has planned it); else what is next by the
            clock (a swipe shows what follows); else the day is done. */}
        {(() => {
          const soon = !!next && next.start - now <= 5 * 60_000;
          let meet: Banner | null = null;
          if (shownMeet) {
            // Today's item by the clock; another day's by its day. The first is "Next"; a swipe shows what follows.
            const m = shownMeet.item;
            const isToday = m.due_date ? m.due_date === localIso(new Date(now)) : shownMeet.start < midnight;
            const at = isToday && (m.scheduled_start ?? m.due_at) ? timeOf(m.scheduled_start ?? m.due_at) : m.due_label;
            const first = meetIdx === 0;
            const who = m.assignee.type !== 'MD' && m.assignee.staff_name ? ` with ${m.assignee.staff_name}` : '';
            const kindWord = m.kind === 'CALL' ? 'Call' : m.kind === 'MEETING' ? 'Meeting' : 'Task';
            const kindHi = m.kind === 'CALL' ? 'कॉल' : m.kind === 'MEETING' ? 'मीटिंग' : 'काम';
            meet = { key: `meet-${m.scope}-${m.id}`, icon: kindIcon[m.kind] ?? 'time-outline',
              label: first ? (soon ? 'Starting now' : 'Next') : `After that · ${meetIdx + 1} of ${ahead.length}`, when: at,
              lines: [m.title],
              sub: `${isToday ? inWords(shownMeet.start - now) : `${kindWord}${who}`}${m.scope === 'private' ? ' · private' : ''}${m.priority === 'URGENT' ? ' · urgent' : ''}`,
              go: () => setOpenTask(m), swipe: meetSwipe,
              speak: () => speakTwin(
                `${address}, ${first ? (soon ? 'starting now' : 'next') : 'after that'}: ${m.title}, ${at}${isToday ? `, ${inWords(shownMeet.start - now)}` : ''}.`,
                `${addressHi}, ${first ? (soon ? 'अभी शुरू हो रहा है' : 'अगला') : 'उसके बाद'}: ${m.title}, ${at}${isToday ? ` बजे, ${inWordsHi(shownMeet.start - now)}` : ''}${!isToday ? ` (${kindHi})` : ''}।`) };
          }
          let free: Banner | null = null;
          if (plan.length) {
            const two = plan.slice(0, 2);
            const till = freeUntil ? `till ${timeOf(freeUntil)}` : 'till midnight';
            const tillHi = freeUntil ? `${timeOf(freeUntil)} तक` : 'रात 12 बजे तक';
            free = { key: 'free', icon: 'sparkles-outline', label: `Free ${till}`, when: '',
              lines: two.map((p) => p.title),
              sub: `${two.length === 1 ? 'This fits now' : 'These fit now'} · ${two.map((p) => p.minutes).join(' + ')} min${plan.length > 2 ? ` · ${plan.length - 2} more after` : ''}`,
              go: () => (served ? openPile(null, served) : select('late')),
              speak: () => (served ? engine.readItems([served]) : speakTwin(
                `${address}, you're free ${till}. ${two.length === 1 ? 'One thing fits now' : 'Two things fit now'}: ${two.map((p) => p.title).join(' and ')}.`,
                `${addressHi}, आप ${tillHi} फ़्री हैं। ${two.length === 1 ? 'एक काम' : 'दो काम'} अभी हो सकते हैं: ${two.map((p) => p.title).join(' और ')}।`)),
              action: served?.question ? { label: 'Put them in', icon: 'calendar-outline',
                onPress: () => engine.answerQuestion(served, served.question!, true).catch(() => undefined) } : undefined };
          }
          let card: Banner | null = soon ? meet : (free ?? meet);
          if (!card) {
            const line = data ? 'Nothing timed left today' : loadError ? 'Can’t reach the JARVIS server' : 'Loading your day…';
            card = { key: 'now', icon: 'sparkles-outline', label: 'Now', when: '', lines: [line], sub: !data && loadError ? loadError : '',
              go: () => (!data && loadError ? reload(true) : talk()),
              speak: () => speakTwin(`${address}, ${data ? 'nothing timed is left today.' : 'I can’t reach the server right now.'}`,
                `${addressHi}, ${data ? 'आज कुछ टाइम वाला बचा नहीं है।' : 'अभी सर्वर से बात नहीं हो पा रही।'}`) };
          }
          const b = card;
          return (
            <View key={b.key} {...(b.swipe ? b.swipe.panHandlers : {})}>
              <Pressable onPress={b.go} style={s.heroWrap}>
                <LinearGradient colors={[...gradients.orb]} start={{ x: 0, y: 0 }} end={{ x: 1, y: 1 }} style={s.hero}>
                  <View style={s.heroTop}>
                    <View style={s.heroIcon}>
                      <Icon name={b.icon} size={16} color={colors.white} />
                    </View>
                    <Txt variant="label" numberOfLines={1} style={{ color: 'rgba(255,255,255,0.85)', flexShrink: 1 }}>{b.label}</Txt>
                    {b.when ? <Txt variant="caption" style={s.heroWhen}>{b.when}</Txt> : null}
                    <Pressable onPress={() => { tick(); b.speak(); }} hitSlop={10} style={[s.heroSpeak, !b.when && { marginLeft: 'auto' }]} accessibilityLabel="Say it">
                      <Icon name="volume-high-outline" size={15} color={colors.white} />
                    </Pressable>
                  </View>
                  {b.lines.map((l, i) => (
                    <Txt key={i} variant="title" numberOfLines={1} style={{ color: colors.white, marginTop: i ? 2 : 8 }}>{l}</Txt>
                  ))}
                  {b.sub ? <Txt variant="small" numberOfLines={2} style={{ color: 'rgba(255,255,255,0.85)', marginTop: 4 }}>{b.sub}</Txt> : null}
                  {b.action ? (
                    <Pressable onPress={() => { tick(); b.action!.onPress(); }} style={s.heroBtn} accessibilityLabel={b.action.label}>
                      <Icon name={b.action.icon} size={14} color={colors.white} />
                      <Txt variant="small" style={{ color: colors.white, marginLeft: 6, fontWeight: '600' }}>{b.action.label}</Txt>
                    </Pressable>
                  ) : null}
                </LinearGradient>
              </Pressable>
            </View>
          );
        })()}

        {/* The Waiting card: the reminders and messages that came and are not done yet, counted by kind, and the
            latest one. It says what it is (his ask, 26 Nov 2026: "are these meetings upcoming or late? what is
            this?"): meeting reminders are split into passed and upcoming by the meeting's time, task reminders
            are told apart from new tasks given to him. A tap opens the pile; the speaker icon says the counts,
            never a title (his rule). */}
        {(() => {
          const byId = new Map<string, TaskItem>();
          if (data) for (const t of [...data.overdue, ...data.today, ...data.upcoming, ...data.anytime]) byId.set(t.id, t);
          const startOf = (it: EngineItem): number | null => {
            const t = it.ref?.id ? byId.get(it.ref.id) : undefined;
            const iso = t?.scheduled_start ?? t?.due_at ?? (t?.due_date ? `${t.due_date}T23:59:59` : null);
            const ms = iso ? Date.parse(iso) : NaN;
            return Number.isNaN(ms) ? null : ms;
          };
          const piles = new Map<AlertGroup, EngineItem[]>();
          for (const it of pile) piles.set(groupOf(it), [...(piles.get(groupOf(it)) ?? []), it]);
          const counts: string[] = [];
          for (const g of GROUP_ORDER) {
            const items = piles.get(g) ?? [];
            const n = items.length;
            if (!n) continue;
            if (g === 'meeting') {
              const known = items.map(startOf).filter((ms): ms is number => ms !== null);
              const passed = known.filter((ms) => ms < now).length;
              const detail = known.length ? ` (${passed} passed, ${known.length - passed} upcoming${known.length < n ? `, ${n - known.length} other` : ''})` : '';
              counts.push(`${n} meeting ${n === 1 ? 'reminder' : 'reminders'}${detail}`);
              continue;
            }
            if (g === 'task') {
              const reminders = items.filter((it) => it.source === 'reminder').length;
              const fresh = n - reminders;
              if (reminders) counts.push(`${reminders} task ${reminders === 1 ? 'reminder' : 'reminders'}`);
              if (fresh) counts.push(`${fresh} new ${fresh === 1 ? 'task' : 'tasks'}`);
              continue;
            }
            const w = GROUP_WORDS[g];
            counts.push(`${n} ${n === 1 ? w.en[0] : w.en[1]}`);
          }
          const said = describeWaiting(pile, address, addressHi);
          return (
            <Pressable onPress={() => { tick(); if (pile.length) openPile(null, latest ?? undefined); }} style={s.card} accessibilityLabel="Came for you, not done yet">
              <View style={s.cardHead}>
                <Icon name="notifications-outline" size={16} color={colors.accent} />
                <Txt variant="label" style={{ marginLeft: 8, flex: 1 }}>Came for you · not done yet</Txt>
                <Pressable onPress={() => { tick(); speakTwin(said.en, said.hi); }} hitSlop={10} style={s.cardSpeak} accessibilityLabel="Say it">
                  <Icon name="volume-high-outline" size={15} color={colors.accent} />
                </Pressable>
              </View>
              {pile.length ? (
                <>
                  <Txt variant="title" numberOfLines={3} style={{ marginTop: 8 }}>{counts.join(' · ')}</Txt>
                  {latest ? (
                    <Txt variant="small" numberOfLines={1} style={{ color: colors.inkSoft, marginTop: 4 }}>
                      Latest: {latest.concealed ? 'Private reminder' : latest.title} · {relativeAgo(latest.fireAt)}
                    </Txt>
                  ) : null}
                  <Txt variant="caption" style={{ marginTop: 6 }}>Reminders and messages that reached you and are still open. Tap to go through them; today’s own meetings and tasks are in the Today line above.</Txt>
                </>
              ) : (
                <Txt variant="small" style={{ color: colors.muted, marginTop: 8 }}>Nothing new has come. Today’s meetings and tasks are in the Today line above.</Txt>
              )}
            </Pressable>
          );
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
  today: { flexDirection: 'row', alignItems: 'center', marginTop: 14 },
  todayItem: { flexDirection: 'row', alignItems: 'baseline', paddingVertical: 4 },
  todayN: { fontSize: 22, fontFamily: fonts.semibold, lineHeight: 26 },
  todayDot: { color: colors.faint, marginHorizontal: 10, fontSize: 18 },
  card: { marginTop: 14, backgroundColor: colors.surface, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line, padding: 16, ...shadow },
  cardHead: { flexDirection: 'row', alignItems: 'center' },
  cardSpeak: { width: 28, height: 28, borderRadius: 14, backgroundColor: colors.accentSoft, alignItems: 'center', justifyContent: 'center' },
  heroBtn: { alignSelf: 'flex-start', marginTop: 10, paddingHorizontal: 12, paddingVertical: 7, borderRadius: 999, backgroundColor: 'rgba(255,255,255,0.22)', flexDirection: 'row', alignItems: 'center' },
  heroWrap: { marginTop: 14, borderRadius: radius.lg, ...shadow, shadowOpacity: 0.18, shadowColor: colors.accentDeep },
  hero: { borderRadius: radius.lg, padding: 18 },
  heroTop: { flexDirection: 'row', alignItems: 'center' },
  heroIcon: { width: 28, height: 28, borderRadius: 14, backgroundColor: 'rgba(255,255,255,0.2)', alignItems: 'center', justifyContent: 'center', marginRight: 8 },
  heroWhen: { marginLeft: 'auto', paddingLeft: 8, color: colors.white, fontFamily: fonts.semibold, fontSize: 13 },
  heroSpeak: { width: 28, height: 28, borderRadius: 14, backgroundColor: 'rgba(255,255,255,0.22)', alignItems: 'center', justifyContent: 'center', marginLeft: 10 },
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
