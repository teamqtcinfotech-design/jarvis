/**
 * One answer drawn as data: stat tiles, a day timeline, a week chart, a table with progress, requests, or
 * Sir's busy spans. Rows open the same task sheet as the lists; requests accept or decline in place.
 * Under every card: Download (CSV / PDF) and Share.
 */
import React, { useState } from 'react';
import { Pressable, ScrollView, StyleSheet, View } from 'react-native';

import { ApiError, api, newIdempotencyKey } from '@/lib/api';
import { dataChanged } from '@/lib/bus';
import { useConversation } from '@/lib/conversation';
import { downloadCsv, downloadPdf, shareText } from '@/lib/download';
import { useLayout } from '@/lib/layout';
import { addressOf } from '@/lib/names';
import { useSession } from '@/lib/session';
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

/** The same numbers in one slim row (rows of three when there are more than four): for when height matters. */
function StatStrip({ stats }: { stats: DataView['stats'] }) {
  if (!stats.length) return null;
  const perRow = stats.length <= 4 ? stats.length : 3;
  const lines: DataView['stats'][] = [];
  for (let i = 0; i < stats.length; i += perRow) lines.push(stats.slice(i, i + perRow));
  return (
    <View style={{ marginTop: 10, rowGap: 6 }}>
      {lines.map((line, idx) => (
        <View key={idx} style={s.stripRow}>
          {line.map((t) => {
            const c = tone[t.tone] ?? tone.muted;
            return (
              <View key={t.label} style={[s.stripTile, { backgroundColor: c.bg }]}>
                <Txt numberOfLines={1} adjustsFontSizeToFit minimumFontScale={0.7} style={[s.stripValue, { color: c.fg }]}>{String(t.value)}</Txt>
                <Txt numberOfLines={1} adjustsFontSizeToFit minimumFontScale={0.8} style={[s.stripLabel, { color: c.fg }]}>{t.label}</Txt>
              </View>
            );
          })}
        </View>
      ))}
    </View>
  );
}

const firstName = (name: string) => name.trim().split(/\s+/)[0] ?? name;

/**
 * Who did how much, one line per person so the whole team reads at a glance:
 * name · a bar as long as the work they were given, filled as far as they have finished · done/given · late.
 */
function PerformanceChart({ rows }: { rows: ViewRow[] }) {
  if (!rows.length) return <Txt variant="small" style={{ marginTop: 12 }}>No work was given to anyone in the last 30 days.</Txt>;
  const max = Math.max(1, ...rows.map((r) => r.assigned ?? 0));
  const mostLate = [...rows].filter((r) => (r.late ?? 0) > 0).sort((a, b) => (b.late ?? 0) - (a.late ?? 0))[0];
  const best = [...rows].filter((r) => (r.done ?? 0) > 0 && !(r.late ?? 0)).sort((a, b) => (b.done ?? 0) - (a.done ?? 0))[0];
  return (
    <View style={{ marginTop: 10 }}>
      {best || mostLate ? (
        <View style={s.insights}>
          {best ? (
            <View style={[s.insight, { backgroundColor: colors.successSoft }]}>
              <Icon name="trophy-outline" size={13} color={colors.success} />
              <Txt numberOfLines={1} style={[s.insightText, { color: colors.success }]}>On track: {firstName(best.title)} {best.done}/{best.assigned}</Txt>
            </View>
          ) : null}
          {mostLate ? (
            <View style={[s.insight, { backgroundColor: colors.dangerSoft }]}>
              <Icon name="alert-circle-outline" size={13} color={colors.danger} />
              <Txt numberOfLines={1} style={[s.insightText, { color: colors.danger }]}>Most late: {firstName(mostLate.title)} ({mostLate.late})</Txt>
            </View>
          ) : null}
        </View>
      ) : null}
      {rows.map((r) => {
        const assigned = r.assigned ?? 0;
        const done = r.done ?? 0;
        const late = r.late ?? 0;
        const all = assigned > 0 && done >= assigned;
        return (
          <View key={r.id} style={s.perfRow}>
            <View style={s.perfName}>
              <Txt numberOfLines={1} style={s.perfNameText}>{r.title}</Txt>
              {r.who ? <Txt numberOfLines={1} style={s.perfDept}>{r.who}</Txt> : null}
            </View>
            <View style={s.perfBarWrap}>
              <View style={[s.perfTrack, { width: `${Math.max(14, (assigned / max) * 100)}%` }]}>
                <View style={[s.perfFill, { width: `${assigned ? Math.min(100, (done / assigned) * 100) : 0}%` }, all && { backgroundColor: colors.success }]} />
              </View>
            </View>
            <Txt style={s.perfCount}>{done}/{assigned}</Txt>
            <View style={[s.perfLate, late ? { backgroundColor: colors.dangerSoft } : null]}>
              {late ? <Txt numberOfLines={1} style={[s.perfLateText, { color: colors.danger }]}>{late} late</Txt>
                : <Icon name={done ? 'checkmark-circle' : 'ellipse-outline'} size={15} color={done ? colors.success : colors.faint} />}
            </View>
          </View>
        );
      })}
      <View style={s.legend}>
        <View style={[s.legendDot, { backgroundColor: colors.surfaceAlt, borderWidth: 1, borderColor: colors.line }]} />
        <Txt variant="caption">given</Txt>
        <View style={[s.legendDot, { backgroundColor: colors.accent, marginLeft: 12 }]} />
        <Txt variant="caption">done</Txt>
        <View style={[s.legendDot, { backgroundColor: colors.success, marginLeft: 12 }]} />
        <Txt variant="caption">all done</Txt>
      </View>
    </View>
  );
}

const initials = (name: string) => name.trim().split(/\s+/).slice(0, 2).map((w) => (w[0] ?? '').toUpperCase()).join('');
/** One colour per team, in the order the teams come. */
const TEAM_COLORS = ['#5B5BD6', '#2F6FB0', '#1F7A4D', '#B7791F', '#8E44AD', '#0E8F8F', '#C0392B', '#3F3F55'];

/**
 * The people themselves, and they come FIRST: a tight grid of name cards that fits the screen, so the list he
 * asked for is what he sees. Teams are a thin coloured bar and a row of chips above it (tap one to see only
 * that team) instead of a tall chart that pushes the names down.
 * On each card: initials in the team's colour, the name, the role; a number = open tasks (red = some are late);
 * "Leave" when the person is away.
 */
function PeopleList({ view, columns, onPerson }: { view: DataView; columns: number; onPerson: (name: string) => void }) {
  const [team, setTeam] = useState('');
  if (!view.rows.length) return <Txt variant="small" style={{ marginTop: 12 }}>Nobody to show.</Txt>;
  const teams: { name: string; count: number; color: string }[] = [];
  for (const r of view.rows) {
    const found = teams.find((t) => t.name === r.who);
    if (found) found.count += 1;
    else teams.push({ name: r.who, count: 1, color: TEAM_COLORS[teams.length % TEAM_COLORS.length] });
  }
  const picked = teams.some((t) => t.name === team) ? team : '';
  const shown = picked ? view.rows.filter((r) => r.who === picked) : view.rows;
  const colorOf = (name: string) => teams.find((t) => t.name === name)?.color ?? colors.accent;
  const oneTeam = teams.length === 1;
  const anyWork = shown.some((r) => r.assigned || r.late);
  return (
    <View style={{ marginTop: 8 }}>
      {!oneTeam ? (
        <>
          <View style={s.shareBar}>
            {teams.map((t) => (
              <View key={t.name} style={{ flex: t.count, backgroundColor: t.color, opacity: picked && picked !== t.name ? 0.2 : 1 }} />
            ))}
          </View>
          <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={s.teamChips} keyboardShouldPersistTaps="handled">
            <Pressable onPress={() => setTeam('')} style={[s.teamChip, !picked && { backgroundColor: colors.ink, borderColor: colors.ink }]}>
              <Txt style={[s.teamChipText, !picked && { color: colors.white }]}>All {view.rows.length}</Txt>
            </Pressable>
            {teams.map((t) => {
              const on = picked === t.name;
              return (
                <Pressable key={t.name} onPress={() => setTeam(on ? '' : t.name)} style={[s.teamChip, on && { backgroundColor: t.color, borderColor: t.color }]}>
                  <View style={[s.teamDot, { backgroundColor: on ? colors.white : t.color }]} />
                  <Txt style={[s.teamChipText, on && { color: colors.white }]}>{t.name} {t.count}</Txt>
                </Pressable>
              );
            })}
          </ScrollView>
        </>
      ) : null}
      <View style={s.peopleGrid}>
        {shown.map((r) => {
          const c = colorOf(r.who);
          // With every team shown the second line says the team; inside one team it says the role.
          const line = picked || oneTeam ? r.kind || r.who : r.kind ? `${r.who} · ${r.kind}` : r.who;
          return (
            <View key={r.id} style={{ width: `${100 / columns}%`, padding: 3 }}>
              {/* a tap opens that person's own card, the same one "Kavita Singh ki detail do" brings up */}
              <Pressable
                onPress={() => onPerson(r.title)}
                accessibilityLabel={`Details of ${r.title}`}
                style={({ pressed }) => [s.personCard, r.on_leave && { backgroundColor: colors.warnSoft, borderColor: colors.warnSoft }, pressed && { opacity: 0.7 }]}
              >
                <View style={[s.personAvatar, { backgroundColor: `${c}22` }]}>
                  <Txt style={[s.personInitials, { color: c }]}>{initials(r.title)}</Txt>
                </View>
                <View style={{ flex: 1, minWidth: 0 }}>
                  <Txt numberOfLines={1} style={s.personName}>{r.title}</Txt>
                  <Txt numberOfLines={1} style={[s.personMeta, r.on_leave && { color: colors.warn }]}>
                    {r.on_leave ? r.status || 'On leave' : line}
                  </Txt>
                </View>
                {!r.on_leave && (r.assigned || r.late) ? (
                  <View style={[s.workBadge, { backgroundColor: r.late ? colors.danger : colors.accent }]}>
                    <Txt style={s.workBadgeText}>{r.assigned ?? 0}</Txt>
                  </View>
                ) : null}
              </Pressable>
            </View>
          );
        })}
      </View>
      <Txt variant="caption" style={{ marginTop: 6 }}>
        Tap a name, or say it, for that person’s details.{anyWork ? ' The number is open tasks; red means some are late.' : ''}
      </Txt>
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

/** One bar per day (or week). `short` = the lower version used where the card must fit the screen, so the list under it starts in view. */
function Bars({ series, short }: { series: DataView['series']; short?: boolean }) {
  const max = Math.max(1, ...series.map((p) => p.value));
  return (
    <View style={[s.bars, short && { height: 104, marginTop: 10 }]}>
      {series.map((p) => (
        <View key={p.date} style={s.barCol}>
          <Txt variant="caption" style={{ marginBottom: 4, color: p.heavy ? colors.danger : colors.muted }}>{p.value}</Txt>
          <View style={[s.barTrack, short && { height: 54 }]}>
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
  const { user } = useSession();
  const address = addressOf(user);
  const [busy, setBusy] = useState('');
  const [note, setNote] = useState('');
  const decide = async (r: ViewRow, accept: boolean) => {
    setBusy(`${r.id}:${accept}`);
    setNote('');
    try {
      await api(`/requests/${r.id}/${accept ? 'accept' : 'decline'}/`, { method: 'POST', body: {}, idempotencyKey: newIdempotencyKey() });
      say(accept ? `Accepted, ${address}. It's on your list.` : `Declined, ${address}. I've informed them.`, { interrupt: true });
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

interface DataCardProps {
  view: DataView;
  extraText?: string;
  onRemove?: () => void;
  compact?: boolean;
  /**
   * Fill the space given and never grow past it: the title, the numbers and the chart are on screen at once,
   * and only the detail underneath scrolls, inside the card. Used where JARVIS answers (the talk sheet).
   */
  fit?: boolean;
}

export function DataCard({ view, extraText, onRemove, compact, fit }: DataCardProps) {
  const { tileColumns, isTablet } = useLayout();
  const { showView } = useConversation();
  const { data } = useDashboard();
  const [open, setOpen] = useState<TaskItem | null>(null);
  const [busy, setBusy] = useState('');
  const [note, setNote] = useState('');
  const [tools, setTools] = useState(false);
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
  // The picture for this kind of answer (the same whether the card fits the screen or flows in a page).
  const body = view.kind === 'performance' ? (
    <PerformanceChart rows={view.rows} />
  ) : view.layout === 'people' ? (
    <PeopleList view={view} columns={!fit && isTablet ? 3 : 2} onPerson={(name) => showView('person', name, `who=${encodeURIComponent(name)}`)} />
  ) : view.layout === 'timeline' || view.layout === 'busy' ? (
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
      <Bars series={view.series} short={fit} />
      {view.rows.length ? <Table view={{ ...view, layout: 'table', rows: view.rows.slice(0, 12) }} onOpen={openRow} /> : null}
    </>
  ) : view.layout === 'requests' ? (
    <Requests view={view} isMD={isMD} />
  ) : view.layout === 'stats' ? (
    view.rows.length ? <Table view={{ ...view, layout: 'table', rows: view.rows.slice(0, 8) }} onOpen={openRow} /> : null
  ) : (
    <>
      {view.series.length ? <Bars series={view.series} short={fit} /> : null}
      <Table view={view} onOpen={openRow} />
    </>
  );
  const downloads = (
    <>
      <Button title="CSV" kind="secondary" compact icon="grid-outline" loading={busy === 'csv'} onPress={() => run('csv')} style={{ marginRight: 6 }} />
      <Button title="PDF" kind="secondary" compact icon="document-outline" loading={busy === 'pdf'} onPress={() => run('pdf')} style={{ marginRight: 6 }} />
      <Button title="Share" kind="ghost" compact icon="share-social-outline" loading={busy === 'share'} onPress={() => run('share')} />
    </>
  );

  if (fit) {
    return (
      <View style={[s.card, s.cardFit]}>
        <View style={[s.head, { alignItems: 'center' }]}>
          {view.profile ? (
            <View style={[s.profileAvatar, view.profile.on_leave && { backgroundColor: colors.warnSoft }]}>
              <Txt style={[s.profileInitials, view.profile.on_leave && { color: colors.warn }]}>{initials(view.profile.name)}</Txt>
            </View>
          ) : null}
          <View style={{ flex: 1, minWidth: 0 }}>
            <Txt variant="title" numberOfLines={1} style={{ fontSize: 17, lineHeight: 24 }}>{view.title}</Txt>
            {view.subtitle ? <Txt variant="caption" numberOfLines={1} style={{ color: colors.muted }}>{view.subtitle}</Txt> : null}
          </View>
          <Pressable onPress={() => setTools((v) => !v)} hitSlop={8} accessibilityLabel="Download or share" style={({ pressed }) => [s.headBtn, tools && { backgroundColor: colors.accentSoft }, pressed && { opacity: 0.6 }]}>
            <Icon name="download-outline" size={19} color={tools ? colors.accent : colors.inkSoft} />
          </Pressable>
          {onRemove ? (
            <Pressable onPress={onRemove} hitSlop={8} accessibilityLabel="Close this card" style={({ pressed }) => [s.headBtn, pressed && { opacity: 0.6 }]}>
              <Icon name="close" size={19} color={colors.inkSoft} />
            </Pressable>
          ) : null}
        </View>
        {tools ? <View style={s.toolsRow}>{downloads}</View> : null}
        {showStats ? <StatStrip stats={view.stats} /> : null}
        <ScrollView style={s.fitBody} contentContainerStyle={{ paddingBottom: 6 }} nestedScrollEnabled showsVerticalScrollIndicator>
          {body}
        </ScrollView>
        {note ? <Txt variant="small" style={{ color: colors.danger, marginTop: 6 }}>{note}</Txt> : null}
        {open ? <TaskSheet item={open} onClose={() => setOpen(null)} /> : null}
      </View>
    );
  }

  return (
    <View style={[s.card, compact && { padding: 14 }]}>
      <View style={s.head}>
        {view.profile ? (
          <View style={[s.profileAvatar, view.profile.on_leave && { backgroundColor: colors.warnSoft }]}>
            <Txt style={[s.profileInitials, view.profile.on_leave && { color: colors.warn }]}>{initials(view.profile.name)}</Txt>
          </View>
        ) : null}
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
      {body}
      {extraText ? <Txt variant="body" style={{ marginTop: 12, lineHeight: 25 }}>{extraText}</Txt> : null}
      <View style={s.foot}>
        <Txt variant="caption" style={{ flex: 1 }}>Download</Txt>
        {downloads}
      </View>
      {note ? <Txt variant="small" style={{ color: colors.danger, marginTop: 6 }}>{note}</Txt> : null}
      {open ? <TaskSheet item={open} onClose={() => setOpen(null)} /> : null}
    </View>
  );
}

const s = StyleSheet.create({
  card: { backgroundColor: colors.surface, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line, padding: 16, marginBottom: 14, ...shadow },
  cardFit: { flex: 1, minHeight: 0, padding: 14, marginBottom: 0 },
  fitBody: { flex: 1, minHeight: 0, marginTop: 2 },
  headBtn: { width: 38, height: 38, borderRadius: 19, alignItems: 'center', justifyContent: 'center', marginLeft: 6, backgroundColor: colors.surfaceAlt },
  toolsRow: { flexDirection: 'row', alignItems: 'center', marginTop: 8 },
  stripRow: { flexDirection: 'row', columnGap: 6 },
  stripTile: { flex: 1, minWidth: 0, borderRadius: radius.sm, paddingVertical: 7, paddingHorizontal: 8 },
  stripValue: { fontSize: 18, fontWeight: '700', lineHeight: 24 },
  stripLabel: { fontSize: 10.5, fontWeight: '600', lineHeight: 14, opacity: 0.9 },
  insights: { flexDirection: 'row', flexWrap: 'wrap', gap: 6, marginBottom: 6 },
  insight: { flexDirection: 'row', alignItems: 'center', borderRadius: 999, paddingVertical: 4, paddingHorizontal: 9, maxWidth: '100%' },
  insightText: { fontSize: 11.5, fontWeight: '600', lineHeight: 16, marginLeft: 5, flexShrink: 1 },
  perfRow: { flexDirection: 'row', alignItems: 'center', minHeight: 36, paddingVertical: 5, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: colors.line },
  perfName: { width: '30%', paddingRight: 8 },
  perfNameText: { fontSize: 13, fontWeight: '600', color: colors.text, lineHeight: 17 },
  perfDept: { fontSize: 10.5, color: colors.faint, lineHeight: 13 },
  perfBarWrap: { flex: 1, minWidth: 0 },
  perfTrack: { height: 10, borderRadius: 5, backgroundColor: colors.surfaceAlt, overflow: 'hidden' },
  perfFill: { height: 10, borderRadius: 5, backgroundColor: colors.accent },
  perfCount: { width: 40, textAlign: 'right', fontSize: 12.5, fontWeight: '700', color: colors.ink },
  perfLate: { width: 52, height: 22, borderRadius: 11, marginLeft: 6, alignItems: 'center', justifyContent: 'center' },
  perfLateText: { fontSize: 11, fontWeight: '700' },
  profileAvatar: { width: 40, height: 40, borderRadius: 20, backgroundColor: colors.accentSoft, alignItems: 'center', justifyContent: 'center', marginRight: 10 },
  profileInitials: { fontSize: 14, fontWeight: '700', color: colors.accent },
  shareBar: { flexDirection: 'row', height: 8, borderRadius: 4, overflow: 'hidden', columnGap: 2 },
  teamChips: { columnGap: 6, paddingTop: 8, paddingBottom: 6, paddingRight: 4 },
  teamChip: { flexDirection: 'row', alignItems: 'center', borderWidth: 1, borderColor: colors.line, backgroundColor: colors.surface, borderRadius: 999, paddingVertical: 5, paddingHorizontal: 10 },
  teamChipText: { fontSize: 12, fontWeight: '600', color: colors.text, lineHeight: 16 },
  teamDot: { width: 7, height: 7, borderRadius: 4, marginRight: 6 },
  peopleGrid: { flexDirection: 'row', flexWrap: 'wrap', marginHorizontal: -3 },
  personCard: { flexDirection: 'row', alignItems: 'center', borderWidth: 1, borderColor: colors.line, backgroundColor: colors.surface, borderRadius: 12, paddingVertical: 7, paddingHorizontal: 8, minHeight: 48 },
  personAvatar: { width: 30, height: 30, borderRadius: 15, alignItems: 'center', justifyContent: 'center', marginRight: 8 },
  personInitials: { fontSize: 11, fontWeight: '700' },
  personName: { fontSize: 13, fontWeight: '600', color: colors.text, lineHeight: 17 },
  personMeta: { fontSize: 10.5, color: colors.muted, lineHeight: 14 },
  workBadge: { minWidth: 20, height: 20, borderRadius: 10, paddingHorizontal: 5, alignItems: 'center', justifyContent: 'center', marginLeft: 4 },
  workBadgeText: { fontSize: 11, fontWeight: '700', color: colors.white, lineHeight: 14 },
  legend: { flexDirection: 'row', alignItems: 'center', marginTop: 8 },
  legendDot: { width: 9, height: 9, borderRadius: 5, marginRight: 5 },
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
