import React, { useEffect, useState } from 'react';
import { Animated, Dimensions, Easing, Modal, Pressable, ScrollView, StyleSheet, Switch, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { colors, fonts, radius } from '@/lib/theme';

import { Icon, Txt } from './ui';

export interface DrawerItem {
  key: string;
  label: string;
  icon: string;
  count?: number;
  tone?: string;
  detail?: string;
}

const WIDTH = Math.min(320, Dimensions.get('window').width * 0.84);

interface Props {
  open: boolean;
  onClose: () => void;
  title: string;
  subtitle: string;
  items: DrawerItem[];
  onSelect: (key: string) => void;
  meetingMode: boolean;
  onMeetingMode: (on: boolean) => void;
  presence: string;
  /** "In a meeting" comes from the diary, not the switch: switching off overrides it until the meeting ends. */
  meetingFromDiary?: boolean;
}

/** Slide-in side menu: the task lists live here, one tap away from the orb. */
export function Drawer({ open, onClose, title, subtitle, items, onSelect, meetingMode, onMeetingMode, presence, meetingFromDiary }: Props) {
  const insets = useSafeAreaInsets();
  const [x] = useState(() => new Animated.Value(-WIDTH));
  const [visible, setVisible] = useState(open);

  useEffect(() => {
    if (open) {
      const kick = setTimeout(() => setVisible(true), 0);
      Animated.timing(x, { toValue: 0, duration: 220, easing: Easing.out(Easing.cubic), useNativeDriver: true }).start();
      return () => clearTimeout(kick);
    }
    // Slide out first, then unmount (the callback runs after the animation, not during render).
    Animated.timing(x, { toValue: -WIDTH, duration: 180, easing: Easing.in(Easing.cubic), useNativeDriver: true }).start(
      ({ finished }) => finished && setVisible(false),
    );
    return undefined;
  }, [open, x]);

  if (!visible) return null;
  return (
    <Modal visible transparent animationType="none" onRequestClose={onClose} statusBarTranslucent>
      <Pressable style={s.backdrop} onPress={onClose} />
      <Animated.View style={[s.panel, { width: WIDTH, paddingTop: insets.top + 14, paddingBottom: insets.bottom + 14, transform: [{ translateX: x }] }]}>
        <View style={s.head}>
          <Txt style={s.brand}>JARVIS</Txt>
          <Txt variant="heading" style={{ marginTop: 6 }}>{title}</Txt>
          <Txt variant="small">{subtitle}</Txt>
        </View>
        <View style={s.meeting}>
          <View style={{ flex: 1 }}>
            <Txt variant="heading" style={{ fontSize: 14 }}>Meeting mode</Txt>
            <Txt variant="caption">
              {meetingMode
                ? meetingFromDiary ? 'From your diary · silent until it ends. Switch off to talk now.' : 'JARVIS stays silent and vibrates only'
                : presence}
            </Txt>
          </View>
          <Switch value={meetingMode} onValueChange={onMeetingMode} trackColor={{ true: colors.gold }} />
        </View>
        <ScrollView contentContainerStyle={{ paddingVertical: 6 }}>
          {items.map((it) => (
            <Pressable key={it.key} onPress={() => onSelect(it.key)} style={({ pressed }) => [s.item, pressed && { backgroundColor: colors.surfaceAlt }]}>
              <Icon name={it.icon} size={20} color={it.tone && it.count ? it.tone : colors.inkSoft} />
              <View style={{ flex: 1, marginLeft: 14 }}>
                <Txt variant="body" style={{ color: colors.text }}>{it.label}</Txt>
                {it.detail ? <Txt variant="caption">{it.detail}</Txt> : null}
              </View>
              {it.count !== undefined ? (
                <View style={[s.count, it.tone && it.count ? { backgroundColor: it.tone } : null]}>
                  <Txt variant="caption" style={{ color: it.tone && it.count ? colors.white : colors.muted, fontWeight: '700' }}>{it.count}</Txt>
                </View>
              ) : null}
            </Pressable>
          ))}
        </ScrollView>
      </Animated.View>
    </Modal>
  );
}

const s = StyleSheet.create({
  backdrop: { position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, backgroundColor: colors.overlay },
  panel: {
    position: 'absolute',
    top: 0,
    bottom: 0,
    left: 0,
    backgroundColor: colors.bg,
    borderTopRightRadius: radius.xl,
    borderBottomRightRadius: radius.xl,
    paddingHorizontal: 18,
  },
  head: { paddingBottom: 14, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: colors.line },
  brand: { fontFamily: fonts.serif, fontSize: 18, fontWeight: '700', lineHeight: 26, letterSpacing: 2, color: colors.ink },
  meeting: { flexDirection: 'row', alignItems: 'center', paddingVertical: 12, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: colors.line },
  item: { flexDirection: 'row', alignItems: 'center', paddingVertical: 13, paddingHorizontal: 6, borderRadius: radius.md },
  count: { minWidth: 26, paddingHorizontal: 8, paddingVertical: 2, borderRadius: 999, backgroundColor: colors.surfaceAlt, alignItems: 'center' },
});
