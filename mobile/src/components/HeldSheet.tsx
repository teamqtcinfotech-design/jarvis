import React from 'react';
import { Modal, Pressable, ScrollView, StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { EngineItem, useAlertEngine } from '@/lib/alertEngine';
import { colors, priorityColor, radius } from '@/lib/theme';

import { Button, PriorityTag, Txt } from './ui';

function HeldRow({ item }: { item: EngineItem }) {
  const engine = useAlertEngine();
  const isRequest = item.alertKind === 'NEW_REQUEST' && Boolean(item.requestId);
  return (
    <View style={[s.row, { borderLeftColor: priorityColor[item.priority] }]}>
      <View style={{ flex: 1 }}>
        <View style={{ flexDirection: 'row', alignItems: 'center' }}>
          <PriorityTag priority={item.priority} />
          <Txt variant="caption">{isRequest ? 'Incoming task' : item.source === 'server' ? 'Update' : 'Reminder'}</Txt>
        </View>
        <Txt variant="heading" style={{ marginTop: 6 }}>{item.title}</Txt>
        {item.body ? <Txt variant="small">{item.body}</Txt> : null}
      </View>
      <View style={s.rowActions}>
        {isRequest ? (
          <>
            <Button title="Accept" compact onPress={() => engine.decideRequest(item, true)} />
            <Button title="Decline" compact kind="ghost" onPress={() => engine.decideRequest(item, false)} />
          </>
        ) : (
          <>
            {item.ref ? <Button title="Done" compact kind="secondary" onPress={() => engine.complete(item)} /> : null}
            <Button title="OK" compact kind="ghost" onPress={() => engine.acknowledge(item)} />
          </>
        )}
      </View>
    </View>
  );
}

export function HeldSheet() {
  const engine = useAlertEngine();
  const insets = useSafeAreaInsets();
  const items = engine.held;
  return (
    <Modal visible={engine.heldVisible && items.length > 0} transparent animationType="slide" onRequestClose={() => engine.setHeldVisible(false)}>
      <Pressable style={s.backdrop} onPress={() => engine.setHeldVisible(false)} />
      <View style={[s.sheet, { paddingBottom: insets.bottom + 16 }]}>
        <View style={s.grabber} />
        <Txt variant="label">While you were away</Txt>
        <Txt variant="title" style={{ marginTop: 4 }}>
          {items.length} alert{items.length > 1 ? 's' : ''} held for you
        </Txt>
        <Txt variant="small" style={{ marginTop: 4 }}>
          JARVIS stayed silent while you were away from the phone, in a meeting or in quiet hours.
        </Txt>
        <ScrollView style={{ marginTop: 14, maxHeight: 440 }}>
          {items.map((item) => (
            <HeldRow key={item.key} item={item} />
          ))}
        </ScrollView>
        <View style={{ flexDirection: 'row', marginTop: 12 }}>
          <Button title="Close" kind="secondary" onPress={() => engine.setHeldVisible(false)} style={{ flex: 1, marginRight: 8 }} />
          <Button title="Mark all seen" onPress={engine.acknowledgeAllHeld} style={{ flex: 1 }} />
        </View>
      </View>
    </Modal>
  );
}

const s = StyleSheet.create({
  backdrop: { flex: 1, backgroundColor: colors.overlay },
  sheet: {
    backgroundColor: colors.bg,
    borderTopLeftRadius: radius.xl,
    borderTopRightRadius: radius.xl,
    paddingHorizontal: 20,
    paddingTop: 10,
  },
  grabber: { alignSelf: 'center', width: 44, height: 5, borderRadius: 3, backgroundColor: colors.line, marginBottom: 14 },
  row: {
    flexDirection: 'row',
    backgroundColor: colors.surface,
    borderRadius: radius.md,
    borderLeftWidth: 4,
    padding: 12,
    marginBottom: 8,
  },
  rowActions: { justifyContent: 'center', marginLeft: 8, rowGap: 6 },
});
