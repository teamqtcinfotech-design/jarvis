/**
 * One answer drawn as data: stat tiles, a day timeline, a week chart, a table with progress, requests, or
 * Sir's busy spans. Rows open the same task sheet as the lists; requests accept or decline in place.
 * Under every card: Download (CSV / PDF) and Share.
 */
import React, { useState } from 'react';
import { Pressable, StyleSheet, View } from 'react-native';

import { ApiError, api, newIdempotencyKey } from '@/lib/api';
import { dataChanged } from '@/lib/bus';
import { downloadCsv, downloadPdf, shareText } from '@/lib/download';
import { useLayout } from '@/lib/layout';
import { say } from '@/lib/speech';
import { colors, priorityColor, radius, shadow, tone } from '@/lib/theme';
import type { DataView, TaskItem, ViewRow, ViewTimelineItem } from '@/lib/types';
import { useDashboard } from '@/lib/useDashboard';

import { TaskSheet } from './TaskSheet';
import { Button, Icon, Txt } from './ui';

export function StatTiles({ stats, columns }: { stats: DataView['stats']; columns: number }) {
  if (!stats.length) return null;
  const gap = 10;
  const cols = Math.max(1, Math.min(columns, stats.length));
  return (
    <View style={[s.tiles, { gap }]}>
      {stats.map((t) => {
        const c = tone[t.tone] ?? tone.muted;
        return (
          <View key={t.label} style={[s.tile, { backgroundColor: c.bg, width: `${100 / cols}%`, flexBasis: `${100 / cols}%`, flexGrow: 1 }]}>
            {t.icon ? (
              <View style={[s.tileIcon, { backgroundColor: colors.surface }]}>
                <Icon name={t.icon} size={15} color={c.fg} />
              </View>
            ) : null}
            <Txt style={[s.tileValue, { color: c.fg }]}>{String(t.value)}</Txt>
            <Txt variant="label" style={{ color: c.fg, opacity: 0.85 }}>{t.label}</Txt>
          </View>
        );
      })}
    </View>
  );
}

/** A horizontal strip of the day (office hours) with busy, routine and free segments. */
function DayStrip({ items, office }: { items: ViewTimelineItem[]; office?: { start_min: number; end_min: number } }) {
  const start = Math.min(office?.start_min ?? 600, ...items.map((i) => i.start_min));
  const end = Math.max(office?.end_min ?? 1140, ...items.map((i) => i.end_min));
  const span = Math.max(60, end - start);
  const pct = (m: number) => `${Math.max(0, Math.min(100, ((m - start) / span) * 100))}%`;
  const hours: number[] = [];
  for (let h = Math.ceil(start / 60); h <= Math.floor(end / 60); h += 2) hours.push(h);
  return (
    <View style={{ marginTop: 12 }}>
      <View style={s.strip}>
        {items.map((i, idx) => (
          <View
            key={`${i.id || i.title}-${idx}`}
            style={[
              s.block,
              { left: pct(i.start_min), width: `${Math.max(2, ((i.end_min - i.start_min) / span) * 100)}%` },
              i.routine ? s.blockRoutine : i.private ? s.blockPrivate : i.kind === 'Busy' ? s.blockBusy : { backgroundColor: priorityColor[i.priority] ?? colors.accent },
            ]}
          />
        ))}
      </View>
      <View style={s.hours}>
        {hours.map((h) => (
          <Txt key={h} variant="caption" style={{ position: 'absolute', left: pct(h * 60), transform: [{ translateX: -10 }] }}>
            {h % 12 || 12}{h < 12 ? 'a' : 'p'}
          </Txt>
        ))}
      </View>
    </View>
  );
}

function Timeline({ items, onOpen }: { items: ViewTimelineItem[]; onOpen: (id: string, scope: string) => void }) {
  if (!items.length) return <Txt variant="small" style={{ marginTop: 10 }}>No timed items.</Txt>;
  return (
    <View style={{ marginTop: 12 }}>
      {items.map((i, idx) => (
        <Pressable key={`${i.id || i.title}-${idx}`} onPress={() => i.id && onOpen(i.id, i.scope)} disabled={!i.id} style={s.tlRow}>
          <View style={s.tlTime}>
            <Txt variant="heading" style={{ fontSize: 13 }}>{i.start}</Txt>
            <Txt variant="caption">{i.end}</Txt>
          </View>
          <View style={[s.tlDot, { backgroundColor: i.routine ? colors.faint : i.private ? colors.inkSoft : priorityColor[i.priority] ?? colors.accent }]} />
          <View style={{ flex: 1 }}>
            <Txt variant="heading" numberOfLines={2} style={i.overdue && { color: colors.danger }}>{i.title}</Txt>
            <Txt variant="caption">{i.kind}{i.private ? ' · private' : ''}{i.overdue ? ' · late' : ''}</Txt>
          </View>
        </Pressable>
      ))}
    </View>
  );
}

function Bars({ series }: { series: DataView['series'] }) {
  const max = Math.max(1, ...series.map((p) => p.value));
  return (
    <View style={s.bars}>
      {series.map((p) => (
        <View key={p.date} style={s.barCol}>
          <Txt variant="caption" style={{ marginBottom: 4, color: p.heavy ? colors.danger : colors.muted }}>{p.value}</Txt>
          <View style={s.barTrack}>
            <View
              style={[
                s.barFill,
                { height: `${Math.max(4, (p.value / max) * 100)}%` },
                p.heavy ? { backgroundColor: colors.danger } : p.off ? { backgroundColor: colors.faint } : p.today ? { backgroundColor: colors.accent } : { backgroundColor: colors.accentGlow },
              ]}
            />
          </View>
          <Txt variant="caption" style={[{ marginTop: 6 }, p.today && { color: colors.accent, fontWeight: '700' }]}>{p.label}</Txt>
        </View>
      ))}
    </View>
  );
}

function Progress({ value, danger }: { value: number; danger?: boolean }) {
  return (
    <View style={s.progress}>
      <View style={[s.progressFill, { width: `${Math.max(0, Math.min(100, value))}%` }, danger && { backgroundColor: colors.danger }, value >= 100 && { backgroundColor: colors.success }]} />
    </View>
  );
}

function Table({ view, onOpen }: { view: DataView; onOpen: (id: string, scope: string) => void }) {
  if (!view.rows.length) return <Txt variant="small" style={{ marginTop: 10 }}>{view.subtitle || 'Nothing here.'}</Txt>;
  const progress = view.layout === 'progress';
  return (
    <View style={{ marginTop: 10 }}>
      {view.rows.map((r, idx) => (
        <Pressable key={`${r.id}-${idx}`} onPress={() => r.id && r.scope && r.scope !== 'request' && onOpen(r.id, r.scope)} style={[s.tr, idx === view.rows.length - 1 && { borderBottomWidth: 0 }]}>
          <View style={[s.priorityBar, { backgroundColor: priorityColor[r.priority] ?? colors.line }]} />
          <View style={{ flex: 1 }}>
            <Txt variant="heading" numberOfLines={2} style={r.overdue && { color: colors.danger }}>{r.title}</Txt>
            <Txt variant="caption" style={{ marginTop: 2 }}>
              {[r.day && r.day !== 'today' ? r.day : '', r.when, r.who, r.kind, r.private ? 'private' : '', r.overdue ? 'late' : ''].filter(Boolean).join(' · ')}
            </Txt>
            {progress ? (
              <View style={{ flexDirection: 'row', alignItems: 'center', marginTop: 6 }}>
                <Progress value={r.progress} danger={r.status === 'Hold' || r.status === 'On hold'} />
                <Txt variant="caption" style={{ marginLeft: 8, width: 84 }}>{r.status}</Txt>
              </View>
            ) : null}
            {r.hold_reason ? <Txt variant="caption" style={{ color: colors.danger }}>On hold: {r.hold_reason}</Txt> : null}
          </View>
        </Pressable>
      ))}
    </View>
  );
}

function Requests({ view, isMD }: { view: DataView; isMD: boolean }) {
  const [busy, setBusy] = useState('');
  const [note, setNote] = useState('');
  const decide = async (r: ViewRow, accept: boolean) => {
    setBusy(`${r.id}:${accept}`);
    setNote('');
    try {
      await api(`/requests/${r.id}/${accept ? 'accept' : 'decline'}/`, { method: 'POST', body: {}, idempotencyKey: newIdempotencyKey() });
      say(accept ? "Accepted, Sir. It's on your list." : "Declined, Sir. I've informed them.", { interrupt: true });
      dataChanged.emit();
    } catch (e) {
      setNote(e instanceof ApiError ? e.message : 'Could not reach the server.');
    } finally {
      setBusy('');
    }
  };
  if (!view.rows.length) return <Txt variant="small" style={{ marginTop: 10 }}>{view.subtitle}</Txt>;
  return (
    <View style={{ marginTop: 10 }}>
      {view.rows.map((r) => (
        <View key={r.id} style={s.req}>
          <View style={{ flexDirection: 'row', justifyContent: 'space-between' }}>
            <Txt variant="label" style={{ color: colors.inkSoft }}>{r.kind} · {r.who}</Txt>
            <Txt variant="caption" style={{ color: priorityColor[r.priority] }}>{r.status}</Txt>
          </View>
          <Txt variant="heading" style={{ marginTop: 4 }}>{r.title}</Txt>
          {r.target ? <Txt variant="small">For: {r.target}</Txt> : null}
          {r.when ? <Txt variant="small">Due {r.when}</Txt> : null}
          {r.reason ? <Txt variant="small" style={{ fontStyle: 'italic' }}>“{r.reason}”</Txt> : null}
          {isMD && r.status === 'Pending' ? (
            <View style={{ flexDirection: 'row', justifyContent: 'flex-end', marginTop: 8 }}>
              <Button title="Decline" kind="danger" compact loading={busy === `${r.id}:false`} onPress={() => decide(r, false)} style={{ marginRight: 8 }} />
              <Button title="Accept" icon="checkmark" compact loading={busy === `${r.id}:true`} onPress={() => decide(r, true)} />
            </View>
          ) : null}
        </View>
      ))}
      {note ? <Txt variant="small" style={{ color: colors.danger }}>{note}</Txt> : null}
    </View>
  );
}

export function DataCard({ view, extraText, onRemove, compact }: { view: DataView; extraText?: string; onRemove?: () => void; compact?: boolean }) {
  const { tileColumns } = useLayout();
  const { data } = useDashboard();
  const [open, setOpen] = useState<TaskItem | null>(null);
  const [busy, setBusy] = useState('');
  const [note, setNote] = useState('');
  const isMD = Boolean(data?.user.role === 'MD');

  const openRow = (id: string, scope: string) => {
    if (!data) return;
    const pool = [...data.overdue, ...data.today, ...data.upcoming, ...data.anytime, ...(data.pa_tasks ?? []), ...(data.md_tasks ?? []), ...data.delegated];
    const item = pool.find((t) => t.id === id && t.scope === scope) ?? pool.find((t) => t.id === id);
    if (item) setOpen(item);
  };

  const run = async (what: 'csv' | 'pdf' | 'share') => {
    setBusy(what);
    setNote('');
    try {
      if (what === 'csv') await downloadCsv(view);
      else if (what === 'pdf') await downloadPdf(view, extraText);
      else await shareText(view, extraText);
    } catch {
      setNote('Could not prepare the file. Please try again.');
    } finally {
      setBusy('');
    }
  };

  const showStats = view.stats.length > 0;
  return (
    <View style={[s.card, compact && { padding: 14 }]}>
      <View style={s.head}>
        <View style={{ flex: 1 }}>
          <Txt variant="title" style={{ fontSize: 19 }}>{view.title}</Txt>
          {view.subtitle ? <Txt variant="small">{view.subtitle}</Txt> : null}
        </View>
        {onRemove ? (
          <Pressable onPress={onRemove} hitSlop={10} accessibilityLabel="Remove">
            <Icon name="close" size={18} color={colors.faint} />
          </Pressable>
        ) : null}
      </View>
      {showStats ? <StatTiles stats={view.stats} columns={Math.min(tileColumns, Math.max(2, view.stats.length))} /> : null}
      {view.layout === 'timeline' || view.layout === 'busy' ? (
        <>
          <DayStrip items={view.timeline} office={view.office} />
          <Timeline items={view.timeline} onOpen={openRow} />
          {view.layout === 'timeline' && view.rows.some((r) => !r.when.includes('–')) ? (
            <>
              <Txt variant="label" style={{ marginTop: 14 }}>Any time today</Txt>
              <Table view={{ ...view, rows: view.rows.filter((r) => !r.when.includes('–')), layout: 'table' }} onOpen={openRow} />
            </>
          ) : null}
        </>
      ) : view.layout === 'bars' ? (
        <>
          <Bars series={view.series} />
          {view.rows.length ? <Table view={{ ...view, layout: 'table', rows: view.rows.slice(0, 12) }} onOpen={openRow} /> : null}
        </>
      ) : view.layout === 'requests' ? (
        <Requests view={view} isMD={isMD} />
      ) : view.layout === 'stats' ? (
        view.rows.length ? <Table view={{ ...view, layout: 'table', rows: view.rows.slice(0, 8) }} onOpen={openRow} /> : null
      ) : (
        <>
          {view.series.length ? <Bars series={view.series} /> : null}
          <Table view={view} onOpen={openRow} />
        </>
      )}
      {extraText ? <Txt variant="body" style={{ marginTop: 12, lineHeight: 25 }}>{extraText}</Txt> : null}
      <View style={s.foot}>
        <Txt variant="caption" style={{ flex: 1 }}>Download</Txt>
        <Button title="CSV" kind="secondary" compact icon="grid-outline" loading={busy === 'csv'} onPress={() => run('csv')} style={{ marginRight: 6 }} />
        <Button title="PDF" kind="secondary" compact icon="document-outline" loading={busy === 'pdf'} onPress={() => run('pdf')} style={{ marginRight: 6 }} />
        <Button title="Share" kind="ghost" compact icon="share-social-outline" loading={busy === 'share'} onPress={() => run('share')} />
      </View>
      {note ? <Txt variant="small" style={{ color: colors.danger, marginTop: 6 }}>{note}</Txt> : null}
      {open ? <TaskSheet item={open} onClose={() => setOpen(null)} /> : null}
    </View>
  );
}

const s = StyleSheet.create({
  card: { backgroundColor: colors.surface, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line, padding: 16, marginBottom: 14, ...shadow },
  head: { flexDirection: 'row', alignItems: 'flex-start' },
  tiles: { flexDirection: 'row', flexWrap: 'wrap', marginTop: 12 },
  tile: { borderRadius: radius.md, paddingVertical: 12, paddingHorizontal: 14, minWidth: 96, maxWidth: '100%', flexShrink: 1 },
  tileIcon: { width: 28, height: 28, borderRadius: 14, alignItems: 'center', justifyContent: 'center', marginBottom: 6 },
  tileValue: { fontSize: 24, fontWeight: '700', lineHeight: 30 },
  strip: { height: 14, borderRadius: 7, backgroundColor: colors.surfaceAlt, overflow: 'hidden' },
  block: { position: 'absolute', top: 0, bottom: 0, borderRadius: 7 },
  blockRoutine: { backgroundColor: colors.faint },
  blockPrivate: { backgroundColor: colors.inkSoft },
  blockBusy: { backgroundColor: colors.accent },
  hours: { height: 16, marginTop: 4 },
  tlRow: { flexDirection: 'row', alignItems: 'center', paddingVertical: 8, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: colors.line },
  tlTime: { width: 58 },
  tlDot: { width: 10, height: 10, borderRadius: 5, marginRight: 12 },
  bars: { flexDirection: 'row', alignItems: 'flex-end', marginTop: 16, height: 150 },
  barCol: { flex: 1, alignItems: 'center' },
  barTrack: { width: 22, height: 96, justifyContent: 'flex-end', backgroundColor: colors.surfaceAlt, borderRadius: 8, overflow: 'hidden' },
  barFill: { width: '100%', borderRadius: 8, backgroundColor: colors.accent },
  progress: { flex: 1, height: 8, borderRadius: 4, backgroundColor: colors.surfaceAlt, overflow: 'hidden' },
  progressFill: { height: 8, borderRadius: 4, backgroundColor: colors.accent },
  tr: { flexDirection: 'row', alignItems: 'center', paddingVertical: 9, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: colors.line },
  priorityBar: { width: 3, alignSelf: 'stretch', borderRadius: 2, marginRight: 10 },
  req: { padding: 12, borderRadius: radius.md, backgroundColor: colors.surfaceAlt, marginBottom: 8 },
  foot: { flexDirection: 'row', alignItems: 'center', marginTop: 14, paddingTop: 10, borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: colors.line },
});
