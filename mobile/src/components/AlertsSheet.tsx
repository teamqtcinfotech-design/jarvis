/**
 * "Sab dikhao": everything waiting for him, in piles (approvals, requests, meetings, tasks, messages, updates), each
 * a card with its own buttons (Seen, Approve / Decline, Accept, Done, Later, Got it) and the microphone underneath,
 * so he can go through them by tap or by voice ("bijli wale ka approve kar do", "Sharmaji wali done", "ye Nikki ko
 * de do", "next"). A card leaves the list the moment it is settled.
 */
import React from 'react';
import { Modal, Pressable, ScrollView, StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { type AlertGroup, GROUP_ORDER, GROUP_WORDS, groupOf, useAlertEngine } from '@/lib/alertEngine';
import { useConversation } from '@/lib/conversation';
import { relativeAgo } from '@/lib/format';
import { colors, priorityColor, radius } from '@/lib/theme';

import { AlertActions, AlertAvatar, alertLabel } from './AlertBanner';
import { Orb } from './Orb';
import { Button, Icon, Txt } from './ui';

export function AlertsSheet() {
  const engine = useAlertEngine();
  const conv = useConversation();
  const insets = useSafeAreaInsets();
  const all = [...engine.waiting, ...engine.held.filter((h) => !engine.waiting.some((w) => w.key === h.key))];
  // Opened from a chip: only that chip's pile (tasks, meetings, messages, or the rest); from elsewhere: everything.
  const g = engine.allGroup;
  const items = all.filter((it) => !g || (g === 'other' ? !['task', 'meeting', 'message'].includes(groupOf(it)) : groupOf(it) === g));
  const piles = new Map<AlertGroup, typeof items>();
  for (const it of items) piles.set(groupOf(it), [...(piles.get(groupOf(it)) ?? []), it]);
  const heading = g && g !== 'other' ? `${items.length} ${items.length === 1 ? GROUP_WORDS[g].en[0] : GROUP_WORDS[g].en[1]} waiting` : g === 'other' ? `${items.length} alert${items.length === 1 ? '' : 's'} waiting` : `${items.length} waiting for you`;
  const close = () => {
    engine.setAllVisible(false);
    engine.setAllGroup(null);
  };
  return (
    <Modal visible={engine.allVisible} transparent animationType="slide" onRequestClose={close}>
      <Pressable style={s.backdrop} onPress={close} />
      <View style={[s.sheet, { paddingBottom: insets.bottom + 12 }]}>
        <View style={s.grabber} />
        <View style={{ flexDirection: 'row', alignItems: 'center' }}>
          <Txt variant="title" style={{ flex: 1 }}>{items.length ? heading : 'Nothing waiting'}</Txt>
          <Pressable onPress={close} hitSlop={10} accessibilityLabel="Close"><Icon name="close" size={22} color={colors.inkSoft} /></Pressable>
        </View>
        <Txt variant="caption" style={{ marginTop: 2 }}>Tap a button, or just say it: “bijli wale ka approve kar do”, “Sharmaji wali done”, “ye Nikki ko de do”, “next”.</Txt>
        <ScrollView style={{ marginTop: 10, maxHeight: 430 }} contentContainerStyle={{ paddingBottom: 6 }}>
          {GROUP_ORDER.map((g) => {
            const pile = piles.get(g);
            if (!pile?.length) return null;
            return (
              <View key={g}>
                <Txt variant="label" style={{ marginTop: 8, marginBottom: 6, color: colors.inkSoft }}>{GROUP_WORDS[g].title.toUpperCase()} · {pile.length}</Txt>
                {pile.map((item) => {
                  const color = priorityColor[item.priority];
                  const current = engine.current?.key === item.key;
                  return (
                    <View key={item.key} style={[s.card, { borderLeftColor: color }, current && s.cardCurrent]}>
                      <View style={{ flexDirection: 'row', alignItems: 'center' }}>
                        <AlertAvatar item={item} size={28} />
                        <View style={{ flex: 1, marginLeft: 10 }}>
                          <Txt variant="label" style={{ color }} numberOfLines={1}>{alertLabel(item)}{item.from ? ` · ${item.from}` : ''}</Txt>
                          <Txt variant="caption">{relativeAgo(item.fireAt)}{item.ring ? ' · rings until seen' : ''}</Txt>
                        </View>
                        {current ? <Icon name="volume-high-outline" size={16} color={colors.accent} /> : null}
                      </View>
                      <Txt variant="heading" style={{ marginTop: 6 }} numberOfLines={3}>{item.concealed ? 'Private reminder' : item.title}</Txt>
                      {!item.concealed && item.body && item.body !== item.title ? <Txt variant="small" style={{ marginTop: 2 }} numberOfLines={4}>{item.body}</Txt> : null}
                      {item.concealed && current ? (
                        <Pressable onPress={engine.reveal} style={{ flexDirection: 'row', alignItems: 'center', marginTop: 6 }}>
                          <Icon name="eye-outline" size={16} color={colors.inkSoft} />
                          <Txt variant="small" style={{ marginLeft: 6, color: colors.inkSoft }}>Tap to reveal</Txt>
                        </Pressable>
                      ) : null}
                      <AlertActions item={item} />
                    </View>
                  );
                })}
              </View>
            );
          })}
        </ScrollView>
        {items.length > 1 ? (
          // The whole pile at once, the same as his words "sab seen", "sab baad mein", "sab hata do".
          <View style={s.all}>
            <Txt variant="caption" style={{ flex: 1 }}>All {items.length}:</Txt>
            <Button title="Seen" kind="ghost" compact icon="eye-outline" onPress={() => { engine.seenAll(items).catch(() => undefined); }} />
            <Button title="Later" kind="ghost" compact icon="time-outline" onPress={() => { engine.snoozeAll().catch(() => undefined); }} />
            <Button title="Clear" kind="ghost" compact icon="close-circle-outline" onPress={() => { engine.dismissAll(items).catch(() => undefined); }} />
          </View>
        ) : null}
        <View style={s.foot}>
          <View style={{ flex: 1 }}>
            <Txt variant="small" numberOfLines={2} style={{ color: colors.inkSoft }}>{conv.status}</Txt>
            {items.length > 1 ? <Button title="Next" kind="ghost" compact icon="play-skip-forward-outline" onPress={engine.skip} style={{ alignSelf: 'flex-start', marginTop: 4 }} /> : null}
          </View>
          <Orb state={conv.orb} size={64} level={conv.level} onPress={() => conv.micPress()} />
        </View>
      </View>
    </Modal>
  );
}

const s = StyleSheet.create({
  backdrop: { flex: 1, backgroundColor: colors.overlay },
  sheet: { backgroundColor: colors.bg, borderTopLeftRadius: radius.xl, borderTopRightRadius: radius.xl, paddingHorizontal: 16, paddingTop: 10 },
  grabber: { alignSelf: 'center', width: 44, height: 5, borderRadius: 3, backgroundColor: colors.line, marginBottom: 12 },
  card: { backgroundColor: colors.surface, borderRadius: radius.md, borderLeftWidth: 4, padding: 12, marginBottom: 8 },
  cardCurrent: { borderWidth: 1, borderColor: colors.accent, borderLeftWidth: 4 },
  all: { flexDirection: 'row', alignItems: 'center', marginTop: 6, columnGap: 4 },
  foot: { flexDirection: 'row', alignItems: 'center', marginTop: 8 },
});
