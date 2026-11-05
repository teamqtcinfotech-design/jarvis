/**
 * Phone: the talk layer. On "Hey JARVIS", the floating orb or a held-alert offer, an indigo glow with
 * moving waves rises from the bottom edge of the phone, the orb lifts into the corner, and a panel with his
 * words, the reply, the waiting card and the answer-as-data slides up behind it. Tap down to close; answers
 * stay on the home screen.
 */
import { LinearGradient } from 'expo-linear-gradient';
import { useRouter } from 'expo-router';
import React, { useEffect, useRef, useState } from 'react';
import { Animated, Easing, KeyboardAvoidingView, Linking, Modal, Platform, Pressable, ScrollView, StyleSheet, TextInput, View, useWindowDimensions } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { useConversation } from '@/lib/conversation';
import { useSession } from '@/lib/session';
import { colors, fonts, radius } from '@/lib/theme';

import { DataCard } from './DataCard';
import { DraftCard } from './DraftCard';
import { Orb, Waves } from './Orb';
import { Icon, IconButton, Txt } from './ui';

const GLOW_H = 300;

export function TalkSheet() {
  const conv = useConversation();
  const { user } = useSession();
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const { width, height } = useWindowDimensions();
  const [typed, setTyped] = useState('');
  const [mounted, setMounted] = useState(conv.talkOpen);
  if (conv.talkOpen && !mounted) setMounted(true); // mount as soon as it opens; unmount after the exit animation
  const scrollRef = useRef<React.ComponentRef<typeof ScrollView>>(null);
  const [glowY] = useState(() => new Animated.Value(GLOW_H));
  const [panelY] = useState(() => new Animated.Value(height));
  const [orbScale] = useState(() => new Animated.Value(0.4));
  const [dim] = useState(() => new Animated.Value(0));
  const [glowRise] = useState(() => new Animated.Value(0));
  const isMD = user?.role === 'MD';

  // Entrance: glow and waves rise from the bezel, the orb lifts, then the panel follows. Exit in reverse.
  useEffect(() => {
    if (conv.talkOpen) {
      glowY.setValue(GLOW_H);
      panelY.setValue(height);
      orbScale.setValue(0.4);
      dim.setValue(0);
      Animated.parallel([
        Animated.timing(dim, { toValue: 1, duration: 350, useNativeDriver: true }),
        Animated.timing(glowY, { toValue: 0, duration: 520, easing: Easing.out(Easing.cubic), useNativeDriver: true }),
        Animated.sequence([Animated.delay(120), Animated.spring(orbScale, { toValue: 1, friction: 6, tension: 60, useNativeDriver: true })]),
        Animated.sequence([Animated.delay(200), Animated.spring(panelY, { toValue: 0, friction: 9, tension: 50, useNativeDriver: true })]),
      ]).start();
      const breathe = Animated.loop(Animated.sequence([
        Animated.timing(glowRise, { toValue: 1, duration: 2400, easing: Easing.inOut(Easing.quad), useNativeDriver: true }),
        Animated.timing(glowRise, { toValue: 0, duration: 2400, easing: Easing.inOut(Easing.quad), useNativeDriver: true }),
      ]));
      breathe.start();
      return () => breathe.stop();
    }
    if (mounted) {
      Animated.parallel([
        Animated.timing(dim, { toValue: 0, duration: 260, useNativeDriver: true }),
        Animated.timing(glowY, { toValue: GLOW_H, duration: 320, easing: Easing.in(Easing.cubic), useNativeDriver: true }),
        Animated.timing(panelY, { toValue: height, duration: 280, easing: Easing.in(Easing.cubic), useNativeDriver: true }),
        Animated.timing(orbScale, { toValue: 0.4, duration: 220, useNativeDriver: true }),
      ]).start(({ finished }) => finished && setMounted(false));
    }
    return undefined;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conv.talkOpen]);

  if (!mounted) return null;

  const send = () => {
    const t = typed.trim();
    if (!t) return;
    setTyped('');
    conv.send(t);
  };
  const recent = conv.messages.slice(-6);
  const suggestions = isMD
    ? ['Aaj ka plan kya hai?', 'Is hafte ka load dikhao', 'Staff performance report', 'Kal 11 baje Sharma ji ke saath meeting']
    : ['Sir aaj free kab hain?', 'Mere tasks dikhao', 'Sir ke liye kal 3 baje bank meeting', 'Kya kya ho gaya?'];
  const glowBob = glowRise.interpolate({ inputRange: [0, 1], outputRange: [0, -14] });
  const panelMax = Math.round(height * 0.74);

  return (
    <Modal visible transparent animationType="none" onRequestClose={conv.closeTalk} statusBarTranslucent>
      {/* dim the home behind */}
      <Animated.View style={[StyleSheet.absoluteFill, { backgroundColor: colors.overlay, opacity: dim }]}>
        <Pressable style={StyleSheet.absoluteFill} onPress={conv.closeTalk} accessibilityLabel="Close" />
      </Animated.View>

      {/* the glow that rises from the bottom edge */}
      <Animated.View pointerEvents="none" style={[s.glow, { height: GLOW_H, transform: [{ translateY: glowY }] }]}>
        <LinearGradient colors={['rgba(91,91,214,0)', 'rgba(142,142,242,0.45)', 'rgba(91,91,214,0.95)']} style={StyleSheet.absoluteFill} />
        <Waves width={width} height={GLOW_H} rise={glowBob} speed={0.8} color="rgba(255,255,255,0.22)" layers={3} baseline={0.5} />
      </Animated.View>

      {/* the panel */}
      <Animated.View style={[s.panel, { maxHeight: panelMax, transform: [{ translateY: panelY }] }]}>
        <View style={s.grabber} />
        <View style={s.head}>
          <View style={{ flex: 1 }}>
            <Txt style={s.brand}>JARVIS</Txt>
            <Txt variant="caption" style={{ color: conv.listening ? colors.accent : colors.muted }}>{conv.status}</Txt>
          </View>
          <IconButton icon="chatbubbles-outline" label="Whole conversation" color={colors.muted} onPress={() => { conv.closeTalk(); router.push('/talk'); }} />
          <IconButton icon="chevron-down" label="Close" onPress={conv.closeTalk} />
        </View>

        <KeyboardAvoidingView style={{ flexShrink: 1 }} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
          <ScrollView
            ref={scrollRef}
            style={{ flexGrow: 0 }}
            contentContainerStyle={s.body}
            keyboardShouldPersistTaps="handled"
            onContentSizeChange={() => scrollRef.current?.scrollToEnd({ animated: true })}
          >
            {conv.listening && conv.partial ? <Txt variant="title" style={s.partial}>{conv.partial}</Txt> : null}
            {!recent.length && !conv.listening ? (
              <View style={s.suggestions}>
                {suggestions.map((q) => (
                  <Pressable key={q} onPress={() => conv.send(q)} style={({ pressed }) => [s.suggestion, pressed && { opacity: 0.7 }]}>
                    <Icon name="sparkles-outline" size={14} color={colors.accent} />
                    <Txt variant="small" style={{ color: colors.text, marginLeft: 8, flex: 1 }}>{q}</Txt>
                  </Pressable>
                ))}
              </View>
            ) : null}
            {recent.map((m) =>
              m.role === 'user' ? (
                <View key={m.id} style={s.userRow}>
                  <View style={s.userBubble}><Txt variant="body">{m.text}</Txt></View>
                </View>
              ) : (
                <View key={m.id} style={s.jarvisRow}>
                  <View style={[s.avatar, m.tone === 'error' && { backgroundColor: colors.dangerSoft }, m.tone === 'saved' && { backgroundColor: colors.successSoft }]}>
                    <Icon name={m.tone === 'error' ? 'alert' : m.tone === 'saved' ? 'checkmark' : 'sparkles'} size={13} color={m.tone === 'error' ? colors.danger : m.tone === 'saved' ? colors.success : colors.accent} />
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
              <DraftCard draft={conv.draft} saving={conv.busy === 'saving'} listening={conv.listening} error={conv.draftError} onChange={conv.editDraft} onSave={conv.save} onCancel={conv.cancelDraft} />
            ) : null}
            {conv.answers.slice(0, 1).map((a) => (
              <DataCard key={a.id} view={a.view} extraText={a.text} onRemove={() => conv.removeAnswer(a.id)} />
            ))}
          </ScrollView>
          <View style={[s.composer, { paddingBottom: insets.bottom + 12 }]}>
            <View style={s.inputWrap}>
              <TextInput
                value={typed}
                onChangeText={setTyped}
                placeholder="Type to JARVIS…"
                placeholderTextColor={colors.faint}
                style={s.input}
                onSubmitEditing={send}
                returnKeyType="send"
              />
              {typed.trim() ? (
                <Pressable onPress={send} style={s.send} accessibilityLabel="Send"><Icon name="arrow-up" size={18} color={colors.white} /></Pressable>
              ) : null}
            </View>
            <View style={{ width: 96 }} />
          </View>
        </KeyboardAvoidingView>
      </Animated.View>

      {/* the orb, lifted into the bottom-right corner above the panel */}
      <Animated.View pointerEvents="box-none" style={[s.orbWrap, { right: 6, bottom: insets.bottom + 2, transform: [{ scale: orbScale }] }]}>
        <Orb state={conv.orb} size={118} level={conv.level} onPress={conv.micPress} />
      </Animated.View>
    </Modal>
  );
}

const s = StyleSheet.create({
  glow: { position: 'absolute', left: 0, right: 0, bottom: 0 },
  panel: {
    position: 'absolute', left: 0, right: 0, bottom: 0,
    backgroundColor: colors.bg, borderTopLeftRadius: radius.xl, borderTopRightRadius: radius.xl,
    paddingTop: 8,
    shadowColor: colors.accentDeep, shadowOpacity: 0.25, shadowRadius: 24, shadowOffset: { width: 0, height: -8 }, elevation: 20,
  },
  grabber: { alignSelf: 'center', width: 44, height: 5, borderRadius: 3, backgroundColor: colors.line, marginBottom: 4 },
  head: { flexDirection: 'row', alignItems: 'center', paddingLeft: 20, paddingRight: 8, paddingBottom: 4 },
  brand: { fontFamily: fonts.bold, fontSize: 15, lineHeight: 22, letterSpacing: 2, color: colors.ink },
  body: { paddingHorizontal: 20, paddingTop: 4, paddingBottom: 16 },
  partial: { textAlign: 'center', marginTop: 6, lineHeight: 30, color: colors.accentDeep },
  suggestions: { marginTop: 6, rowGap: 8 },
  suggestion: { flexDirection: 'row', alignItems: 'center', backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, borderRadius: radius.md, paddingVertical: 11, paddingHorizontal: 14 },
  userRow: { flexDirection: 'row', justifyContent: 'flex-end', marginTop: 12 },
  userBubble: { maxWidth: '84%', backgroundColor: colors.accentSoft, borderRadius: 18, borderBottomRightRadius: 6, paddingVertical: 9, paddingHorizontal: 14 },
  jarvisRow: { flexDirection: 'row', alignItems: 'flex-start', marginTop: 12 },
  avatar: { width: 26, height: 26, borderRadius: 13, backgroundColor: colors.accentSoft, alignItems: 'center', justifyContent: 'center', marginRight: 10, marginTop: 1 },
  jarvisText: { fontSize: 16, lineHeight: 24 },
  sources: { flexDirection: 'row', flexWrap: 'wrap', marginTop: 6, columnGap: 8, rowGap: 6 },
  source: { flexDirection: 'row', alignItems: 'center', backgroundColor: colors.surfaceAlt, borderRadius: 999, paddingHorizontal: 8, paddingVertical: 3 },
  composer: { flexDirection: 'row', alignItems: 'center', paddingLeft: 16, paddingRight: 8, paddingTop: 8, borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: colors.line, backgroundColor: colors.bg },
  inputWrap: { flex: 1, flexDirection: 'row', alignItems: 'center', backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, borderRadius: 24, paddingLeft: 16, paddingRight: 6, minHeight: 46 },
  input: { flex: 1, fontSize: 15, color: colors.text, paddingVertical: 9, fontFamily: fonts.regular },
  send: { width: 34, height: 34, borderRadius: 17, backgroundColor: colors.ink, alignItems: 'center', justifyContent: 'center' },
  orbWrap: { position: 'absolute' },
});
