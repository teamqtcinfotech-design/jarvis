import React from 'react';
import { Pressable, StyleSheet, View } from 'react-native';

import { timeOf } from '@/lib/format';
import { colors, kindIcon, kindLabel, priorityColor, radius } from '@/lib/theme';
import type { TaskItem } from '@/lib/types';

import { Icon, PriorityTag, Tag, Txt } from './ui';

// Server status labels (staff app: todo / working / waiting / done, plus sending states) in everyday words.
const STATUS_STYLE: Record<string, { label: string; color: string; bg: string }> = {
  'Not started': { label: 'Not started', color: colors.info, bg: colors.infoSoft },
  Working: { label: 'In progress', color: colors.warn, bg: colors.warnSoft },
  Hold: { label: 'On hold', color: colors.danger, bg: colors.dangerSoft },
  Completed: { label: 'Done', color: colors.success, bg: colors.successSoft },
  'Pending confirmation': { label: 'Being sent', color: colors.warn, bg: colors.warnSoft },
  'Not assigned': { label: "Couldn't send", color: colors.danger, bg: colors.dangerSoft },
  Assigned: { label: 'Given', color: colors.info, bg: colors.infoSoft },
};

export function plainStatus(label: string) {
  return STATUS_STYLE[label] ?? { label, color: colors.info, bg: colors.infoSoft };
}

/** "every month on the 10th" -> "Monthly · 10th"; "every Monday and Thursday" -> "Mon, Thu"; short enough for a tag. */
export function shortRepeat(label: string): string {
  const m = label.match(/^every month on the (\w+)$/);
  if (m) return `Monthly · ${m[1]}`;
  if (label === 'every day') return 'Daily';
  if (label === 'every working day') return 'Weekdays';
  if (label === 'every week') return 'Weekly';
  if (label.startsWith('every year')) return 'Yearly';
  if (label.startsWith('every ')) return label.slice(6).split(' and ').map((d) => d.slice(0, 3)).join(', ');
  return label;
}

/** How far along a staff / PA task is, from its status words. */
export function progressOf(item: TaskItem): number {
  if (item.status === 'COMPLETED') return 100;
  const remote: Record<string, number> = { todo: 0, working: 50, waiting: 50, done: 100 };
  if (item.remote_status && item.remote_status in remote) return remote[item.remote_status];
  return item.status === 'IN_PROGRESS' || item.status === 'BLOCKED' ? 50 : 0;
}

export function TaskRow({ item, onPress, showStaffStatus, flat, last }: {
  item: TaskItem; onPress?: () => void; showStaffStatus?: boolean;
  /** Inside a card: no own border, a hairline between rows instead. */
  flat?: boolean; last?: boolean;
}) {
  const when = item.scheduled_start
    ? `${timeOf(item.scheduled_start)}${item.scheduled_end ? ` – ${timeOf(item.scheduled_end)}` : ''}`
    : item.due_label;
  const staff = ['STAFF', 'USER', 'PA', 'MG'].includes(item.assignee?.type ?? '') ? item.assignee.staff_name : null;
  const status = item.status_label ? plainStatus(item.status_label) : null;
  return (
    <Pressable onPress={onPress} style={({ pressed }) => [s.row, flat && s.rowFlat, flat && !last && s.rowLine, pressed && { opacity: 0.8 }]}>
      <View style={[s.bar, { backgroundColor: priorityColor[item.priority] }, flat && s.barFlat]} />
      <View style={[s.icon, { backgroundColor: item.scope === 'private' ? colors.surfaceAlt : colors.accentSoft }]}>
        <Icon name={kindIcon[item.kind] ?? 'ellipse-outline'} size={16} color={item.scope === 'private' ? colors.inkSoft : colors.accent} />
      </View>
      <View style={{ flex: 1 }}>
        <Txt variant="heading" numberOfLines={2} style={item.status === 'COMPLETED' && s.done}>
          {item.title}
        </Txt>
        <View style={s.meta}>
          {/* A meeting or a call is not a task: it says so on the row, so the lists read at a glance. */}
          {item.kind === 'MEETING' || item.kind === 'CALL' ? (
            <Tag label={kindLabel[item.kind]} icon={kindIcon[item.kind]} color={colors.accentDeep} bg={colors.accentSoft} />
          ) : null}
          {item.overdue ? <Tag label="Late" color={colors.danger} bg={colors.dangerSoft} /> : null}
          {item.priority === 'URGENT' || item.priority === 'HIGH' ? <PriorityTag priority={item.priority} /> : null}
          {item.scope === 'private' ? <Tag label="Private" icon="lock-closed" color={colors.inkSoft} /> : null}
          {item.scope === 'shared' && item.assignee?.type === 'MD' && item.created_by ? (
            <Tag label={`From ${item.created_by}`} color={colors.gold} bg={colors.goldSoft} />
          ) : null}
          {staff ? <Tag label={staff} icon="person-outline" /> : null}
          {item.section ? <Tag label={item.section} icon="folder-outline" color={item.section_colour || colors.accent} bg={colors.accentSoft} /> : null}
          {showStaffStatus && status ? <Tag label={status.label} color={status.color} bg={status.bg} /> : null}
          {/* Nov 2026: an approval's amount, a repeat, the checklist's count, files on it, the chain of hands. */}
          {item.kind === 'APPROVAL' ? <Tag label={item.amount_label || 'Approval'} icon="ribbon-outline" color={colors.gold} bg={colors.goldSoft} /> : null}
          {item.repeat_label ? <Tag label={shortRepeat(item.repeat_label)} icon="repeat-outline" color={colors.info} bg={colors.infoSoft} /> : null}
          {item.steps_total ? (
            <Tag label={`${item.steps_done ?? 0} of ${item.steps_total}`} icon="list-outline"
              color={item.steps_done === item.steps_total ? colors.success : colors.inkSoft}
              bg={item.steps_done === item.steps_total ? colors.successSoft : colors.surfaceAlt} />
          ) : null}
          {item.files_count ? <Tag label={String(item.files_count)} icon="attach-outline" /> : null}
          {item.chain?.length ? <Tag label={item.chain.join(' → ')} icon="git-branch-outline" color={colors.inkSoft} /> : null}
        </View>
        {showStaffStatus ? (
          <View style={s.progress}>
            <View style={[s.progressFill, { width: `${progressOf(item)}%` }, item.status === 'BLOCKED' && { backgroundColor: colors.danger }, item.status === 'COMPLETED' && { backgroundColor: colors.success }]} />
          </View>
        ) : null}
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
    borderWidth: 1,
    borderColor: colors.line,
    paddingVertical: 12,
    paddingRight: 12,
    marginBottom: 8,
    overflow: 'hidden',
  },
  rowFlat: { borderWidth: 0, borderRadius: 0, marginBottom: 0, backgroundColor: 'transparent', paddingVertical: 13 },
  rowLine: { borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: colors.line },
  bar: { width: 4, alignSelf: 'stretch', marginRight: 10, borderTopRightRadius: 2, borderBottomRightRadius: 2 },
  barFlat: { width: 3, borderRadius: 2, marginLeft: 8, alignSelf: 'center', height: 28 },
  icon: { width: 32, height: 32, borderRadius: 16, alignItems: 'center', justifyContent: 'center', marginRight: 10 },
  meta: { flexDirection: 'row', flexWrap: 'wrap', marginTop: 5, rowGap: 4 },
  when: { marginLeft: 10, maxWidth: 110, textAlign: 'right', color: colors.accentDeep, fontWeight: '600' },
  done: { textDecorationLine: 'line-through', color: colors.muted },
  progress: { height: 6, borderRadius: 3, backgroundColor: colors.surfaceAlt, overflow: 'hidden', marginTop: 8, marginRight: 4 },
  progressFill: { height: 6, borderRadius: 3, backgroundColor: colors.accent },
});
