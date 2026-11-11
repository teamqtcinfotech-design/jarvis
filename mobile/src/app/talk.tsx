import { Redirect, useRouter } from 'expo-router';
import React, { useRef, useState } from 'react';
import { KeyboardAvoidingView, Linking, Platform, Pressable, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import { SafeAreaView, useSafeAreaInsets } from 'react-native-safe-area-context';

import { DraftCard } from '@/components/DraftCard';
import { Icon, IconButton, Txt } from '@/components/ui';
import { useConversation } from '@/lib/conversation';
import { useSession } from '@/lib/session';
import { colors, fonts, radius } from '@/lib/theme';

/** Signed out (by hand, or the login expired) while this screen is open: go to the login page before the screen
 *  below runs, because the conversation it depends on is gone the moment the user is. */
export default function Talk() {
  const { user } = useSession();
  if (!user) return <Redirect href="/login" />;
  return <TalkScreen />;
}

/** The whole conversation, for reading back. The orb screen is the main way to talk. */
function TalkScreen() {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const conv = useConversation();
  const [typed, setTyped] = useState('');
  const scrollRef = useRef<React.ComponentRef<typeof ScrollView>>(null);

  const send = () => {
    const t = typed.trim();
    if (!t) return;
    setTyped('');
    conv.send(t);
  };

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }} edges={['top']}>
      <View style={s.top}>
        <IconButton icon="chevron-back" label="Back" onPress={() => (router.canGoBack() ? router.back() : router.replace('/'))} />
        <View style={{ flex: 1, alignItems: 'center' }}>
          <Txt style={s.brand}>JARVIS</Txt>
          <Txt variant="caption" style={{ color: conv.listening ? colors.gold : colors.muted }}>{conv.status}</Txt>
        </View>
        <IconButton icon="trash-outline" label="Clear conversation" onPress={conv.clear} color={colors.muted} />
      </View>

      <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
        <ScrollView
          ref={scrollRef}
          contentContainerStyle={{ padding: 18, paddingBottom: 24, flexGrow: 1 }}
          keyboardShouldPersistTaps="handled"
          onContentSizeChange={() => scrollRef.current?.scrollToEnd({ animated: true })}
        >
          {!conv.messages.length ? <Txt variant="small" style={{ textAlign: 'center', marginTop: 40 }}>Nothing yet. Tap the mic and speak.</Txt> : null}
          {conv.messages.map((m) =>
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
          {conv.listening && conv.partial ? (
            <View style={s.userRow}>
              <View style={[s.userBubble, { opacity: 0.55 }]}>
                <Txt variant="body">{conv.partial}</Txt>
              </View>
            </View>
          ) : null}
          {conv.draft ? (
            <DraftCard
              draft={conv.draft}
              saving={conv.busy === 'saving'}
              listening={conv.listening}
              error={conv.draftError}
              onChange={conv.editDraft}
              onSave={conv.save}
              onCancel={conv.cancelDraft}
              onReply={conv.send}
            />
          ) : null}
        </ScrollView>

        <View style={[s.composer, { paddingBottom: insets.bottom + 10 }]}>
          <View style={s.inputWrap}>
            <TextInput
              value={typed}
              onChangeText={setTyped}
              placeholder="Ask or tell JARVIS…"
              placeholderTextColor={colors.faint}
              style={s.input}
              multiline
            />
            {typed.trim() ? (
              <Pressable onPress={send} style={s.send} accessibilityLabel="Send">
                <Icon name="arrow-up" size={20} color={colors.white} />
              </Pressable>
            ) : null}
          </View>
          <Pressable
            onPress={conv.micPress}
            style={({ pressed }) => [s.mic, conv.listening && { backgroundColor: colors.gold }, pressed && { transform: [{ scale: 0.95 }] }]}
            accessibilityLabel={conv.listening ? 'Stop listening' : 'Speak to JARVIS'}
          >
            <Icon name={conv.listening ? 'mic' : conv.speaking ? 'stop' : 'mic-outline'} size={26} color={colors.white} />
          </Pressable>
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  top: { flexDirection: 'row', alignItems: 'center', paddingHorizontal: 10, paddingTop: 4, paddingBottom: 6 },
  brand: { fontFamily: fonts.serif, fontSize: 16, fontWeight: '700', lineHeight: 24, letterSpacing: 2, color: colors.ink },
  userRow: { flexDirection: 'row', justifyContent: 'flex-end', marginBottom: 14 },
  userBubble: { maxWidth: '82%', backgroundColor: colors.surfaceAlt, borderRadius: 20, borderBottomRightRadius: 6, paddingVertical: 10, paddingHorizontal: 14 },
  jarvisRow: { flexDirection: 'row', alignItems: 'flex-start', marginBottom: 16 },
  avatar: { width: 26, height: 26, borderRadius: 13, backgroundColor: colors.goldSoft, alignItems: 'center', justifyContent: 'center', marginRight: 10, marginTop: 1 },
  jarvisText: { fontSize: 16, lineHeight: 24 },
  sources: { flexDirection: 'row', flexWrap: 'wrap', marginTop: 6, columnGap: 8, rowGap: 6 },
  source: { flexDirection: 'row', alignItems: 'center', backgroundColor: colors.surfaceAlt, borderRadius: 999, paddingHorizontal: 8, paddingVertical: 3 },
  composer: { flexDirection: 'row', alignItems: 'flex-end', paddingHorizontal: 14, paddingTop: 10, borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: colors.line, backgroundColor: colors.bg },
  inputWrap: { flex: 1, flexDirection: 'row', alignItems: 'flex-end', backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, borderRadius: radius.xl, paddingLeft: 16, paddingRight: 6, minHeight: 48, marginRight: 10 },
  input: { flex: 1, fontSize: 16, color: colors.text, paddingVertical: 12, maxHeight: 120 },
  send: { width: 36, height: 36, borderRadius: 18, backgroundColor: colors.ink, alignItems: 'center', justifyContent: 'center', marginBottom: 6 },
  mic: { width: 52, height: 52, borderRadius: 26, backgroundColor: colors.ink, alignItems: 'center', justifyContent: 'center' },
});
