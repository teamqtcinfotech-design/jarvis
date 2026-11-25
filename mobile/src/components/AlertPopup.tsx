/**
 * A reminder at its moment, or the free-time suggestion, in front of him (his ask, 25 Nov 2026: "give the pop up for
 * upcoming task"): a card over the top of whatever screen he is on, with the item's own buttons (Done / Later / Got it;
 * Not now / Put them in), a tap on the card opens the full list, and it goes by itself after a while. Never for
 * messages or tasks from people: those are counted first and read only when he says so.
 */
import React from 'react';
import { Pressable, StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { chipOf, useAlertEngine } from '@/lib/alertEngine';
import { relativeAgo } from '@/lib/format';
import { colors, priorityColor, radius, shadow } from '@/lib/theme';

import { AlertActions, AlertAvatar, alertLabel } from './AlertBanner';
import { Icon, Txt } from './ui';

export function AlertPopup() {
  const engine = useAlertEngine();
  const insets = useSafeAreaInsets();
  const item = engine.popup;
  if (!item || engine.allVisible) return null;
  const color = priorityColor[item.priority];
  const open = () => {
    engine.dismissPopup();
    engine.setAllGroup(chipOf(item));
    engine.setAllVisible(true);
  };
  return (
    <View style={[s.wrap, { top: insets.top + 8 }]} pointerEvents="box-none">
      <Pressable onPress={open} style={[s.card, { borderLeftColor: color }]} accessibilityLabel="Open">
        <View style={s.head}>
          <AlertAvatar item={item} size={28} />
          <View style={{ flex: 1, marginLeft: 10 }}>
            <Txt variant="label" style={{ color }} numberOfLines={1}>{item.question?.kind === 'place' ? 'Free time' : alertLabel(item)}{item.from ? ` · ${item.from}` : ''}</Txt>
            <Txt variant="caption" numberOfLines={1}>{relativeAgo(item.fireAt)}</Txt>
          </View>
          <Pressable onPress={engine.dismissPopup} hitSlop={10} accessibilityLabel="Close">
            <Icon name="close" size={18} color={colors.inkSoft} />
          </Pressable>
        </View>
        <Txt variant="heading" numberOfLines={2} style={{ marginTop: 6 }}>{item.concealed ? 'Private reminder' : item.title}</Txt>
        {!item.concealed && item.body && item.body !== item.title ? <Txt variant="small" numberOfLines={3} style={{ marginTop: 2 }}>{item.body}</Txt> : null}
        <AlertActions item={item} />
      </Pressable>
    </View>
  );
}

const s = StyleSheet.create({
  wrap: { position: 'absolute', left: 12, right: 12, zIndex: 60 },
  card: { backgroundColor: colors.surface, borderRadius: radius.lg, borderLeftWidth: 4, padding: 14, ...shadow, shadowOpacity: 0.2, elevation: 10 },
  head: { flexDirection: 'row', alignItems: 'center' },
});
