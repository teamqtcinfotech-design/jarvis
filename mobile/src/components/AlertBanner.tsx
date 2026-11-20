/**
 * The banner at the top of the screen: what is waiting for him right now.
 *
 *  - The most important item is the big card: who it is from, how long ago, its priority, and its one or two
 *    buttons (Seen / Later for work and messages that ring until seen; Accept / Decline for a request; Done / Got
 *    it for a reminder).
 *  - Everything else waiting sits beneath it as a short stack, ordered by priority (urgent first), each row with
 *    its colour, who it is from, and its own quick Seen. A tap on a row brings it to the front.
 *  - A ringing item shows a pulsing dot; an urgent one a soft red glow. Nothing here is a plain "Got it" for
 *    work or a message from the team: those stay until Seen.
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Animated, PanResponder, Pressable, StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { type EngineItem, useAlertEngine } from '@/lib/alertEngine';
import { relativeAgo } from '@/lib/format';
import { usePrefs } from '@/lib/prefs';
import { colors, kindIcon, priorityColor, priorityLabel, radius, shadow } from '@/lib/theme';

import { Button, Icon, Txt } from './ui';

const MAX_ROWS = 3;
// The banner sits UNDER the screen's own top bar (menu, title, settings), never over it: those stay tappable.
const BELOW_TOP_BAR = 54;
// After this long the card folds into a slim pill by itself, so the screen underneath is usable; a tap unfolds it.
const FOLD_AFTER_MS = 7000;

/** "Incoming task" / "New task" / "Message" / "Update" / "Urgent reminder". */
function labelOf(item: EngineItem): string {
  if (item.alertKind === 'NEW_REQUEST' && item.requestId) return 'Incoming task';
  if (item.alertKind === 'TASK_ASSIGNED') return 'New task';
  if (item.personal) return 'Message';
  if (item.source === 'server') return 'Update';
  return `${priorityLabel[item.priority]} reminder`;
}

function iconOf(item: EngineItem): string {
  if (item.alertKind === 'NEW_REQUEST' && item.requestId) return 'mail-unread-outline';
  if (item.personal && item.alertKind !== 'TASK_ASSIGNED') return 'chatbubble-ellipses-outline';
  if (item.source === 'server') return 'notifications-outline';
  return kindIcon[item.kind ?? 'TASK'] ?? 'alarm-outline';
}

/** The first letter of whoever it is from, in a round badge of the item's priority colour. */
function Avatar({ item, size = 34 }: { item: EngineItem; size?: number }) {
  const color = priorityColor[item.priority];
  const letter = (item.from || '').trim().charAt(0).toUpperCase();
  return (
    <View style={[s.avatar, { width: size, height: size, borderRadius: size / 2, backgroundColor: color }]}>
      {letter ? (
        <Txt variant="heading" style={{ color: colors.white, fontSize: size * 0.46, lineHeight: size * 0.56 }}>{letter}</Txt>
      ) : (
        <Icon name={iconOf(item)} size={size * 0.52} color={colors.white} />
      )}
    </View>
  );
}

/** A small pulsing dot: this item rings until Seen. */
function RingDot({ color }: { color: string }) {
  const [pulse] = useState(() => new Animated.Value(0.35));
  useEffect(() => {
    const loop = Animated.loop(Animated.sequence([
      Animated.timing(pulse, { toValue: 1, duration: 650, useNativeDriver: true }),
      Animated.timing(pulse, { toValue: 0.35, duration: 650, useNativeDriver: true }),
    ]));
    loop.start();
    return () => loop.stop();
  }, [pulse]);
  return <Animated.View style={[s.dot, { backgroundColor: color, opacity: pulse }]} />;
}

export function AlertBanner() {
  const engine = useAlertEngine();
  const { prefs } = usePrefs();
  const item = engine.current;
  // "Later" = the person's usual snooze, learned from what they say by voice ("10 minute baad").
  const later = `Later · ${prefs.snoozeMinutes}m`;
  const insets = useSafeAreaInsets();
  const [slide] = useState(() => new Animated.Value(-320));
  const [glow] = useState(() => new Animated.Value(0));
  const urgent = item?.priority === 'URGENT';
  const others = useMemo(() => engine.waiting.filter((w) => w.key !== item?.key), [engine.waiting, item?.key]);
  const [showAll, setShowAll] = useState(false);
  // Folded = the slim pill (who, what, how many more); unfolded = the full card. A new item unfolds; after a few
  // seconds it folds by itself; a swipe up folds it at once; a tap on the pill unfolds it again.
  const [folded, setFolded] = useState(false);
  const [foldedKey, setFoldedKey] = useState('');
  if (item && item.key !== foldedKey) {
    setFoldedKey(item.key);
    setFolded(false);
  }
  useEffect(() => {
    if (!item || folded) return undefined;
    const t = setTimeout(() => setFolded(true), FOLD_AFTER_MS);
    return () => clearTimeout(t);
  }, [item, folded]);
  const pan = useMemo(() => PanResponder.create({
    onMoveShouldSetPanResponder: (_e, g) => Math.abs(g.dy) > 12 && Math.abs(g.dy) > Math.abs(g.dx),
    onPanResponderRelease: (_e, g) => {
      if (g.dy < -30) setFolded(true);     // swipe up: out of the way
      else if (g.dy > 30) setFolded(false); // swipe down: the whole card
    },
  }), []);

  useEffect(() => {
    Animated.spring(slide, { toValue: item ? 0 : -320, useNativeDriver: true, friction: 9 }).start();
  }, [item, slide]);

  // An urgent item breathes a soft glow until it is dealt with.
  useEffect(() => {
    if (!urgent) {
      glow.setValue(0);
      return undefined;
    }
    const loop = Animated.loop(Animated.sequence([
      Animated.timing(glow, { toValue: 1, duration: 900, useNativeDriver: true }),
      Animated.timing(glow, { toValue: 0.15, duration: 900, useNativeDriver: true }),
    ]));
    loop.start();
    return () => loop.stop();
  }, [urgent, glow]);

  if (!item) return null;
  const isRequest = item.alertKind === 'NEW_REQUEST' && Boolean(item.requestId);
  const color = priorityColor[item.priority];
  const title = item.concealed ? (item.priority === 'URGENT' ? 'Urgent private reminder' : 'Private reminder') : item.title;
  const rows = showAll ? others : others.slice(0, MAX_ROWS);
  const more = others.length - rows.length;
  const quick = () => (item.ring ? engine.markSeen(item) : item.source === 'reminder' || isRequest ? setFolded(false) : engine.acknowledge(item));

  if (folded) {
    // The pill: one line under the top bar, the screen free beneath. Tap = the full card; the eye = Seen / Got it.
    return (
      <Animated.View style={[s.wrap, { top: insets.top + BELOW_TOP_BAR, transform: [{ translateY: slide }] }]} pointerEvents="box-none" {...pan.panHandlers}>
        <Pressable onPress={() => setFolded(false)} style={({ pressed }) => [s.pill, { borderColor: color }, pressed && { opacity: 0.85 }]} accessibilityLabel="Show what is waiting">
          <Avatar item={item} size={24} />
          {item.ring ? <View style={{ marginLeft: 8 }}><RingDot color={color} /></View> : null}
          <Txt variant="small" numberOfLines={1} style={{ flex: 1, marginLeft: item.ring ? 2 : 8, fontWeight: '600', color: colors.text }}>
            {item.from ? `${item.from} · ` : ''}{item.concealed ? 'Private reminder' : item.title}
          </Txt>
          {others.length ? (
            <View style={[s.countPill, { backgroundColor: color, marginLeft: 6 }]}>
              <Txt variant="caption" style={{ color: colors.white, fontWeight: '700' }}>+{others.length}</Txt>
            </View>
          ) : null}
          <Pressable onPress={quick} hitSlop={8} style={[s.pillAct, { borderColor: color }]} accessibilityLabel={item.ring ? item.seenLabel || 'Seen' : 'Open'}>
            <Icon name={item.ring ? 'eye-outline' : 'chevron-down'} size={15} color={color} />
          </Pressable>
        </Pressable>
      </Animated.View>
    );
  }

  return (
    <Animated.View style={[s.wrap, { top: insets.top + BELOW_TOP_BAR, transform: [{ translateY: slide }] }]} pointerEvents="box-none">
      <View style={[s.card, { borderLeftColor: color }]} {...pan.panHandlers}>
        {urgent ? (
          <Animated.View pointerEvents="none" style={[StyleSheet.absoluteFill, s.glow, { borderColor: color, opacity: glow }]} />
        ) : null}

        {/* who, when, what kind */}
        <View style={s.head}>
          <Avatar item={item} />
          <View style={{ flex: 1, marginLeft: 12 }}>
            <View style={s.metaRow}>
              {item.ring ? <RingDot color={color} /> : null}
              <Txt variant="label" style={{ color }} numberOfLines={1}>
                {labelOf(item)}{item.from ? ` · ${item.from}` : ''}
              </Txt>
            </View>
            <Txt variant="caption" numberOfLines={1}>
              {relativeAgo(item.fireAt)}
              {item.priority === 'URGENT' || item.priority === 'HIGH' ? ` · ${priorityLabel[item.priority]}` : ''}
              {item.ring ? ' · rings until seen' : item.mode === 'silent' ? ' · silent' : ''}
              {item.followups ? ` · repeat ${item.followups}` : ''}
            </Txt>
          </View>
          {others.length ? (
            <View style={[s.countPill, { backgroundColor: color }]}>
              <Txt variant="caption" style={{ color: colors.white, fontWeight: '700' }}>+{others.length}</Txt>
            </View>
          ) : null}
          <Pressable onPress={() => setFolded(true)} hitSlop={10} style={s.fold} accessibilityLabel="Fold away">
            <Icon name="chevron-up" size={18} color={colors.faint} />
          </Pressable>
        </View>

        <Txt variant="heading" numberOfLines={3} style={{ marginTop: 10 }}>{title}</Txt>
        {!item.concealed && item.body && item.body !== item.title ? (
          <Txt variant="small" numberOfLines={3} style={{ marginTop: 4 }}>{item.body}</Txt>
        ) : null}
        {item.concealed ? (
          <Pressable onPress={engine.reveal} style={s.reveal}>
            <Icon name="eye-outline" size={16} color={colors.inkSoft} />
            <Txt variant="small" style={{ marginLeft: 6, color: colors.inkSoft }}>Tap to reveal</Txt>
          </Pressable>
        ) : null}

        <View style={s.actions}>
          {item.question?.kind === 'approve' ? (
            // An approval asked of him, with the amount in the line: one tap decides, and whoever asked is told.
            <>
              <Button title={later} kind="ghost" compact onPress={() => engine.snooze(item).catch(() => undefined)} />
              <Button title="Decline" kind="danger" compact onPress={() => engine.answerQuestion(item, item.question!, false).catch(() => undefined)} style={{ marginLeft: 6 }} />
              <Button title="Approve" icon="checkmark" compact onPress={() => engine.answerQuestion(item, item.question!, true).catch(() => undefined)} style={{ marginLeft: 6 }} />
            </>
          ) : item.question?.kind === 'signout' ? (
            // "Signed in on another phone — not you?": the sign-out of that phone is one tap away.
            <>
              <Button title="It's me" kind="ghost" compact onPress={() => engine.answerQuestion(item, item.question!, false).catch(() => undefined)} />
              <Button title="Sign it out" kind="danger" compact icon="log-out-outline" onPress={() => engine.answerQuestion(item, item.question!, true).catch(() => undefined)} style={{ marginLeft: 6 }} />
            </>
          ) : isRequest ? (
            <>
              <Button title={later} kind="ghost" compact onPress={() => engine.snooze(item).catch(() => undefined)} />
              <Button title="Decline" kind="danger" compact onPress={() => engine.decideRequest(item, false)} style={{ marginLeft: 6 }} />
              <Button title="Accept" icon="checkmark" compact onPress={() => engine.decideRequest(item, true)} style={{ marginLeft: 6 }} />
            </>
          ) : item.ring ? (
            // Ring until seen: Seen stops the ringing everywhere and tells whoever sent it (for work, JARVIS then
            // asks by when). Later also counts as seen; it comes back at that time.
            <>
              <Button title={later} kind="ghost" compact onPress={() => engine.snooze(item).catch(() => undefined)} />
              <Button title={item.seenLabel || 'Seen'} icon="eye-outline" compact onPress={() => engine.markSeen(item)} style={{ marginLeft: 6 }} />
            </>
          ) : item.source === 'reminder' ? (
            <>
              <Button title={later} kind="ghost" compact onPress={() => engine.snooze(item).catch(() => undefined)} />
              <Button title="Done" kind="secondary" compact icon="checkmark-done" onPress={() => engine.complete(item)} style={{ marginLeft: 6 }} />
              <Button title="Got it" compact onPress={() => engine.acknowledge(item)} style={{ marginLeft: 6 }} />
            </>
          ) : (
            <Button title="Got it" compact onPress={() => engine.acknowledge(item)} />
          )}
        </View>
      </View>

      {/* the rest, as a stack beneath: most urgent first, each with its own quick Seen */}
      {others.length ? (
        <View style={s.stack}>
          <View style={s.stackHead}>
            <Txt variant="label" style={{ color: colors.inkSoft }}>ALSO WAITING · {others.length}</Txt>
            {others.length > MAX_ROWS ? (
              <Pressable onPress={() => setShowAll((v) => !v)} hitSlop={8}>
                <Txt variant="caption" style={{ color: colors.gold }}>{showAll ? 'Fewer' : `All ${others.length}`}</Txt>
              </Pressable>
            ) : null}
          </View>
          {rows.map((w) => {
            const c = priorityColor[w.priority];
            return (
              <Pressable key={w.key} onPress={() => engine.focus(w)} style={({ pressed }) => [s.row, pressed && { opacity: 0.7 }]}>
                <View style={[s.bar, { backgroundColor: c }]} />
                <Avatar item={w} size={26} />
                <View style={{ flex: 1, marginLeft: 10 }}>
                  <Txt variant="small" numberOfLines={1} style={{ fontWeight: '600' }}>
                    {w.concealed ? 'Private reminder' : w.title}
                  </Txt>
                  <Txt variant="caption" numberOfLines={1}>
                    {labelOf(w)}{w.from ? ` · ${w.from}` : ''} · {relativeAgo(w.fireAt)}
                    {w.priority === 'URGENT' || w.priority === 'HIGH' ? ` · ${priorityLabel[w.priority]}` : ''}
                  </Txt>
                </View>
                {w.ring ? (
                  <Pressable onPress={() => engine.markSeen(w)} hitSlop={8} style={[s.quickSeen, { borderColor: c }]} accessibilityLabel={w.seenLabel || 'Seen'}>
                    <Icon name="eye-outline" size={15} color={c} />
                  </Pressable>
                ) : (
                  <Icon name="chevron-forward" size={16} color={colors.faint} />
                )}
              </Pressable>
            );
          })}
          {more > 0 ? <Txt variant="caption" style={{ marginTop: 4, marginLeft: 14 }}>and {more} more</Txt> : null}
        </View>
      ) : null}
    </Animated.View>
  );
}

const s = StyleSheet.create({
  wrap: { position: 'absolute', left: 12, right: 12, zIndex: 50 },
  card: {
    backgroundColor: colors.surface,
    borderRadius: radius.lg,
    borderLeftWidth: 4,
    padding: 14,
    ...shadow,
    shadowOpacity: 0.2,
    elevation: 12,
  },
  glow: { borderRadius: radius.lg, borderWidth: 3 },
  head: { flexDirection: 'row', alignItems: 'center' },
  metaRow: { flexDirection: 'row', alignItems: 'center' },
  avatar: { alignItems: 'center', justifyContent: 'center' },
  dot: { width: 8, height: 8, borderRadius: 4, marginRight: 6 },
  countPill: { minWidth: 30, height: 22, borderRadius: 11, paddingHorizontal: 8, alignItems: 'center', justifyContent: 'center', marginLeft: 8 },
  fold: { marginLeft: 6, width: 28, height: 28, alignItems: 'center', justifyContent: 'center' },
  pill: {
    flexDirection: 'row',
    alignItems: 'center',
    alignSelf: 'center',
    maxWidth: '100%',
    backgroundColor: colors.surface,
    borderRadius: 24,
    borderWidth: 1.5,
    paddingLeft: 6,
    paddingRight: 6,
    paddingVertical: 6,
    ...shadow,
    shadowOpacity: 0.18,
    elevation: 10,
  },
  pillAct: { width: 30, height: 30, borderRadius: 15, borderWidth: 1.5, alignItems: 'center', justifyContent: 'center', marginLeft: 8 },
  reveal: { flexDirection: 'row', alignItems: 'center', marginTop: 10, marginLeft: 46 },
  actions: { flexDirection: 'row', justifyContent: 'flex-end', marginTop: 12 },
  // the stack beneath: a little narrower, so it reads as cards under the first one
  stack: {
    marginHorizontal: 8,
    backgroundColor: colors.surfaceAlt,
    borderBottomLeftRadius: radius.lg,
    borderBottomRightRadius: radius.lg,
    paddingHorizontal: 8,
    paddingTop: 8,
    paddingBottom: 6,
    ...shadow,
    shadowOpacity: 0.12,
    elevation: 6,
  },
  stackHead: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', paddingHorizontal: 6, marginBottom: 2 },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: colors.surface,
    borderRadius: radius.md,
    paddingVertical: 8,
    paddingRight: 10,
    paddingLeft: 10,
    marginTop: 6,
    overflow: 'hidden',
  },
  bar: { position: 'absolute', left: 0, top: 0, bottom: 0, width: 4 },
  quickSeen: { width: 32, height: 32, borderRadius: 16, borderWidth: 1.5, alignItems: 'center', justifyContent: 'center', marginLeft: 8 },
});
