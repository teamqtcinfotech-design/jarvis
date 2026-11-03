import React from 'react';
import { Pressable, StyleSheet, View } from 'react-native';

import { useAlertEngine } from '@/lib/alertEngine';
import { presenceLabel } from '@/lib/presence';
import { colors, radius } from '@/lib/theme';

import { Icon, Txt } from './ui';

const DOT: Record<string, string> = {
  present: colors.success,
  away: colors.faint,
  facedown: colors.inkSoft,
  meeting: colors.gold,
  quiet: colors.inkSoft,
};

export function PresenceBar() {
  const engine = useAlertEngine();
  const state = engine.presence.state;
  const info = presenceLabel[state];
  const heldCount = engine.held.length;

  return (
    <View style={s.wrap}>
      <View style={s.left}>
        <View style={[s.dot, { backgroundColor: DOT[state] }]} />
        <View style={{ flex: 1 }}>
          <Txt variant="heading" style={{ fontSize: 14 }}>{info.title}</Txt>
          <Txt variant="caption" numberOfLines={1}>{info.detail}</Txt>
        </View>
      </View>
      {heldCount > 0 ? (
        <Pressable onPress={() => engine.setHeldVisible(true)} style={s.held} accessibilityLabel="Show held alerts">
          <Icon name="notifications-outline" size={15} color={colors.white} />
          <Txt style={s.heldText}>{heldCount} held</Txt>
        </Pressable>
      ) : null}
      <Pressable
        onPress={() => engine.setMeetingMode(!engine.meetingMode)}
        style={[s.meeting, engine.meetingMode && s.meetingOn]}
        accessibilityLabel="Toggle meeting mode"
      >
        <Icon name={engine.meetingMode ? 'volume-mute' : 'people-outline'} size={15} color={engine.meetingMode ? colors.white : colors.ink} />
        <Txt style={[s.meetingText, engine.meetingMode && { color: colors.white }]}>
          {engine.meetingMode ? 'Meeting on' : 'Meeting'}
        </Txt>
      </Pressable>
    </View>
  );
}

const s = StyleSheet.create({
  wrap: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: colors.surface,
    borderRadius: radius.md,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.line,
    paddingVertical: 10,
    paddingHorizontal: 12,
    marginTop: 16,
  },
  left: { flexDirection: 'row', alignItems: 'center', flex: 1 },
  dot: { width: 10, height: 10, borderRadius: 5, marginRight: 10 },
  held: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: colors.danger,
    paddingHorizontal: 10,
    paddingVertical: 6,
    borderRadius: 999,
    marginRight: 8,
  },
  heldText: { color: colors.white, fontSize: 12, fontWeight: '700', marginLeft: 5 },
  meeting: {
    flexDirection: 'row',
    alignItems: 'center',
    borderWidth: 1,
    borderColor: colors.line,
    paddingHorizontal: 10,
    paddingVertical: 6,
    borderRadius: 999,
  },
  meetingOn: { backgroundColor: colors.gold, borderColor: colors.gold },
  meetingText: { fontSize: 12, fontWeight: '600', marginLeft: 5, color: colors.ink },
});
