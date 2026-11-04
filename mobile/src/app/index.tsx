import { Redirect, useRouter } from 'expo-router';
import React, { useState } from 'react';
import { KeyboardAvoidingView, Linking, Platform, Pressable, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import { SafeAreaView, useSafeAreaInsets } from 'react-native-safe-area-context';

import { DraftCard } from '@/components/DraftCard';
import { Drawer, type DrawerItem } from '@/components/Drawer';
import { Orb } from '@/components/Orb';
import { Icon, IconButton, Txt } from '@/components/ui';
import { useAlertEngine } from '@/lib/alertEngine';
import { useConversation } from '@/lib/conversation';
import { greeting } from '@/lib/format';
import { presenceLabel } from '@/lib/presence';
import { useSession } from '@/lib/session';
import { colors, fonts, radius } from '@/lib/theme';
import type { SessionUser } from '@/lib/types';
import { useDashboard } from '@/lib/useDashboard';

const DOT: Record<string, string> = {
  present: colors.success,
  away: colors.faint,
  facedown: colors.inkSoft,
  meeting: colors.gold,
  quiet: colors.inkSoft,
};

export default function Home() {
  const { user, ready } = useSession();
  if (!ready) return null;
  if (!user) return <Redirect href="/login" />;
  return <Assistant user={user} />;
}

function Assistant({ user }: { user: SessionUser }) {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const conv = useConversation();
  const engine = useAlertEngine();
  const { data } = useDashboard();
  const [menu, setMenu] = useState(false);
  const [typing, setTyping] = useState(false);
  const [typed, setTyped] = useState('');

  const isMD = user.role === 'MD';
  const address = isMD ? 'Sir' : user.name;
  const open = (items: { status: string }[] | undefined) => (items ?? []).filter((t) => t.status !== 'COMPLETED' && t.status !== 'CANCELLED').length;
  const pending = data?.requests.filter((r) => r.state === 'PENDING').length ?? 0;
  const privateCount = data ? [...data.overdue, ...data.today, ...data.upcoming, ...data.anytime].filter((t) => t.scope === 'private').length : 0;
  const heldCount = engine.held.length;
  const presence = engine.presence.state;

  const items: DrawerItem[] = [
    { key: 'today', label: 'Today', icon: 'today-outline', count: data?.today.length },
    { key: 'late', label: 'Late', icon: 'alert-circle-outline', count: data?.overdue.length, tone: colors.danger },
    { key: 'upcoming', label: 'Coming up', icon: 'calendar-outline', count: data?.upcoming.length },
    { key: 'nodate', label: 'No date yet', icon: 'infinite-outline', count: data?.anytime.length },
    ...(isMD
      ? [
          { key: 'private', label: 'Private reminders', icon: 'lock-closed-outline', count: privateCount, detail: 'Only you can see these' },
          { key: 'requests', label: 'Need your OK', icon: 'hand-left-outline', count: pending, tone: colors.gold },
          { key: 'pa', label: 'Given to your PA', icon: 'person-outline', count: open(data?.pa_tasks) },
        ]
      : [
          { key: 'requests', label: 'Waiting for Sir', icon: 'hand-left-outline', count: pending, tone: colors.gold },
          { key: 'md_tasks', label: 'Sir’s tasks from you', icon: 'person-outline', count: open(data?.md_tasks) },
          { key: 'busy', label: 'When Sir is busy', icon: 'time-outline' },
        ]),
    { key: 'staff', label: 'Given to staff', icon: 'briefcase-outline', count: open(data?.delegated) },
    { key: 'held', label: 'Held alerts', icon: 'notifications-outline', count: heldCount, tone: colors.danger, detail: 'Kept quiet while you were busy' },
    { key: 'briefing', label: 'Briefing', icon: 'sunny-outline' },
    { key: 'settings', label: 'Settings', icon: 'settings-outline' },
  ];

  const select = (key: string) => {
    setMenu(false);
    if (key === 'settings') router.push('/settings');
    else if (key === 'briefing') router.push('/briefing');
    else if (key === 'held') engine.setHeldVisible(true);
    else router.push({ pathname: '/lists/[section]', params: { section: key } });
  };

  const sendTyped = () => {
    const t = typed.trim();
    if (!t) return;
    setTyped('');
    conv.send(t);
  };

  const recent = conv.messages.slice(-2);
  const suggestions = isMD
    ? ['Aaj mera din kaisa hai?', 'Kal 11 baje Sharma ji ke saath meeting', 'Mansi ko bolo hotel book kare']
    : ['Sir ke liye kal 3 baje bank meeting', 'Ramesh ko bolo report bheje', 'Aaj Sir free kab hain?'];

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }} edges={['top']}>
      <View style={s.top}>
        <View>
          <IconButton icon="menu-outline" label="Menu" onPress={() => setMenu(true)} />
          {pending + heldCount > 0 ? <View style={s.badge} /> : null}
        </View>
        <View style={s.pill}>
          <View style={[s.dot, { backgroundColor: DOT[presence] }]} />
          <Txt variant="caption" style={{ color: colors.inkSoft, fontWeight: '600' }}>{presenceLabel[presence].title}</Txt>
        </View>
        <IconButton icon="settings-outline" label="Settings" onPress={() => router.push('/settings')} />
      </View>

      <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
        <ScrollView contentContainerStyle={s.body} keyboardShouldPersistTaps="handled">
          <Txt variant="small" style={{ color: colors.muted }}>{greeting()}, {address}</Txt>
          <View style={s.orbWrap}>
            <Orb state={conv.orb} onPress={conv.micPress} />
          </View>
          <Txt variant="small" style={[s.status, conv.listening && { color: colors.gold }]}>{conv.status}</Txt>
          {conv.listening && conv.partial ? (
            <Txt variant="title" style={s.partial}>{conv.partial}</Txt>
          ) : null}

          {!recent.length && !conv.listening ? (
            <View style={s.suggestions}>
              {suggestions.map((q) => (
                <Pressable key={q} onPress={() => conv.send(q)} style={({ pressed }) => [s.suggestion, pressed && { opacity: 0.7 }]}>
                  <Txt variant="small" style={{ color: colors.text }}>“{q}”</Txt>
                </Pressable>
              ))}
            </View>
          ) : null}

          {recent.map((m) =>
            m.role === 'user' ? (
              <View key={m.id} style={s.userRow}>
                <View style={s.userBubble}>
                  <Txt variant="body">{m.text}</Txt>
                </View>
              </View>
            ) : (
              <View key={m.id} style={s.jarvisRow}>
                <View style={[s.avatar, m.tone === 'error' && { backgroundColor: colors.dangerSoft }, m.tone === 'saved' && { backgroundColor: colors.successSoft }]}>
                  <Icon
                    name={m.tone === 'error' ? 'alert' : m.tone === 'saved' ? 'checkmark' : 'sparkles'}
                    size={13}
                    color={m.tone === 'error' ? colors.danger : m.tone === 'saved' ? colors.success : colors.gold}
                  />
                </View>
                <View style={{ flex: 1 }}>
                  <Txt variant="body" style={[s.jarvisText, m.tone === 'error' && { color: colors.danger }]}>{m.text}</Txt>
                  {m.sources?.length ? (
                    <View style={s.sources}>
                      {m.sources.map((src) => (
                        <Pressable key={src.url} onPress={() => Linking.openURL(src.url)} style={s.source}>
                          <Icon name="link-outline" size={12} color={colors.muted} />
                          <Txt variant="caption" numberOfLines={1} style={{ marginLeft: 4, maxWidth: 200 }}>{src.title}</Txt>
                        </Pressable>
                      ))}
                    </View>
                  ) : null}
                </View>
              </View>
            ),
          )}

          {conv.draft ? (
            <DraftCard
              draft={conv.draft}
              saving={conv.busy === 'saving'}
              listening={conv.listening}
              error={conv.draftError}
              onChange={conv.editDraft}
              onSave={conv.save}
              onCancel={conv.cancelDraft}
            />
          ) : null}

          {conv.messages.length > 2 ? (
            <Pressable onPress={() => router.push('/talk')} style={s.more}>
              <Icon name="chatbubbles-outline" size={15} color={colors.inkSoft} />
              <Txt variant="small" style={{ marginLeft: 6, color: colors.inkSoft }}>See the whole conversation</Txt>
            </Pressable>
          ) : null}
        </ScrollView>

        <View style={[s.bottom, { paddingBottom: insets.bottom + 10 }]}>
          {typing ? (
            <View style={s.inputWrap}>
              <TextInput
                value={typed}
                onChangeText={setTyped}
                placeholder="Type to JARVIS…"
                placeholderTextColor={colors.faint}
                style={s.input}
                autoFocus
                onSubmitEditing={sendTyped}
                returnKeyType="send"
              />
              <Pressable onPress={typed.trim() ? sendTyped : () => setTyping(false)} style={s.send} accessibilityLabel={typed.trim() ? 'Send' : 'Close'}>
                <Icon name={typed.trim() ? 'arrow-up' : 'close'} size={20} color={colors.white} />
              </Pressable>
            </View>
          ) : (
            <Pressable onPress={() => setTyping(true)} style={s.typeLink} accessibilityLabel="Type instead">
              <Icon name="create-outline" size={16} color={colors.muted} />
              <Txt variant="caption" style={{ marginLeft: 6 }}>Type instead</Txt>
            </Pressable>
          )}
        </View>
      </KeyboardAvoidingView>

      <Drawer
        open={menu}
        onClose={() => setMenu(false)}
        title={isMD ? 'Managing Director' : user.name}
        subtitle={data?.date_label ?? ''}
        items={items}
        onSelect={select}
        meetingMode={engine.meetingMode}
        onMeetingMode={engine.setMeetingMode}
        presence={presenceLabel[presence].detail}
      />
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  top: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingHorizontal: 10, paddingTop: 4 },
  badge: { position: 'absolute', top: 8, right: 8, width: 8, height: 8, borderRadius: 4, backgroundColor: colors.gold },
  pill: { flexDirection: 'row', alignItems: 'center', backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, borderRadius: 999, paddingHorizontal: 12, paddingVertical: 6 },
  dot: { width: 8, height: 8, borderRadius: 4, marginRight: 7 },
  body: { paddingHorizontal: 22, paddingTop: 18, paddingBottom: 24, alignItems: 'center' },
  orbWrap: { marginTop: 22, marginBottom: 14 },
  status: { color: colors.muted, textAlign: 'center' },
  partial: { fontFamily: fonts.serif, textAlign: 'center', marginTop: 10, lineHeight: 30 },
  suggestions: { alignSelf: 'stretch', marginTop: 26, rowGap: 10 },
  suggestion: { backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, borderRadius: radius.lg, paddingVertical: 12, paddingHorizontal: 16 },
  userRow: { alignSelf: 'stretch', flexDirection: 'row', justifyContent: 'flex-end', marginTop: 18 },
  userBubble: { maxWidth: '84%', backgroundColor: colors.surfaceAlt, borderRadius: 20, borderBottomRightRadius: 6, paddingVertical: 10, paddingHorizontal: 14 },
  jarvisRow: { alignSelf: 'stretch', flexDirection: 'row', alignItems: 'flex-start', marginTop: 14 },
  avatar: { width: 26, height: 26, borderRadius: 13, backgroundColor: colors.goldSoft, alignItems: 'center', justifyContent: 'center', marginRight: 10, marginTop: 1 },
  jarvisText: { fontSize: 17, lineHeight: 25 },
  sources: { flexDirection: 'row', flexWrap: 'wrap', marginTop: 6, columnGap: 8, rowGap: 6 },
  source: { flexDirection: 'row', alignItems: 'center', backgroundColor: colors.surfaceAlt, borderRadius: 999, paddingHorizontal: 8, paddingVertical: 3 },
  more: { flexDirection: 'row', alignItems: 'center', marginTop: 16 },
  bottom: { paddingHorizontal: 18, paddingTop: 8, alignItems: 'center' },
  typeLink: { flexDirection: 'row', alignItems: 'center', paddingVertical: 8, paddingHorizontal: 12 },
  inputWrap: { alignSelf: 'stretch', flexDirection: 'row', alignItems: 'center', backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, borderRadius: 26, paddingLeft: 16, paddingRight: 6, minHeight: 50 },
  input: { flex: 1, fontSize: 16, color: colors.text, paddingVertical: 10 },
  send: { width: 38, height: 38, borderRadius: 19, backgroundColor: colors.ink, alignItems: 'center', justifyContent: 'center' },
});
