/**
 * The card JARVIS shows before anything is saved: what it understood, laid out so it reads at a glance on any
 * width (a narrow phone, the tablet's assistant column). Two actions only, side by side and equal to the width:
 * nothing can spill off the card. When JARVIS is asking something, quick answers can be tapped instead of spoken.
 */
import React, { useState } from 'react';
import { Pressable, StyleSheet, TextInput, View } from 'react-native';

import { api } from '@/lib/api';
import { TASK_DRAFTS, draftHeading } from '@/lib/drafts';
import { addDays, dayLabel, isoDate, timeLabel } from '@/lib/format';
import { useSession } from '@/lib/session';
import { colors, kindIcon, kindLabel, priorityColor, priorityLabel, prioritySoft, radius, shadow } from '@/lib/theme';
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
  pa_self_task: 'Save',
  accept_request: 'Accept',
  decline_request: 'Decline',
  complete: 'Mark done',
  remember: 'Yes, remember',
  change_task: 'Change',
  delete_task: 'Delete',
};

interface Props {
  draft: Draft;
  saving: boolean;
  listening: boolean;
  error: string;
  onChange: (patch: Partial<Draft>) => void;
  onSave: () => void;
  onCancel: () => void;
  /** Answer JARVIS's question with a tap (sent exactly as if it had been said). */
  onReply?: (text: string) => void;
}

export function DraftCard({ draft, saving, listening, error, onChange, onSave, onCancel, onReply }: Props) {
  const { user } = useSession();
  const [editing, setEditing] = useState(false);
  const [allStaff, setAllStaff] = useState<StaffOption[] | null>(null);
  const [categories, setCategories] = useState<CategoryOption[]>(draft.categories ?? []);
  const head = draftHeading(draft);
  const isMD = user?.role === 'MD';
  const isTask = TASK_DRAFTS.includes(draft.action);
  const choosing = draft.action === 'choose_destination';
  const waitingForAnswer = draft.needs_clarification || choosing;

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
  const when = draft.due_date ? `${dayLabel(draft.due_date)}${draft.due_time ? ` · ${timeLabel(draft.due_time)}` : ''}` : 'No date';
  const saveLabel = draft.action === 'team_delegation' ? `Assign to ${draft.team?.members.length ?? 0}` : SAVE_LABEL[draft.action] ?? 'Confirm';
  const canSave = !waitingForAnswer && (isTask ? !!draft.title.trim() : draft.action === 'remember' ? !!draft.memory_text : !!(draft.target || draft.targets?.length))
    && (draft.action !== 'delegation' || !!draft.staff);
  const danger = draft.action === 'delete_task' || draft.action === 'decline_request';

  // JARVIS found a clash or a free slot and offered a time: one tap takes it (or he can just say "haan").
  const offered = draft.schedule?.suggested ?? null;
  const takeOffered = () => {
    if (!offered) return;
    const cleared = draft.schedule ? { ...draft.schedule, suggested: null, question: '', question_hi: '' } : null;
    if (draft.action === 'change_task') onChange({ changes: { ...draft.changes, due_date: offered.due_date, due_time: offered.due_time }, schedule: cleared });
    else onChange({ due_date: offered.due_date, due_time: offered.due_time, schedule: cleared });
  };

  // Quick answers to the two questions JARVIS asks most: who is this person, and whose task is it.
  const askingWho = /don't know any/i.test(draft.question ?? '');
  const quick: { label: string; say: string; icon: string }[] = [];
  if (onReply && waitingForAnswer) {
    if (askingWho) {
      quick.push({ label: 'On the staff', say: 'staff mein hai', icon: 'briefcase-outline' });
      quick.push({ label: 'Outside contact', say: 'bahar ka contact hai', icon: 'person-add-outline' });
    } else if (choosing && isMD) {
      quick.push({ label: 'My personal', say: 'mere personal mein daalo', icon: 'lock-closed-outline' });
      quick.push({ label: 'Give to my PA', say: 'PA ko de do', icon: 'person-outline' });
    } else if (choosing) {
      quick.push({ label: 'For Sir', say: 'Sir ke liye hai', icon: 'paper-plane-outline' });
      quick.push({ label: 'For me', say: 'mere liye hai', icon: 'person-outline' });
    }
  }

  const titleText = draft.action === 'remember' ? draft.memory_text
    : isTask || choosing ? draft.title
    : draft.target && !(draft.targets?.length > 1) ? draft.target.title
    : '';

  return (
    <View style={s.card}>
      {/* what kind of thing this is, and who will see it */}
      <View style={s.head}>
        <View style={[s.headIcon, danger && { backgroundColor: colors.dangerSoft }]}>
          <Icon name={head.icon} size={17} color={danger ? colors.danger : colors.accent} />
        </View>
        <View style={s.headText}>
          <Txt variant="heading" numberOfLines={1}>{head.title}</Txt>
          {head.detail ? <Txt variant="caption" numberOfLines={2} style={{ color: colors.muted }}>{head.detail}</Txt> : null}
        </View>
        {isTask && !waitingForAnswer ? (
          <Pressable
            onPress={() => setEditing((v) => !v)}
            hitSlop={8}
            accessibilityLabel={editing ? 'Done editing' : 'Change details'}
            style={[s.editBtn, editing && { backgroundColor: colors.accent, borderColor: colors.accent }]}
          >
            <Icon name={editing ? 'checkmark' : 'create-outline'} size={16} color={editing ? colors.white : colors.inkSoft} />
          </Pressable>
        ) : null}
      </View>

      {titleText ? <Txt variant="title" numberOfLines={4} style={s.main}>{titleText}</Txt>
        : isTask ? <Txt variant="title" style={[s.main, { color: colors.faint }]}>What should the task say?</Txt> : null}

      {isTask ? (
        <>
          <View style={s.facts}>
            <Fact icon="calendar-outline" text={when} strong={!!draft.due_date} />
            {draft.kind !== 'TASK' ? <Fact icon={kindIcon[draft.kind] ?? 'pricetag-outline'} text={kindLabel[draft.kind]} /> : null}
            {draft.priority !== 'NORMAL' ? (
              <Fact icon="flag" text={priorityLabel[draft.priority]} color={priorityColor[draft.priority]} bg={prioritySoft[draft.priority]} />
            ) : null}
            {draft.action === 'delegation' && draft.staff ? <Fact icon="person-outline" text={draft.staff.name} /> : null}
          </View>
          {draft.action === 'team_delegation' && draft.team ? (
            <Txt variant="small" style={{ marginTop: 10 }}>
              {draft.team.members.map((m) => m.name).join(', ')}
              {draft.team.skipped.length ? `  ·  On leave, skipped: ${draft.team.skipped.map((m) => m.name).join(', ')}` : ''}
            </Txt>
          ) : null}
          {draft.kind === 'PAYMENT' ? <Txt variant="caption" style={{ marginTop: 8 }}>Reminder only. JARVIS never makes payments.</Txt> : null}
        </>
      ) : draft.targets?.length > 1 ? (
        <View style={{ marginTop: 12, rowGap: 6 }}>
          {draft.targets.map((t) => (
            <View key={t.id} style={s.targetRow}>
              <View style={s.bullet} />
              <Txt variant="heading" style={{ flex: 1 }}>{t.title}</Txt>
            </View>
          ))}
        </View>
      ) : !draft.target && draft.target_candidates.length ? (
        <View style={[s.rowWrap, { marginTop: 12 }]}>
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
          <Icon name="information-circle-outline" size={16} color={colors.warn} />
          <View style={{ flex: 1, marginLeft: 8, rowGap: 2 }}>
            {draft.warnings.map((w) => <Txt key={w} variant="small" style={{ color: colors.warn }}>{w}</Txt>)}
          </View>
        </View>
      ) : null}

      {offered && !waitingForAnswer ? (
        <Pressable onPress={takeOffered} style={({ pressed }) => [s.offer, pressed && { opacity: 0.8 }]}>
          <Icon name="time-outline" size={16} color={colors.accent} />
          <Txt variant="small" style={s.offerText}>
            Free {dayLabel(offered.due_date) === 'Today' ? '' : `${dayLabel(offered.due_date).toLowerCase()} `}at {timeLabel(offered.due_time)}
          </Txt>
          <Txt variant="small" style={{ color: colors.accent, fontWeight: '600' }}>Use it</Txt>
        </Pressable>
      ) : null}

      {editing && isTask ? (
        <View style={s.form}>
          <Txt variant="label" style={[s.fieldLabel, { marginTop: 0 }]}>Title</Txt>
          <TextInput value={draft.title} onChangeText={(v) => onChange({ title: v })} style={s.input} placeholder="What needs doing" placeholderTextColor={colors.faint} multiline />
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
              style={[s.input, s.timeInput]}
            />
            {['10:00', '12:00', '15:00', '18:00'].map((t) => <Chip key={t} label={timeLabel(t)} selected={draft.due_time === t} onPress={() => onChange({ due_time: t })} />)}
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

      {error ? (
        <View style={s.error}>
          <Icon name="alert-circle-outline" size={16} color={colors.danger} />
          <Txt variant="small" style={{ color: colors.danger, flex: 1, marginLeft: 8 }}>{error}</Txt>
        </View>
      ) : null}

      {waitingForAnswer ? (
        <>
          {quick.length ? (
            <View style={s.quick}>
              {quick.map((q) => (
                <Pressable key={q.label} onPress={() => onReply?.(q.say)} style={({ pressed }) => [s.quickBtn, pressed && { opacity: 0.75 }]}>
                  <Icon name={q.icon} size={15} color={colors.accent} />
                  <Txt variant="small" numberOfLines={1} style={s.quickText}>{q.label}</Txt>
                </Pressable>
              ))}
            </View>
          ) : null}
          <View style={s.hint}>
            <Icon name={listening ? 'mic' : 'chatbubble-ellipses-outline'} size={14} color={listening ? colors.accent : colors.muted} />
            <Txt variant="caption" style={[s.hintText, listening && { color: colors.accent }]}>
              {listening ? 'Listening for your answer…' : quick.length ? 'Tap one, say it, or type below.' : 'Answer by voice, or type below.'}
            </Txt>
            <Pressable onPress={onCancel} hitSlop={10}>
              <Txt variant="caption" style={{ color: colors.muted, fontWeight: '600' }}>Cancel</Txt>
            </Pressable>
          </View>
        </>
      ) : (
        <>
          <View style={s.buttons}>
            <Button title="Cancel" kind="secondary" onPress={onCancel} style={s.btnCancel} />
            <Button title={saveLabel} kind={danger ? 'danger' : 'primary'} icon={danger ? undefined : 'checkmark'} loading={saving} disabled={!canSave} onPress={onSave} style={s.btnSave} />
          </View>
          <View style={[s.hint, { justifyContent: 'center' }]}>
            <Icon name={listening ? 'mic' : 'shield-checkmark-outline'} size={13} color={listening ? colors.accent : colors.faint} />
            <Txt variant="caption" style={[{ marginLeft: 6 }, listening && { color: colors.accent }]}>
              {listening ? 'Listening… say yes, no, or what to change' : 'Nothing is saved until you say so'}
            </Txt>
          </View>
        </>
      )}
    </View>
  );
}

function Fact({ icon, text, color = colors.inkSoft, bg = colors.surfaceAlt, strong }: { icon: string; text: string; color?: string; bg?: string; strong?: boolean }) {
  return (
    <View style={[s.fact, { backgroundColor: bg }]}>
      <Icon name={icon} size={14} color={color} />
      <Txt variant="small" numberOfLines={1} style={[s.factText, { color }, strong && { color: colors.ink, fontWeight: '600' }]}>{text}</Txt>
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
    marginTop: 12,
    marginBottom: 6,
    ...shadow,
  },
  head: { flexDirection: 'row', alignItems: 'center' },
  headIcon: { width: 36, height: 36, borderRadius: 12, backgroundColor: colors.accentSoft, alignItems: 'center', justifyContent: 'center', marginRight: 10 },
  headText: { flex: 1, minWidth: 0 },
  editBtn: { width: 34, height: 34, borderRadius: 17, borderWidth: 1, borderColor: colors.line, alignItems: 'center', justifyContent: 'center', marginLeft: 8 },
  main: { marginTop: 14, fontSize: 20, lineHeight: 28 },
  facts: { flexDirection: 'row', flexWrap: 'wrap', marginTop: 12, gap: 8 },
  fact: { flexDirection: 'row', alignItems: 'center', borderRadius: 999, paddingVertical: 6, paddingHorizontal: 10, maxWidth: '100%' },
  factText: { marginLeft: 6, flexShrink: 1 },
  targetRow: { flexDirection: 'row', alignItems: 'flex-start' },
  bullet: { width: 6, height: 6, borderRadius: 3, backgroundColor: colors.accent, marginTop: 8, marginRight: 10 },
  fieldLabel: { marginTop: 14, marginBottom: 6, color: colors.inkSoft },
  rowWrap: { flexDirection: 'row', flexWrap: 'wrap' },
  form: { marginTop: 14, paddingTop: 14, borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: colors.line },
  input: {
    backgroundColor: colors.bg,
    borderWidth: 1,
    borderColor: colors.line,
    borderRadius: radius.md,
    paddingHorizontal: 12,
    paddingVertical: 9,
    fontSize: 15,
    color: colors.text,
  },
  timeInput: { width: 92, marginRight: 8, marginBottom: 8 },
  warn: { flexDirection: 'row', alignItems: 'flex-start', marginTop: 12, padding: 10, borderRadius: radius.md, backgroundColor: colors.warnSoft },
  offer: { flexDirection: 'row', alignItems: 'center', marginTop: 10, paddingVertical: 10, paddingHorizontal: 12, borderRadius: radius.md, backgroundColor: colors.accentSoft },
  offerText: { flex: 1, marginLeft: 8, color: colors.accentDeep, fontWeight: '500' },
  error: { flexDirection: 'row', alignItems: 'flex-start', marginTop: 12, padding: 10, borderRadius: radius.md, backgroundColor: colors.dangerSoft },
  quick: { flexDirection: 'row', flexWrap: 'wrap', marginTop: 14, gap: 8 },
  quickBtn: {
    flexGrow: 1, flexBasis: '45%', minWidth: 0, flexDirection: 'row', alignItems: 'center', justifyContent: 'center',
    paddingVertical: 11, paddingHorizontal: 10, borderRadius: radius.md, backgroundColor: colors.accentSoft,
  },
  quickText: { marginLeft: 7, color: colors.accentDeep, fontWeight: '600', flexShrink: 1 },
  hint: { flexDirection: 'row', alignItems: 'center', marginTop: 12 },
  hintText: { flex: 1, marginLeft: 6, color: colors.muted },
  buttons: { flexDirection: 'row', alignItems: 'stretch', marginTop: 16, gap: 10 },
  btnCancel: { flex: 1, minWidth: 0, minHeight: 46, paddingHorizontal: 8 },
  btnSave: { flex: 1.7, minWidth: 0, minHeight: 46, paddingHorizontal: 8 },
});
