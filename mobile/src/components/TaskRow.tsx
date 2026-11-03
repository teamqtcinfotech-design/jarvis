import React from 'react';
import { Pressable, StyleSheet, View } from 'react-native';

import { timeOf } from '@/lib/format';
import { colors, kindIcon, priorityColor, radius } from '@/lib/theme';
import type { TaskItem } from '@/lib/types';

import { Icon, PriorityTag, Tag, Txt } from './ui';

// TaskPanel labels (todo / working / waiting / done) plus delivery states.
const STATUS_STYLE: Record<string, { color: string; bg: string }> = {
  'Not started': { color: colors.info, bg: colors.infoSoft },
  Working: { color: colors.warn, bg: colors.warnSoft },
  Hold: { color: colors.danger, bg: colors.dangerSoft },
  Completed: { color: colors.success, bg: colors.successSoft },
  'Pending confirmation': { color: colors.warn, bg: colors.warnSoft },
  'Not assigned': { color: colors.danger, bg: colors.dangerSoft },
  Assigned: { color: colors.info, bg: colors.infoSoft },
};

export function TaskRow({ item, onPress, showStaffStatus }: { item: TaskItem; onPress?: () => void; showStaffStatus?: boolean }) {
  const when = item.scheduled_start
    ? `${timeOf(item.scheduled_start)}${item.scheduled_end ? ` – ${timeOf(item.scheduled_end)}` : ''}`
    : item.due_label;
  const staff = item.assignee?.type === 'STAFF' || item.assignee?.type === 'PA' ? item.assignee.staff_name : null;
  const statusLabel = item.status_label || '';
  const status = statusLabel ? { label: statusLabel, ...(STATUS_STYLE[statusLabel] ?? STATUS_STYLE.Assigned) } : null;
  return (
    <Pressable onPress={onPress} style={({ pressed }) => [s.row, pressed && { opacity: 0.8 }]}>
      <View style={[s.bar, { backgroundColor: priorityColor[item.priority] }]} />
      <View style={s.icon}>
        <Icon name={kindIcon[item.kind] ?? 'ellipse-outline'} size={18} color={colors.inkSoft} />
      </View>
      <View style={{ flex: 1 }}>
        <Txt variant="heading" numberOfLines={2} style={item.status === 'COMPLETED' && s.done}>
          {item.title}
        </Txt>
        <View style={s.meta}>
          {item.overdue ? <Tag label="Overdue" color={colors.danger} bg={colors.dangerSoft} /> : null}
          {item.priority === 'URGENT' || item.priority === 'HIGH' ? <PriorityTag priority={item.priority} /> : null}
          {item.scope === 'private' ? <Tag label="Private" icon="lock-closed" color={colors.inkSoft} /> : null}
          {item.scope === 'shared' && item.assignee?.type === 'MD' && item.created_by ? (
            <Tag label={`From ${item.created_by}`} color={colors.gold} bg={colors.goldSoft} />
          ) : null}
          {staff ? <Tag label={staff} icon="person-outline" /> : null}
          {showStaffStatus && status ? <Tag label={status.label} color={status.color} bg={status.bg} /> : null}
          {showStaffStatus && item.remote_task_id ? <Tag label={item.remote_task_id} color={colors.faint} /> : null}
        </View>
      </View>
      {when ? <Txt variant="small" style={s.when}>{when}</Txt> : null}
    </Pressable>
  );
}

const s = StyleSheet.create({
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: colors.surface,
    borderRadius: radius.md,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.line,
    paddingVertical: 12,
    paddingRight: 12,
    marginBottom: 8,
    overflow: 'hidden',
  },
  bar: { width: 4, alignSelf: 'stretch', marginRight: 10, borderTopRightRadius: 2, borderBottomRightRadius: 2 },
  icon: { width: 30, alignItems: 'center', marginRight: 6 },
  meta: { flexDirection: 'row', flexWrap: 'wrap', marginTop: 5, rowGap: 4 },
  when: { marginLeft: 10, maxWidth: 110, textAlign: 'right', color: colors.inkSoft, fontWeight: '600' },
  done: { textDecorationLine: 'line-through', color: colors.muted },
});
