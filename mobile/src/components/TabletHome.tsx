/**
 * Tablet (or a phone turned sideways): two columns. Left, about a third: JARVIS — nav icons, the orb, the
 * conversation, the card waiting for a yes, the type box. Right, the rest: data — the selected list as a
 * timeline / table, and every answer as a card with its Download button.
 */
import { useRouter } from 'expo-router';
import React, { useEffect, useRef, useState } from 'react';
import { KeyboardAvoidingView, Linking, Platform, Pressable, RefreshControl, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { useAlertEngine } from '@/lib/alertEngine';
import { useConversation } from '@/lib/conversation';
import { greeting } from '@/lib/format';
import { useLayout } from '@/lib/layout';
import { addressOf, mdWord } from '@/lib/names';
import { presenceLabel } from '@/lib/presence';
import { colors, fonts, radius, shadow } from '@/lib/theme';
import type { SessionUser } from '@/lib/types';
import { useDashboard } from '@/lib/useDashboard';
import { useDataView } from '@/lib/useDataView';

import { DataCard, StatTiles } from './DataCard';
import { DraftCard } from './DraftCard';
import { VIEW_FOR_KEY, navItems } from './nav';
import { Orb } from './Orb';
import { TaskSheet } from './TaskSheet';
import { Icon, IconButton, Txt } from './ui';

const DOT: Record<string, string> = { present: colors.success, away: colors.faint, facedown: colors.inkSoft, meeting: colors.gold, quiet: colors.inkSoft };

export function TabletHome({ user }: { user: SessionUser }) {
  const router = useRouter();
  const conv = useConversation();
  const engine = useAlertEngine();
  const { data, reload } = useDashboard();
  const { assistantWidth, tileColumns } = useLayout();
  const [section, setSection] = useState<string>('today');
  const [typed, setTyped] = useState('');
  const scrollRef = useRef<React.ComponentRef<typeof ScrollView>>(null);
  const workRef = useRef<React.ComponentRef<typeof ScrollView>>(null);
  const sectionView = useDataView(VIEW_FOR_KEY[section] ?? null);
  // A new answer: bring the data pane back to the top, where it has just appeared.
  const newestAnswer = conv.answers[0]?.id;
  useEffect(() => {
    if (newestAnswer) workRef.current?.scrollTo({ y: 0, animated: true });
  }, [newestAnswer]);

  const isMD = user.role === 'MD';
  const address = addressOf(user);
  const md = mdWord(data);
  const presence = engine.presence.state;
  const items = navItems(user, data, engine);
  const pending = data?.requests.filter((r) => r.state === 'PENDING').length ?? 0;
  const stats = data
    ? [
        { label: 'Today', value: data.today.length, tone: 'accent' },
        { label: 'Late', value: data.overdue.length, tone: data.overdue.length ? 'danger' : 'muted' },
        { label: 'Done today', value: data.completed_today, tone: 'success' },
        { label: isMD ? 'Need your OK' : `Waiting for ${md}`, value: pending, tone: pending ? 'warn' : 'muted' },
        ...(isMD ? [{ label: `With ${data.pa_name || 'your PA'}`, value: (data.pa_tasks ?? []).filter((t) => t.status !== 'COMPLETED' && t.status !== 'CANCELLED').length, tone: 'info' }] : []),
        { label: 'With staff', value: data.delegated.filter((t) => t.status !== 'COMPLETED' && t.status !== 'CANCELLED').length, tone: 'info' },
      ]
    : [];

  const select = (key: string) => {
    if (key === 'settings') router.push('/settings');
    else if (key === 'briefing') conv.send('Give me the briefing');
    else if (key === 'held') engine.setHeldVisible(true);
    else setSection(key);
  };
  const send = () => {
    const t = typed.trim();
    if (!t) return;
    setTyped('');
    conv.send(t);
  };
  const recent = conv.messages.slice(-6);
  // A task he asked to open by voice ("pehla wala kholo").
  const voiceItem = conv.itemToOpen && data
    ? [...data.overdue, ...data.today, ...data.upcoming, ...data.anytime, ...(data.pa_tasks ?? []), ...(data.md_tasks ?? []), ...data.delegated]
        .find((t) => t.id === conv.itemToOpen) ?? null
    : null;
  const suggestions = isMD
    ? ['Aaj ka plan kya hai?', 'Meri staff ki list dikhao', `${data?.pa_name || 'PA'} ke tasks dikhao`, 'Kal 11 baje Sharma ji ke saath meeting']
    : [`${md} aaj free kab hain?`, 'Mere tasks dikhao', `${md} ke liye kal 3 baje bank meeting`];

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }}>
      <View style={{ flex: 1, flexDirection: 'row' }}>
        {/* ---------------------------------------------------------------- JARVIS column */}
        <View style={[s.assistant, { width: assistantWidth }]}>
          <View style={s.colTop}>
            <Txt style={s.brand}>JARVIS</Txt>
            <View style={s.pill}>
              <View style={[s.dot, { backgroundColor: DOT[presence] }]} />
              <Txt variant="caption" style={{ color: colors.inkSoft, fontWeight: '600' }}>{presenceLabel[presence].title}</Txt>
            </View>
          </View>
          <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={s.rail}>
            {items.map((it) => {
              const active = section === it.key && VIEW_FOR_KEY[it.key];
              return (
                <Pressable key={it.key} onPress={() => select(it.key)} style={[s.railItem, active && s.railItemActive]} accessibilityLabel={it.label}>
                  <Icon name={it.icon} size={18} color={active ? colors.white : it.tone && it.count ? it.tone : colors.inkSoft} />
                  {it.count ? (
                    <View style={[s.railCount, active && { backgroundColor: colors.white }]}>
                      <Txt variant="caption" style={{ fontSize: 10, fontWeight: '700', color: active ? colors.accent : colors.inkSoft }}>{it.count}</Txt>
                    </View>
                  ) : null}
                </Pressable>
              );
            })}
          </ScrollView>

          <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
            <ScrollView ref={scrollRef} contentContainerStyle={s.convo} keyboardShouldPersistTaps="handled" onContentSizeChange={() => scrollRef.current?.scrollToEnd({ animated: true })}>
              <View style={{ alignItems: 'center', marginTop: 6 }}>
                <Orb state={conv.orb} size={150} level={conv.level} onPress={conv.micPress} />
                <Txt variant="small" style={[{ marginTop: 6, color: colors.muted, textAlign: 'center' }, conv.listening && { color: colors.accent }]}>{conv.status}</Txt>
                {conv.listening && conv.partial ? <Txt variant="heading" style={{ textAlign: 'center', marginTop: 6 }}>{conv.partial}</Txt> : null}
              </View>
              {!recent.length && !conv.listening ? (
                <View style={{ marginTop: 16, rowGap: 8 }}>
                  {suggestions.map((q) => (
                    <Pressable key={q} onPress={() => conv.send(q)} style={({ pressed }) => [s.suggestion, pressed && { opacity: 0.7 }]}>
                      <Icon name="sparkles-outline" size={13} color={colors.accent} />
                      <Txt variant="small" style={{ color: colors.text, marginLeft: 8, flex: 1 }}>{q}</Txt>
                    </Pressable>
                  ))}
                </View>
              ) : null}
              {recent.map((m) =>
                m.role === 'user' ? (
                  <View key={m.id} style={s.userRow}>
                    <View style={s.userBubble}><Txt variant="body" style={{ fontSize: 15 }}>{m.text}</Txt></View>
                  </View>
                ) : (
                  <View key={m.id} style={s.jarvisRow}>
                    <View style={[s.avatar, m.tone === 'error' && { backgroundColor: colors.dangerSoft }, m.tone === 'saved' && { backgroundColor: colors.successSoft }]}>
                      <Icon name={m.tone === 'error' ? 'alert' : m.tone === 'saved' ? 'checkmark' : 'sparkles'} size={12} color={m.tone === 'error' ? colors.danger : m.tone === 'saved' ? colors.success : colors.accent} />
                    </View>
                    <View style={{ flex: 1 }}>
                      <Txt variant="body" style={[{ fontSize: 15, lineHeight: 22 }, m.tone === 'error' && { color: colors.danger }]}>{m.text}</Txt>
                      {m.sources?.length ? (
                        <View style={s.sources}>
                          {m.sources.map((src) => (
                            <Pressable key={src.url} onPress={() => Linking.openURL(src.url)} style={s.source}>
                              <Icon name="link-outline" size={11} color={colors.muted} />
                              <Txt variant="caption" numberOfLines={1} style={{ marginLeft: 4, maxWidth: 160 }}>{src.title}</Txt>
                            </Pressable>
                          ))}
                        </View>
                      ) : null}
                    </View>
                  </View>
                ),
              )}
              {conv.draft ? (
                <DraftCard draft={conv.draft} saving={conv.busy === 'saving'} listening={conv.listening} error={conv.draftError} onChange={conv.editDraft} onSave={conv.save} onCancel={conv.cancelDraft} onReply={conv.send} />
              ) : null}
              {conv.messages.length > 6 ? (
                <Pressable onPress={() => router.push('/talk')} style={{ flexDirection: 'row', alignItems: 'center', marginTop: 10 }}>
                  <Icon name="chatbubbles-outline" size={14} color={colors.inkSoft} />
                  <Txt variant="caption" style={{ marginLeft: 6, color: colors.inkSoft }}>Whole conversation</Txt>
                </Pressable>
              ) : null}
            </ScrollView>
            <View style={s.composer}>
              <View style={s.inputWrap}>
                <TextInput value={typed} onChangeText={setTyped} placeholder="Type to JARVIS…" placeholderTextColor={colors.faint} style={s.input} onSubmitEditing={send} returnKeyType="send" />
                {typed.trim() ? (
                  <Pressable onPress={send} style={s.send} accessibilityLabel="Send"><Icon name="arrow-up" size={18} color={colors.white} /></Pressable>
                ) : null}
              </View>
              <Pressable onPress={conv.micPress} style={({ pressed }) => [s.mic, conv.listening && { backgroundColor: colors.accent }, pressed && { transform: [{ scale: 0.95 }] }]} accessibilityLabel={conv.listening ? 'Stop listening' : 'Speak to JARVIS'}>
                <Icon name={conv.listening || conv.speaking ? 'stop' : 'mic-outline'} size={22} color={colors.white} />
              </Pressable>
            </View>
          </KeyboardAvoidingView>
        </View>

        {/* ---------------------------------------------------------------- workspace */}
        <ScrollView
          ref={workRef}
          style={{ flex: 1 }}
          contentContainerStyle={s.work}
          refreshControl={<RefreshControl refreshing={false} onRefresh={() => reload(true)} tintColor={colors.accent} />}
        >
          <View style={s.workTop}>
            <View style={{ flex: 1 }}>
              <Txt variant="small" style={{ color: colors.muted }}>{data?.date_label ?? ''}</Txt>
              <Txt variant="display">{greeting()}, {address}</Txt>
            </View>
            <IconButton icon="settings-outline" label="Settings" onPress={() => router.push('/settings')} />
          </View>
          <StatTiles stats={stats} columns={Math.min(tileColumns, stats.length || 1)} />

          {/* What he just asked for comes first, right under the numbers: never below a long list. */}
          {conv.answers.length ? (
            <View style={{ marginTop: 8 }}>
              <View style={s.answersHead}>
                <Txt variant="label">Answers</Txt>
                <Pressable onPress={conv.clearAnswers} hitSlop={8}><Txt variant="caption" style={{ color: colors.muted }}>Clear all</Txt></Pressable>
              </View>
              {conv.answers.map((a) => (
                <View key={a.id}>
                  <Txt variant="caption" style={{ marginBottom: 6 }}>“{a.question}”</Txt>
                  <DataCard view={a.view} extraText={a.text} onRemove={() => conv.removeAnswer(a.id)} />
                </View>
              ))}
            </View>
          ) : null}

          {sectionView.view ? (
            <View style={{ marginTop: 18 }}>
              <Txt variant="label" style={{ marginBottom: 8 }}>{items.find((i) => i.key === section)?.label ?? 'List'}</Txt>
              <DataCard view={sectionView.view} />
            </View>
          ) : sectionView.loading ? (
            <Txt variant="small" style={{ marginTop: 18 }}>Loading…</Txt>
          ) : null}
        </ScrollView>
      </View>
      {voiceItem ? <TaskSheet item={voiceItem} onClose={conv.clearItemToOpen} /> : null}
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  assistant: { borderRightWidth: StyleSheet.hairlineWidth, borderRightColor: colors.line, backgroundColor: colors.surface },
  colTop: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingHorizontal: 16, paddingTop: 10 },
  brand: { fontFamily: fonts.serif, fontSize: 17, fontWeight: '700', lineHeight: 24, letterSpacing: 2, color: colors.ink },
  pill: { flexDirection: 'row', alignItems: 'center', backgroundColor: colors.bg, borderWidth: 1, borderColor: colors.line, borderRadius: 999, paddingHorizontal: 10, paddingVertical: 5 },
  dot: { width: 8, height: 8, borderRadius: 4, marginRight: 7 },
  rail: { paddingHorizontal: 12, paddingVertical: 10, columnGap: 6 },
  railItem: { width: 40, height: 40, borderRadius: 12, alignItems: 'center', justifyContent: 'center', backgroundColor: colors.surfaceAlt },
  railItemActive: { backgroundColor: colors.accent },
  railCount: { position: 'absolute', top: -4, right: -4, minWidth: 16, height: 16, borderRadius: 8, paddingHorizontal: 3, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, alignItems: 'center', justifyContent: 'center' },
  convo: { paddingHorizontal: 14, paddingBottom: 14 },
  suggestion: { flexDirection: 'row', alignItems: 'center', backgroundColor: colors.bg, borderWidth: 1, borderColor: colors.line, borderRadius: radius.md, paddingVertical: 10, paddingHorizontal: 12 },
  userRow: { flexDirection: 'row', justifyContent: 'flex-end', marginTop: 12 },
  userBubble: { maxWidth: '88%', backgroundColor: colors.accentSoft, borderRadius: 16, borderBottomRightRadius: 5, paddingVertical: 8, paddingHorizontal: 12 },
  jarvisRow: { flexDirection: 'row', alignItems: 'flex-start', marginTop: 12 },
  avatar: { width: 24, height: 24, borderRadius: 12, backgroundColor: colors.accentSoft, alignItems: 'center', justifyContent: 'center', marginRight: 8, marginTop: 1 },
  sources: { flexDirection: 'row', flexWrap: 'wrap', marginTop: 6, columnGap: 6, rowGap: 6 },
  source: { flexDirection: 'row', alignItems: 'center', backgroundColor: colors.surfaceAlt, borderRadius: 999, paddingHorizontal: 8, paddingVertical: 3 },
  composer: { flexDirection: 'row', alignItems: 'center', padding: 12, borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: colors.line },
  inputWrap: { flex: 1, flexDirection: 'row', alignItems: 'center', backgroundColor: colors.bg, borderWidth: 1, borderColor: colors.line, borderRadius: 24, paddingLeft: 14, paddingRight: 5, minHeight: 44, marginRight: 8 },
  input: { flex: 1, fontSize: 15, color: colors.text, paddingVertical: 8 },
  send: { width: 32, height: 32, borderRadius: 16, backgroundColor: colors.ink, alignItems: 'center', justifyContent: 'center' },
  mic: { width: 44, height: 44, borderRadius: 22, backgroundColor: colors.ink, alignItems: 'center', justifyContent: 'center' },
  work: { padding: 24, paddingBottom: 60 },
  workTop: { flexDirection: 'row', alignItems: 'center', marginBottom: 4 },
  answersHead: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginTop: 14, marginBottom: 8 },
  card: { backgroundColor: colors.surface, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line, padding: 16, ...shadow },
});
