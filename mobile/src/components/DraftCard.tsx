import React, { useState } from 'react';
import { StyleSheet, TextInput, View } from 'react-native';

import { api } from '@/lib/api';
import { TASK_DRAFTS, draftHeading } from '@/lib/drafts';
import { addDays, dayLabel, isoDate } from '@/lib/format';
import { colors, kindLabel, priorityColor, priorityLabel, radius, shadow } from '@/lib/theme';
import type { CategoryOption, Draft, DraftAction, Kind, Priority, StaffOption } from '@/lib/types';

import { Button, Chip, Icon, Txt } from './ui';

const PRIORITIES: Priority[] = ['URGENT', 'HIGH', 'NORMAL', 'LOW'];
const KINDS: Kind[] = ['TASK', 'MEETING', 'CALL', 'PAYMENT', 'FOLLOW_UP'];

const SAVE_LABEL: Partial<Record<DraftAction, string>> = {
  private_task: 'Save',
  delegation: 'Assign',
  pa_task: 'Give to PA',
  pa_meeting: 'Save meeting',
  md_request: 'Send to Sir',
  accept_request: 'Accept',
  decline_request: 'Decline',
  complete: 'Mark done',
  remember: 'Yes, remember',
};

interface Props {
  draft: Draft;
  saving: boolean;
  listening: boolean;
  error: string;
  onChange: (patch: Partial<Draft>) => void;
  onSave: () => void;
  onCancel: () => void;
}

export function DraftCard({ draft, saving, listening, error, onChange, onSave, onCancel }: Props) {
  const [editing, setEditing] = useState(false);
  const [allStaff, setAllStaff] = useState<StaffOption[] | null>(null);
  const [categories, setCategories] = useState<CategoryOption[]>(draft.categories ?? []);
  const head = draftHeading(draft);
  const isTask = TASK_DRAFTS.includes(draft.action);
  const waitingForAnswer = draft.needs_clarification || draft.action === 'choose_destination';

  const loadStaff = async () => {
    if (allStaff) return;
    try {
      const res = await api<{ results: StaffOption[]; categories: CategoryOption[] }>('/staff/');
      setAllStaff(res.data.results);
      if (res.data.categories?.length) setCategories(res.data.categories);
    } catch {
      setAllStaff([]);
    }
  };

  const today = isoDate(new Date());
  const dateChips = [
    { label: 'Today', value: today },
    { label: 'Tomorrow', value: isoDate(addDays(new Date(), 1)) },
    { label: 'Day after', value: isoDate(addDays(new Date(), 2)) },
    { label: 'No date', value: '' },
  ];
  const staffOptions = allStaff ?? draft.staff_candidates ?? [];
  const when = draft.due_date ? `${dayLabel(draft.due_date)}${draft.due_time ? ` · ${draft.due_time}` : ''}` : 'No date';
  const saveLabel = draft.action === 'team_delegation' ? `Assign to ${draft.team?.members.length ?? 0}` : SAVE_LABEL[draft.action] ?? 'Confirm';
  const canSave = !waitingForAnswer && (isTask ? !!draft.title.trim() : draft.action === 'remember' ? !!draft.memory_text : !!(draft.target || draft.targets?.length))
    && (draft.action !== 'delegation' || !!draft.staff);

  return (
    <View style={s.card}>
      <View style={s.head}>
        <View style={s.headIcon}>
          <Icon name={head.icon} size={18} color={colors.ink} />
        </View>
        <View style={{ flex: 1 }}>
          <Txt variant="heading">{head.title}</Txt>
          {head.detail ? <Txt variant="small">{head.detail}</Txt> : null}
        </View>
      </View>

      {draft.action === 'remember' ? (
        <Txt variant="title" style={s.main}>{draft.memory_text}</Txt>
      ) : isTask ? (
        <>
          <Txt variant="title" style={s.main}>{draft.title || 'What should the task say?'}</Txt>
          <View style={s.facts}>
            <Fact icon="calendar-outline" text={when} />
            {draft.priority !== 'NORMAL' ? <Fact icon="flag-outline" text={priorityLabel[draft.priority]} color={priorityColor[draft.priority]} /> : null}
            {draft.kind !== 'TASK' ? <Fact icon="pricetag-outline" text={kindLabel[draft.kind]} /> : null}
          </View>
          {draft.action === 'team_delegation' && draft.team ? (
            <Txt variant="small" style={{ marginTop: 8 }}>
              {draft.team.members.map((m) => m.name).join(', ')}
              {draft.team.skipped.length ? `  ·  On leave, skipped: ${draft.team.skipped.map((m) => m.name).join(', ')}` : ''}
            </Txt>
          ) : null}
          {draft.kind === 'PAYMENT' ? <Txt variant="caption" style={{ marginTop: 6 }}>Reminder only. JARVIS never makes payments.</Txt> : null}
        </>
      ) : draft.targets?.length > 1 ? (
        <View style={{ marginTop: 10 }}>
          {draft.targets.map((t) => (
            <Txt key={t.id} variant="heading" style={{ marginTop: 4 }}>• {t.title}</Txt>
          ))}
        </View>
      ) : draft.target ? (
        <Txt variant="title" style={s.main}>{draft.target.title}</Txt>
      ) : draft.target_candidates.length ? (
        <View style={[s.rowWrap, { marginTop: 10 }]}>
          {draft.target_candidates.map((c) => (
            <Chip key={c.id} label={c.title} onPress={() => onChange({ target: c, needs_clarification: false })} />
          ))}
        </View>
      ) : null}

      {draft.action === 'delegation' && (!draft.staff || editing) ? (
        <>
          <Txt variant="label" style={s.fieldLabel}>Who should do it</Txt>
          <View style={s.rowWrap}>
            {staffOptions.map((st) => (
              <Chip
                key={st.id}
                label={`${st.name} · ${st.department}${st.on_leave ? ' · on leave' : ''}`}
                color={st.on_leave ? colors.danger : colors.ink}
                selected={draft.staff?.id === st.id}
                onPress={() => onChange({ staff: st, needs_clarification: false })}
              />
            ))}
            {!allStaff ? <Chip label="Other staff…" icon="search-outline" onPress={loadStaff} /> : null}
          </View>
        </>
      ) : null}

      {draft.warnings?.length ? (
        <View style={s.warn}>
          {draft.warnings.map((w) => <Txt key={w} variant="small" style={{ color: colors.warn }}>{w}</Txt>)}
        </View>
      ) : null}

      {editing && isTask ? (
        <View style={{ marginTop: 6 }}>
          <Txt variant="label" style={s.fieldLabel}>Title</Txt>
          <TextInput value={draft.title} onChangeText={(v) => onChange({ title: v })} style={s.input} placeholder="What needs doing" placeholderTextColor={colors.faint} />
          <Txt variant="label" style={s.fieldLabel}>Day</Txt>
          <View style={s.rowWrap}>
            {dateChips.map((c) => <Chip key={c.label} label={c.label} selected={draft.due_date === c.value} onPress={() => onChange({ due_date: c.value })} />)}
          </View>
          <Txt variant="label" style={s.fieldLabel}>Time</Txt>
          <View style={[s.rowWrap, { alignItems: 'center' }]}>
            <TextInput
              value={draft.due_time}
              onChangeText={(v) => onChange({ due_time: v.replace(/[^0-9:]/g, '').slice(0, 5) })}
              placeholder="HH:MM"
              placeholderTextColor={colors.faint}
              keyboardType="numbers-and-punctuation"
              style={[s.input, { width: 92, marginTop: 0, marginRight: 8, marginBottom: 8 }]}
            />
            {['10:00', '12:00', '15:00', '18:00'].map((t) => <Chip key={t} label={t} selected={draft.due_time === t} onPress={() => onChange({ due_time: t })} />)}
            {draft.due_time ? <Chip label="No time" onPress={() => onChange({ due_time: '' })} /> : null}
          </View>
          <Txt variant="label" style={s.fieldLabel}>How important</Txt>
          <View style={s.rowWrap}>
            {PRIORITIES.filter((p) => !['delegation', 'team_delegation'].includes(draft.action) || p !== 'LOW').map((p) => (
              <Chip key={p} label={priorityLabel[p]} color={priorityColor[p]} selected={draft.priority === p} onPress={() => onChange({ priority: p })} />
            ))}
          </View>
          <Txt variant="label" style={s.fieldLabel}>Type</Txt>
          <View style={s.rowWrap}>
            {KINDS.map((k) => <Chip key={k} label={kindLabel[k]} selected={draft.kind === k} onPress={() => onChange({ kind: k })} />)}
          </View>
          {draft.action === 'delegation' && categories.length ? (
            <>
              <Txt variant="label" style={s.fieldLabel}>Category</Txt>
              <View style={s.rowWrap}>
                {categories.map((c) => (
                  <Chip key={c.key} label={c.department ? `${c.name} · ${c.department}` : c.name} selected={draft.category_key === c.key} onPress={() => onChange({ category_key: c.key })} />
                ))}
              </View>
            </>
          ) : null}
        </View>
      ) : null}

      {error ? <Txt variant="small" style={{ color: colors.danger, marginTop: 10 }}>{error}</Txt> : null}

      {waitingForAnswer ? (
        <Txt variant="small" style={{ marginTop: 12, color: colors.inkSoft }}>
          {listening ? 'Listening for your answer…' : 'Answer by voice or type below.'}
        </Txt>
      ) : (
        <>
          <View style={s.buttons}>
            <Button title="Cancel" kind="secondary" compact onPress={onCancel} style={{ marginRight: 8 }} />
            {isTask ? (
              <Button title={editing ? 'Done editing' : 'Change details'} kind="ghost" compact icon="create-outline" onPress={() => setEditing((v) => !v)} style={{ marginRight: 'auto' }} />
            ) : <View style={{ flex: 1 }} />}
            <Button title={saveLabel} icon="checkmark" compact loading={saving} disabled={!canSave} onPress={onSave} />
          </View>
          <Txt variant="caption" style={{ marginTop: 8 }}>
            {listening ? 'Listening… tell me in your own words: save it, leave it, or what to change.' : 'Nothing is saved until you say so.'}
          </Txt>
        </>
      )}
    </View>
  );
}

function Fact({ icon, text, color = colors.inkSoft }: { icon: string; text: string; color?: string }) {
  return (
    <View style={s.fact}>
      <Icon name={icon} size={14} color={color} />
      <Txt variant="small" style={{ marginLeft: 5, color }}>{text}</Txt>
    </View>
  );
}

const s = StyleSheet.create({
  card: {
    backgroundColor: colors.surface,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.line,
    padding: 16,
    marginTop: 6,
    marginBottom: 14,
    ...shadow,
  },
  head: { flexDirection: 'row', alignItems: 'center' },
  headIcon: { width: 34, height: 34, borderRadius: 17, backgroundColor: colors.surfaceAlt, alignItems: 'center', justifyContent: 'center', marginRight: 10 },
  main: { marginTop: 12, fontSize: 21, lineHeight: 28 },
  facts: { flexDirection: 'row', flexWrap: 'wrap', marginTop: 8, columnGap: 14, rowGap: 4 },
  fact: { flexDirection: 'row', alignItems: 'center' },
  fieldLabel: { marginTop: 12, marginBottom: 4, color: colors.inkSoft },
  rowWrap: { flexDirection: 'row', flexWrap: 'wrap' },
  input: {
    backgroundColor: colors.bg,
    borderWidth: 1,
    borderColor: colors.line,
    borderRadius: radius.md,
    paddingHorizontal: 12,
    paddingVertical: 9,
    fontSize: 16,
    color: colors.text,
  },
  warn: { marginTop: 10, padding: 10, borderRadius: radius.md, backgroundColor: colors.warnSoft, rowGap: 2 },
  buttons: { flexDirection: 'row', alignItems: 'center', marginTop: 14 },
});
