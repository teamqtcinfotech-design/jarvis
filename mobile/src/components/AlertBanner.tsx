/**
 * What is waiting for him right now, without hiding anything:
 *
 *  - On the home screen it sits inside the four round chips (PhoneHome): task alerts under Tasks, meeting alerts
 *    under Meetings, messages under Messages, the rest under Alerts; a tap opens that pile (AlertsSheet), where each
 *    card has its one or two buttons (Seen / Later for work and messages that ring until seen; Approve / Decline for
 *    an approval; Accept / Decline for a request; Done / Got it for a reminder). Nothing floats over the page.
 *  - On every other screen it is a slim pill at the BOTTOM (above the phone's own bar): who, what, +N; a tap opens
 *    the full list (AlertsSheet), the eye is a quick Seen.
 *  - A ringing item shows a pulsing dot. Nothing here is a plain "Got it" for work or a message from the team: those
 *    stay until Seen.
 */
import { usePathname } from 'expo-router';
import React, { useEffect, useState } from 'react';
import { Animated, Pressable, StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { type EngineItem, useAlertEngine } from '@/lib/alertEngine';
import { usePrefs } from '@/lib/prefs';
import { colors, kindIcon, priorityColor, priorityLabel, shadow } from '@/lib/theme';

import { Button, Icon, Txt } from './ui';

/** "Incoming task" / "New task" / "Approval" / "Meeting" / "Message" / "Update" / "Urgent reminder". */
export function alertLabel(item: EngineItem): string {
  if (item.alertKind === 'NEW_REQUEST' && item.requestId) return 'Incoming task';
  if (item.question?.kind === 'approve') return 'Approval';
  if (item.alertKind === 'TASK_ASSIGNED') return /^meeting with/i.test(item.title) ? 'Meeting' : 'New task';
  if (item.refType === 'message') return 'Message';
  if (item.source === 'server') return 'Update';
  return `${priorityLabel[item.priority]} reminder`;
}

function iconOf(item: EngineItem): string {
  if (item.alertKind === 'NEW_REQUEST' && item.requestId) return 'mail-unread-outline';
  if (item.refType === 'message') return 'chatbubble-ellipses-outline';
  if (item.source === 'server') return 'notifications-outline';
  return kindIcon[item.kind ?? 'TASK'] ?? 'alarm-outline';
}

/** The first letter of whoever it is from, in a round badge of the item's priority colour. */
export function AlertAvatar({ item, size = 34 }: { item: EngineItem; size?: number }) {
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

/** The buttons an alert answers with: Seen / Later for work and messages that ring, Approve / Decline for an
 *  approval, Accept / Decline for a request, Done / Later / Got it for a reminder, Got it for news. The same on the
 *  home card and on the "show all" sheet. */
export function AlertActions({ item }: { item: EngineItem }) {
  const engine = useAlertEngine();
  const { prefs } = usePrefs();
  const later = `Later · ${prefs.snoozeMinutes}m`;
  const isRequest = item.alertKind === 'NEW_REQUEST' && Boolean(item.requestId);
  return (
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
  );
}

/** Away from the home screen: a slim pill at the bottom; it never covers the page. */
export function AlertBanner() {
  const engine = useAlertEngine();
  const pathname = usePathname();
  const insets = useSafeAreaInsets();
  const item = engine.current;
  if (!item || pathname === '/' || engine.allVisible) return null;
  const color = priorityColor[item.priority];
  const others = engine.waiting.filter((w) => w.key !== item.key).length;
  const quick = () => (item.ring ? engine.markSeen(item) : engine.setAllVisible(true));
  return (
    <View style={[s.wrap, { bottom: insets.bottom + 14 }]} pointerEvents="box-none">
      <Pressable onPress={() => engine.setAllVisible(true)} style={({ pressed }) => [s.pill, { borderColor: color }, pressed && { opacity: 0.85 }]} accessibilityLabel="Show what is waiting">
        <AlertAvatar item={item} size={24} />
        {item.ring ? <View style={{ marginLeft: 8 }}><RingDot color={color} /></View> : null}
        <Txt variant="small" numberOfLines={1} style={{ flex: 1, marginLeft: item.ring ? 2 : 8, fontWeight: '600', color: colors.text }}>
          {item.from ? `${item.from} · ` : ''}{item.concealed ? 'Private reminder' : item.title}
        </Txt>
        {others ? (
          <View style={[s.countPill, { backgroundColor: color, marginLeft: 6 }]}>
            <Txt variant="caption" style={{ color: colors.white, fontWeight: '700' }}>+{others}</Txt>
          </View>
        ) : null}
        <Pressable onPress={quick} hitSlop={8} style={[s.pillAct, { borderColor: color }]} accessibilityLabel={item.ring ? item.seenLabel || 'Seen' : 'Open'}>
          <Icon name={item.ring ? 'eye-outline' : 'list-outline'} size={15} color={color} />
        </Pressable>
      </Pressable>
    </View>
  );
}

const s = StyleSheet.create({
  wrap: { position: 'absolute', left: 12, right: 12, zIndex: 50 },
  avatar: { alignItems: 'center', justifyContent: 'center' },
  dot: { width: 8, height: 8, borderRadius: 4, marginRight: 6 },
  countPill: { minWidth: 30, height: 22, borderRadius: 11, paddingHorizontal: 8, alignItems: 'center', justifyContent: 'center', marginLeft: 8 },
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
  actions: { flexDirection: 'row', justifyContent: 'flex-end', marginTop: 12 },
});
