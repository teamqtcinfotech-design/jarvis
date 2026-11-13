import React, { useEffect, useState } from 'react';
import { Animated, Pressable, StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { useAlertEngine } from '@/lib/alertEngine';
import { usePrefs } from '@/lib/prefs';
import { colors, kindIcon, priorityColor, priorityLabel, radius, shadow } from '@/lib/theme';

import { Button, Icon, Txt } from './ui';

export function AlertBanner() {
  const engine = useAlertEngine();
  const { prefs } = usePrefs();
  const item = engine.current;
  // "Later" = the person's usual snooze, learned from what they say by voice ("10 minute baad").
  const later = `Later · ${prefs.snoozeMinutes}m`;
  const insets = useSafeAreaInsets();
  const [slide] = useState(() => new Animated.Value(-260));
  const [glow] = useState(() => new Animated.Value(0));
  const flashing = Boolean(item?.personal);

  useEffect(() => {
    Animated.spring(slide, { toValue: item ? 0 : -260, useNativeDriver: true, friction: 9 }).start();
  }, [item, slide]);

  // A message or a task from one of the three keeps flashing until it is answered or tapped "Got it".
  useEffect(() => {
    if (!flashing) {
      glow.setValue(0);
      return undefined;
    }
    const loop = Animated.loop(Animated.sequence([
      Animated.timing(glow, { toValue: 1, duration: 550, useNativeDriver: true }),
      Animated.timing(glow, { toValue: 0, duration: 550, useNativeDriver: true }),
    ]));
    loop.start();
    return () => loop.stop();
  }, [flashing, glow]);

  if (!item) return null;
  const isRequest = item.alertKind === 'NEW_REQUEST' && Boolean(item.requestId);
  const color = priorityColor[item.priority];
  const title = item.concealed ? (item.priority === 'URGENT' ? 'Urgent private reminder' : 'Private reminder') : item.title;

  return (
    <Animated.View style={[s.wrap, { top: insets.top + 8, transform: [{ translateY: slide }] }]} pointerEvents="box-none">
      <View style={[s.card, { borderLeftColor: color }]}>
        {flashing ? (
          <Animated.View pointerEvents="none" style={[StyleSheet.absoluteFill, s.flash, { borderColor: color, opacity: glow }]} />
        ) : null}
        <View style={s.head}>
          <View style={[s.iconWrap, { backgroundColor: color }]}>
            <Icon
              name={isRequest ? 'mail-unread-outline' : item.source === 'server' ? 'notifications-outline' : kindIcon[item.kind ?? 'TASK'] ?? 'alarm-outline'}
              size={18}
              color={colors.white}
            />
          </View>
          <View style={{ flex: 1 }}>
            <Txt variant="label" style={{ color }}>
              {isRequest ? 'Incoming task' : item.alertKind === 'TASK_ASSIGNED' ? 'New task' : item.personal ? 'Message' : item.source === 'server' ? 'Update' : `${priorityLabel[item.priority]} reminder`}
              {item.mode === 'silent' ? ' · silent' : ''}
              {item.followups ? ` · repeat ${item.followups}` : ''}
            </Txt>
            <Txt variant="heading" numberOfLines={3} style={{ marginTop: 2 }}>{title}</Txt>
            {!item.concealed && item.body ? <Txt variant="small" numberOfLines={2}>{item.body}</Txt> : null}
          </View>
          {engine.queued > 0 ? <Txt variant="caption">+{engine.queued}</Txt> : null}
        </View>
        {item.concealed ? (
          <Pressable onPress={engine.reveal} style={s.reveal}>
            <Icon name="eye-outline" size={16} color={colors.inkSoft} />
            <Txt variant="small" style={{ marginLeft: 6, color: colors.inkSoft }}>Tap to reveal</Txt>
          </Pressable>
        ) : null}
        <View style={s.actions}>
          {isRequest ? (
            <>
              <Button title={later} kind="ghost" compact onPress={() => engine.snooze(item).catch(() => undefined)} />
              <Button title="Decline" kind="danger" compact onPress={() => engine.decideRequest(item, false)} style={{ marginLeft: 6 }} />
              <Button title="Accept" icon="checkmark" compact onPress={() => engine.decideRequest(item, true)} style={{ marginLeft: 6 }} />
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
  head: { flexDirection: 'row', alignItems: 'flex-start' },
  flash: { borderRadius: radius.lg, borderWidth: 3 },
  iconWrap: { width: 34, height: 34, borderRadius: 17, alignItems: 'center', justifyContent: 'center', marginRight: 12 },
  reveal: { flexDirection: 'row', alignItems: 'center', marginTop: 10, marginLeft: 46 },
  actions: { flexDirection: 'row', justifyContent: 'flex-end', marginTop: 12 },
});
