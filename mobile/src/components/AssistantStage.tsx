/**
 * Phone: JARVIS itself. There is no drawer to pull up and no second orb.
 *
 * On "Hey JARVIS" the screen behind blurs and frosts over, soft colours drift across it, and ONE orb rises in
 * the middle, slowly changing colour. His words appear under it, then the reply. Tap the orb to stop (or say
 * "bas"); tap the empty space, the X or Back to leave.
 *
 * When there is something to show (a card that needs a yes, a list, a chart) the orb glides up to the top and
 * gets small, and the card takes the screen beneath it, so nothing ever sits on top of anything else.
 *
 * The pencil on the home screen opens the same place ready for typing: the orb small at the top, a type box at
 * the bottom with a microphone beside it.
 */
import { useRouter } from 'expo-router';
import React, { useEffect, useRef, useState } from 'react';
import { Animated, Easing, Keyboard, Linking, Modal, Platform, Pressable, ScrollView, StyleSheet, TextInput, View, useWindowDimensions } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import Svg, { Circle, Defs, RadialGradient, Stop } from 'react-native-svg';

import { useConversation } from '@/lib/conversation';
import { mdWord } from '@/lib/names';
import { useSession } from '@/lib/session';
import { colors, fonts, radius } from '@/lib/theme';
import { useDashboard } from '@/lib/useDashboard';

import { DataCard } from './DataCard';
import { DraftCard } from './DraftCard';
import { Orb } from './Orb';
import { Icon, Txt } from './ui';

const BIG = 216;            // the orb in the middle
const SMALL = 0.4;          // and how small it gets at the top
/** Android 12 and later can blur what is behind; older phones get a more solid frost instead. */
export const CAN_BLUR = Platform.OS === 'android' && Number(Platform.Version) >= 31;

/** One soft blob of colour that fades to nothing at its edge. */
function Blob({ size, color, id }: { size: number; color: string; id: string }) {
  return (
    <Svg width={size} height={size}>
      <Defs>
        <RadialGradient id={id} cx="50%" cy="50%" r="50%">
          <Stop offset="0" stopColor={color} stopOpacity={0.6} />
          <Stop offset="0.55" stopColor={color} stopOpacity={0.22} />
          <Stop offset="1" stopColor={color} stopOpacity={0} />
        </RadialGradient>
      </Defs>
      <Circle cx={size / 2} cy={size / 2} r={size / 2} fill={`url(#${id})`} />
    </Svg>
  );
}

/** The changing colours behind the orb: three soft lights that drift and breathe at different speeds. */
function Aura({ width, height }: { width: number; height: number }) {
  const [a] = useState(() => new Animated.Value(0));
  const [b] = useState(() => new Animated.Value(0));
  const [c] = useState(() => new Animated.Value(0));
  useEffect(() => {
    const drift = (v: Animated.Value, ms: number) => Animated.loop(Animated.sequence([
      Animated.timing(v, { toValue: 1, duration: ms, easing: Easing.inOut(Easing.quad), useNativeDriver: true }),
      Animated.timing(v, { toValue: 0, duration: ms, easing: Easing.inOut(Easing.quad), useNativeDriver: true }),
    ]));
    const loops = [drift(a, 6200), drift(b, 8300), drift(c, 10100)];
    loops.forEach((l) => l.start());
    return () => loops.forEach((l) => l.stop());
  }, [a, b, c]);
  const size = Math.round(Math.max(width, 320) * 1.25);
  const move = (v: Animated.Value, x: [number, number], y: [number, number], o: [number, number]) => ({
    opacity: v.interpolate({ inputRange: [0, 1], outputRange: o }),
    transform: [
      { translateX: v.interpolate({ inputRange: [0, 1], outputRange: x }) },
      { translateY: v.interpolate({ inputRange: [0, 1], outputRange: y }) },
    ],
  });
  return (
    <View style={StyleSheet.absoluteFill} pointerEvents="none">
      <Animated.View style={[s.blob, { left: -size * 0.45, top: -size * 0.25 }, move(a, [0, size * 0.22], [0, size * 0.18], [0.55, 1])]}>
        <Blob size={size} color="#6C63FF" id="auraA" />
      </Animated.View>
      <Animated.View style={[s.blob, { left: width - size * 0.55, top: height * 0.22 }, move(b, [0, -size * 0.2], [0, size * 0.16], [1, 0.5])]}>
        <Blob size={size} color="#D46BFF" id="auraB" />
      </Animated.View>
      <Animated.View style={[s.blob, { left: -size * 0.3, top: height - size * 0.75 }, move(c, [0, size * 0.25], [0, -size * 0.14], [0.45, 0.95])]}>
        <Blob size={size} color="#45C8FF" id="auraC" />
      </Animated.View>
    </View>
  );
}

function useKeyboardHeight() {
  const [h, setH] = useState(0);
  useEffect(() => {
    const show = Keyboard.addListener('keyboardDidShow', (e) => setH(e.endCoordinates.height));
    const hide = Keyboard.addListener('keyboardDidHide', () => setH(0));
    return () => {
      show.remove();
      hide.remove();
    };
  }, []);
  return h;
}

export function AssistantStage() {
  const conv = useConversation();
  const { user } = useSession();
  const { data } = useDashboard();
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const { width, height } = useWindowDimensions();
  const [typed, setTyped] = useState('');
  const [mounted, setMounted] = useState(conv.talkOpen);
  if (conv.talkOpen && !mounted) setMounted(true); // mount as soon as it opens; unmount after the fade out
  const [fade] = useState(() => new Animated.Value(0));
  const [rise] = useState(() => new Animated.Value(0));      // the orb coming up as the stage opens
  const [small] = useState(() => new Animated.Value(0));     // 0 = big in the middle, 1 = small at the top
  const [typingChoice, setTypingChoice] = useState<{ at: number; on: boolean } | null>(null);
  const [readAll, setReadAll] = useState('');
  const [setAside, setSetAside] = useState('');
  const [rootH, setRootH] = useState(0);
  const [fullH, setFullH] = useState(0);
  if (rootH > fullH) setFullH(rootH);
  const scrollRef = useRef<React.ComponentRef<typeof ScrollView>>(null);
  const keyboard = useKeyboardHeight();
  const isMD = user?.role === 'MD';

  // Typing or talking: as it was opened (pencil = typing), unless he switched since.
  const typing = typingChoice && typingChoice.at === conv.talkOpenedAt ? typingChoice.on : conv.talkMode === 'type';
  const setTyping = (on: boolean) => setTypingChoice({ at: conv.talkOpenedAt, on });

  // What is there to show. An answer that came as data and is still the latest thing takes the screen; a card
  // left waiting from earlier never hides it (a line offers the way back to the card).
  const lastUser = [...conv.messages].reverse().find((m) => m.role === 'user');
  const lastJarvis = [...conv.messages].reverse().find((m) => m.role === 'jarvis');
  const topAnswer = conv.answers[0];
  const shown = topAnswer && topAnswer.id !== setAside && topAnswer.at >= Math.max(lastUser?.at ?? 0, conv.talkOpenedAt) ? topAnswer : null;
  const said = shown ? [...conv.messages].reverse().find((m) => m.role === 'jarvis' && m.at >= shown.at) : undefined;
  const compact = Boolean(shown || conv.draft || typing || keyboard > 0);

  // Open: everything fades in and the orb rises into place. Close: fade out, then leave.
  useEffect(() => {
    if (conv.talkOpen) {
      fade.setValue(0);
      rise.setValue(0);
      Animated.parallel([
        Animated.timing(fade, { toValue: 1, duration: 260, useNativeDriver: true }),
        Animated.spring(rise, { toValue: 1, friction: 7, tension: 50, useNativeDriver: true }),
      ]).start();
    } else if (mounted) {
      Animated.timing(fade, { toValue: 0, duration: 200, useNativeDriver: true }).start(({ finished }) => finished && setMounted(false));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conv.talkOpen]);
  useEffect(() => {
    Animated.spring(small, { toValue: compact ? 1 : 0, friction: 9, tension: 60, useNativeDriver: true }).start();
  }, [compact, small]);

  // He got a short spoken answer and there is nothing to look at: JARVIS steps off the screen by itself after a
  // few quiet seconds. A card, a list, a long answer or the type box keep it open.
  const last = conv.messages[conv.messages.length - 1];
  const nothingToRead = !last || (last.text.length <= 140 && !last.sources?.length && last.tone !== 'error');
  const resting = conv.talkOpen && conv.orb === 'idle' && !compact && nothingToRead;
  const closeRef = useRef(conv.closeTalk);
  useEffect(() => {
    closeRef.current = conv.closeTalk;
  });
  useEffect(() => {
    if (!resting) return undefined;
    const t = setTimeout(() => closeRef.current(), 6000);
    return () => clearTimeout(t);
  }, [resting, last?.id]);

  if (!mounted) return null;

  const send = () => {
    const t = typed.trim();
    if (!t) return;
    setTyped('');
    Keyboard.dismiss(); // the answer gets the whole screen, not the half the keyboard leaves
    conv.send(t);
  };
  const speak = () => {
    Keyboard.dismiss();
    setTyping(false);
    conv.micPress();
  };
  const md = mdWord(data);
  const suggestions = isMD
    ? ['Aaj ka plan kya hai?', 'Meri staff ki list dikhao', `${data?.pa_name || 'PA'} ke kal ke tasks dikhao`, 'Kal 11 baje Sharma ji ke saath meeting']
    : [`${md} aaj free kab hain?`, 'Mere tasks dikhao', `${md} ke liye kal 3 baje bank meeting`, 'Kya kya ho gaya?'];
  const recent = conv.draft ? conv.messages.slice(-2) : conv.messages.slice(-6);

  // Where the orb sits. It is one orb the whole time: it only moves and changes size.
  const bigTop = Math.round(height * 0.3 - BIG / 2);
  const smallTop = insets.top + 6 - (BIG * (1 - SMALL)) / 2; // so its visible top edge is just under the status bar
  const orbY = Animated.add(
    small.interpolate({ inputRange: [0, 1], outputRange: [bigTop, smallTop] }),
    rise.interpolate({ inputRange: [0, 1], outputRange: [60, 0] }),
  );
  const orbScale = Animated.multiply(
    small.interpolate({ inputRange: [0, 1], outputRange: [1, SMALL] }),
    rise.interpolate({ inputRange: [0, 1], outputRange: [0.6, 1] }),
  );
  const headRoom = insets.top + 6 + BIG * SMALL + 6;      // space the small orb takes at the top
  // Some phones resize the window for the keyboard and some do not: pad by hand only when it did not shrink.
  const keyboardPad = keyboard > 0 && rootH > fullH - 80 ? keyboard : 0;

  return (
    <Modal visible transparent animationType="none" onRequestClose={conv.closeTalk} statusBarTranslucent>
      <Animated.View style={[StyleSheet.absoluteFill, { opacity: fade }]} onLayout={(e) => setRootH(e.nativeEvent.layout.height)}>
        {/* frost + drifting colours over the (blurred) home; a tap on empty space leaves */}
        <View style={[StyleSheet.absoluteFill, { backgroundColor: CAN_BLUR ? 'rgba(248,248,253,0.72)' : 'rgba(248,248,253,0.93)' }]} />
        <Aura width={width} height={height} />
        <Pressable style={StyleSheet.absoluteFill} onPress={compact ? undefined : conv.closeTalk} accessibilityLabel="Close JARVIS" />

        {/* the words under the big orb */}
        {!compact ? (
          <View style={[s.voice, { top: bigTop + BIG - 6 }]} pointerEvents="box-none">
            <Txt style={[s.status, conv.listening && { color: colors.accent }]}>{conv.status}</Txt>
            {conv.listening && conv.partial ? (
              <Txt style={s.partial}>{conv.partial}</Txt>
            ) : (
              <ScrollView style={{ maxHeight: height * 0.3, alignSelf: 'stretch' }} contentContainerStyle={{ alignItems: 'center' }} showsVerticalScrollIndicator={false}>
                {lastUser && lastJarvis && lastUser.at <= lastJarvis.at ? <Txt style={s.heard}>“{lastUser.text}”</Txt> : null}
                {lastJarvis ? <Txt style={[s.reply, lastJarvis.tone === 'error' && { color: colors.danger }]}>{lastJarvis.text}</Txt> : null}
                {lastJarvis?.sources?.length ? (
                  <View style={s.sources}>
                    {lastJarvis.sources.map((src) => (
                      <Pressable key={src.url} onPress={() => Linking.openURL(src.url)} style={s.source}>
                        <Icon name="link-outline" size={12} color={colors.muted} />
                        <Txt variant="caption" numberOfLines={1} style={{ marginLeft: 4, maxWidth: 200 }}>{src.title}</Txt>
                      </Pressable>
                    ))}
                  </View>
                ) : null}
              </ScrollView>
            )}
          </View>
        ) : null}

        {/* something to show: it gets the screen under the small orb */}
        {compact ? (
          <View style={[s.content, { paddingTop: headRoom, paddingBottom: keyboardPad }]} pointerEvents="box-none">
            <Txt numberOfLines={1} style={[s.status, { marginBottom: 6 }, conv.listening && { color: colors.accent }]}>
              {conv.listening && conv.partial ? conv.partial : conv.status}
            </Txt>
            {shown ? (
              <View style={s.fill}>
                {said ? (
                  <Pressable style={s.said} onPress={() => setReadAll((id) => (id === shown.id ? '' : shown.id))}>
                    <Txt variant="body" numberOfLines={readAll === shown.id ? 12 : 3} style={{ lineHeight: 21, textAlign: 'center' }}>{said.text}</Txt>
                  </Pressable>
                ) : null}
                {conv.draft ? (
                  <Pressable style={s.waiting} onPress={() => setSetAside(shown.id)}>
                    <Icon name="document-text-outline" size={15} color={colors.warn} />
                    <Txt variant="small" numberOfLines={1} style={s.waitingText}>
                      Still waiting for your yes: {conv.draft.title || conv.draft.target?.title || 'a card'}
                    </Txt>
                    <Txt variant="small" style={{ color: colors.warn, fontWeight: '600' }}>Open</Txt>
                  </Pressable>
                ) : null}
                <DataCard key={shown.id} fit view={shown.view} onRemove={() => conv.removeAnswer(shown.id)} />
              </View>
            ) : (
              <ScrollView
                ref={scrollRef}
                style={s.fill}
                contentContainerStyle={{ paddingBottom: 12 }}
                keyboardShouldPersistTaps="handled"
                showsVerticalScrollIndicator={false}
                onContentSizeChange={() => scrollRef.current?.scrollToEnd({ animated: true })}
              >
                {!recent.length && !conv.busy ? (
                  <View style={{ rowGap: 8, marginTop: 4 }}>
                    <Txt variant="small" style={{ textAlign: 'center', marginBottom: 2 }}>Type, or tap the orb and speak.</Txt>
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
                        <Txt variant="body" style={[{ fontSize: 16, lineHeight: 24 }, m.tone === 'error' && { color: colors.danger }]}>{m.text}</Txt>
                        {m.sources?.length ? (
                          <View style={[s.sources, { justifyContent: 'flex-start' }]}>
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
                  <DraftCard draft={conv.draft} saving={conv.busy === 'saving'} listening={conv.listening} error={conv.draftError} onChange={conv.editDraft} onSave={conv.save} onCancel={conv.cancelDraft} onReply={conv.send} />
                ) : null}
              </ScrollView>
            )}

            {/* the bottom row: a type box when typing, otherwise one small key to start typing */}
            {typing || keyboard > 0 ? (
              <View style={[s.composer, { paddingBottom: (keyboardPad ? 8 : insets.bottom + 10) }]}>
                <View style={s.inputWrap}>
                  <TextInput
                    value={typed}
                    onChangeText={setTyped}
                    placeholder="Type to JARVIS…"
                    placeholderTextColor={colors.faint}
                    style={s.input}
                    onSubmitEditing={send}
                    returnKeyType="send"
                    autoFocus
                  />
                </View>
                <Pressable onPress={typed.trim() ? send : speak} style={({ pressed }) => [s.round, s.roundDark, pressed && { opacity: 0.7 }]} accessibilityLabel={typed.trim() ? 'Send' : 'Speak instead'}>
                  <Icon name={typed.trim() ? 'arrow-up' : 'mic'} size={20} color={colors.white} />
                </Pressable>
              </View>
            ) : (
              <View style={[s.composer, { paddingBottom: insets.bottom + 10, justifyContent: 'flex-end' }]}>
                <Pressable onPress={() => setTyping(true)} style={({ pressed }) => [s.round, pressed && { opacity: 0.7 }]} accessibilityLabel="Type instead">
                  <Icon name="create-outline" size={20} color={colors.inkSoft} />
                </Pressable>
              </View>
            )}
          </View>
        ) : null}

        {/* the one orb: big in the middle, or small at the top when there is something to show */}
        <Animated.View style={[s.orb, { left: (width - BIG) / 2, transform: [{ translateY: orbY }, { scale: orbScale }] }]} pointerEvents="box-none">
          <Orb state={conv.orb} size={BIG} level={conv.level} onPress={conv.micPress} shift />
        </Animated.View>

        {/* always in the same two corners: the whole conversation, and the way out */}
        <Pressable onPress={() => { conv.closeTalk(); router.push('/talk'); }} hitSlop={10} accessibilityLabel="Whole conversation" style={({ pressed }) => [s.corner, { top: insets.top + 10, left: 14 }, pressed && { opacity: 0.6 }]}>
          <Icon name="chatbubbles-outline" size={19} color={colors.inkSoft} />
        </Pressable>
        <Pressable onPress={conv.closeTalk} hitSlop={10} accessibilityLabel="Close" style={({ pressed }) => [s.corner, { top: insets.top + 10, right: 14 }, pressed && { opacity: 0.6 }]}>
          <Icon name="close" size={21} color={colors.inkSoft} />
        </Pressable>

        {/* when it is only the orb: how to stop, and the pencil to type instead */}
        {!compact ? (
          <View style={[s.foot, { bottom: insets.bottom + 18 }]} pointerEvents="box-none">
            <View style={s.hint}>
              <Txt variant="caption" style={{ color: colors.inkSoft }}>
                {conv.listening ? 'Tap the orb to stop · or say “bas”' : conv.speaking ? 'Tap the orb to interrupt' : 'Tap the orb to speak'}
              </Txt>
            </View>
            <Pressable onPress={() => setTyping(true)} style={({ pressed }) => [s.round, { marginLeft: 10 }, pressed && { opacity: 0.7 }]} accessibilityLabel="Type instead">
              <Icon name="create-outline" size={20} color={colors.inkSoft} />
            </Pressable>
          </View>
        ) : null}
      </Animated.View>
    </Modal>
  );
}

const s = StyleSheet.create({
  blob: { position: 'absolute' },
  orb: { position: 'absolute', top: 0, width: BIG, height: BIG },
  voice: { position: 'absolute', left: 24, right: 24, alignItems: 'center' },
  status: { fontSize: 13, lineHeight: 18, color: colors.muted, fontFamily: fonts.medium, textAlign: 'center' },
  partial: { marginTop: 12, fontSize: 22, lineHeight: 31, color: colors.accentDeep, fontFamily: fonts.semibold, textAlign: 'center' },
  heard: { marginTop: 12, fontSize: 14, lineHeight: 20, color: colors.muted, textAlign: 'center' },
  reply: { marginTop: 8, fontSize: 19, lineHeight: 28, color: colors.ink, fontFamily: fonts.medium, textAlign: 'center' },
  sources: { flexDirection: 'row', flexWrap: 'wrap', justifyContent: 'center', marginTop: 10, columnGap: 8, rowGap: 6 },
  source: { flexDirection: 'row', alignItems: 'center', backgroundColor: 'rgba(255,255,255,0.7)', borderRadius: 999, paddingHorizontal: 8, paddingVertical: 3 },
  content: { position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, paddingHorizontal: 14 },
  fill: { flex: 1, minHeight: 0 },
  said: { marginBottom: 8, paddingHorizontal: 8 },
  waiting: { flexDirection: 'row', alignItems: 'center', backgroundColor: colors.warnSoft, borderRadius: radius.md, paddingVertical: 8, paddingHorizontal: 10, marginBottom: 8 },
  waitingText: { flex: 1, marginHorizontal: 8, color: colors.warn },
  suggestion: { flexDirection: 'row', alignItems: 'center', backgroundColor: 'rgba(255,255,255,0.85)', borderWidth: 1, borderColor: colors.line, borderRadius: radius.md, paddingVertical: 12, paddingHorizontal: 14 },
  userRow: { flexDirection: 'row', justifyContent: 'flex-end', marginTop: 12 },
  userBubble: { maxWidth: '84%', backgroundColor: colors.accentSoft, borderRadius: 18, borderBottomRightRadius: 6, paddingVertical: 9, paddingHorizontal: 14 },
  jarvisRow: { flexDirection: 'row', alignItems: 'flex-start', marginTop: 12 },
  avatar: { width: 26, height: 26, borderRadius: 13, backgroundColor: colors.accentSoft, alignItems: 'center', justifyContent: 'center', marginRight: 10, marginTop: 1 },
  composer: { flexDirection: 'row', alignItems: 'center', paddingTop: 8 },
  inputWrap: { flex: 1, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, borderRadius: 26, paddingHorizontal: 16, minHeight: 50, justifyContent: 'center', marginRight: 8 },
  input: { fontSize: 15, color: colors.text, paddingVertical: 10, fontFamily: fonts.regular },
  round: { width: 50, height: 50, borderRadius: 25, backgroundColor: 'rgba(255,255,255,0.92)', borderWidth: 1, borderColor: colors.line, alignItems: 'center', justifyContent: 'center' },
  roundDark: { backgroundColor: colors.ink, borderColor: colors.ink },
  corner: { position: 'absolute', width: 42, height: 42, borderRadius: 21, backgroundColor: 'rgba(255,255,255,0.9)', borderWidth: 1, borderColor: colors.line, alignItems: 'center', justifyContent: 'center' },
  foot: { position: 'absolute', left: 0, right: 0, flexDirection: 'row', alignItems: 'center', justifyContent: 'center' },
  hint: { backgroundColor: 'rgba(255,255,255,0.9)', borderWidth: 1, borderColor: colors.line, borderRadius: 999, paddingHorizontal: 14, paddingVertical: 9 },
});
