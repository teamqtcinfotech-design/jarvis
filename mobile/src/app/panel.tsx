/**
 * Partners & franchise: the company's own records from the main software, read through the staff panel's door,
 * view only, masked on their side (no PAN, Aadhaar, bank or phone ever). Pick a report, search a name or an ID,
 * tap a row for every column it has.
 */
import { Redirect, useRouter } from 'expo-router';
import React, { useEffect, useState } from 'react';
import { Pressable, RefreshControl, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { Chip, Empty, Icon, IconButton, Txt } from '@/components/ui';
import { ApiError, api } from '@/lib/api';
import { useSession } from '@/lib/session';
import { colors, fonts, radius } from '@/lib/theme';
import type { DataView, ViewRow } from '@/lib/types';

const KINDS: { key: string; label: string; icon: string }[] = [
  { key: 'partners', label: 'Partners', icon: 'people-outline' },
  { key: 'franchises', label: 'Franchises', icon: 'storefront-outline' },
  { key: 'franchise_requests', label: 'Requests', icon: 'mail-unread-outline' },
  { key: 'franchise_onboarding', label: 'Onboarding', icon: 'walk-outline' },
  { key: 'payments', label: 'Payments', icon: 'cash-outline' },
  { key: 'withdrawal_payouts', label: 'Withdrawals', icon: 'arrow-undo-outline' },
  { key: 'commissions', label: 'Commissions', icon: 'pie-chart-outline' },
  { key: 'agreements', label: 'Agreements', icon: 'document-text-outline' },
  { key: 'franchise_targets', label: 'Targets', icon: 'flag-outline' },
  { key: 'franchise_pipeline', label: 'Pipeline', icon: 'git-branch-outline' },
  { key: 'partner_ledger', label: 'Ledger', icon: 'book-outline' },
  { key: 'tds_sheet', label: 'TDS', icon: 'receipt-outline' },
];

type PanelView = DataView & { panel?: { kind: string; label: string; total: number; page?: number; pages?: number; hidden_columns?: number; totals?: unknown; as_of?: string; query?: string; columns: [string, string][] } };

export default function PanelScreen() {
  const { user } = useSession();
  if (!user) return <Redirect href="/login" />;
  if (user.role !== 'MD') return <Redirect href="/" />;
  return <PanelBody />;
}

function PanelBody() {
  const router = useRouter();
  const [kind, setKind] = useState('partners');
  const [q, setQ] = useState('');
  const [asked, setAsked] = useState('');
  const [page, setPage] = useState(1);
  const [view, setView] = useState<PanelView | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [open, setOpen] = useState<string | null>(null);

  const load = async (k = kind, words = asked, pg = page) => {
    setLoading(true);
    setError('');
    try {
      const r = await api<PanelView>(`/views/panel/?group=${encodeURIComponent(k)}&words=${encodeURIComponent(words)}&page=${pg}`);
      setView(r.data);
    } catch (e) {
      setView(null);
      setError(e instanceof ApiError ? e.message : 'Could not read the panel.');
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    const kick = setTimeout(() => load(kind, asked, page), 0);
    return () => clearTimeout(kick);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- on each report, search or page
  }, [kind, asked, page]);
  const pages = view?.panel?.pages ?? 1;
  const shownPage = view?.panel?.page ?? page;

  const back = () => (router.canGoBack() ? router.back() : router.replace('/'));
  const label = KINDS.find((k) => k.key === kind)?.label ?? kind;
  const columns = view?.columns ?? [];
  const allColumns = view?.panel?.columns ?? columns.map((c) => [c.key, c.label] as [string, string]);
  const cell = (row: ViewRow, key: string) => String((row as unknown as Record<string, unknown>)[key] ?? '');

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }}>
      <View style={s.top}>
        <IconButton icon="chevron-back" label="Back" onPress={back} />
        <Txt variant="title" style={{ flex: 1, textAlign: 'center' }}>Partners & franchise</Txt>
        <IconButton icon="refresh-outline" label="Refresh" onPress={() => load()} />
      </View>
      <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ paddingHorizontal: 20, columnGap: 6, alignItems: 'center' }} style={{ flexGrow: 0, height: 56 }}>
        {KINDS.map((k) => <Chip key={k.key} label={k.label} icon={k.icon} selected={kind === k.key} onPress={() => { setOpen(null); setPage(1); setKind(k.key); }} color={colors.accent} />)}
      </ScrollView>
      <View style={s.search}>
        <Icon name="search-outline" size={16} color={colors.muted} />
        <TextInput value={q} onChangeText={setQ} onSubmitEditing={() => { setPage(1); setAsked(q.trim()); }} returnKeyType="search" placeholder={`Search ${label.toLowerCase()} by name or ID`} placeholderTextColor={colors.faint} style={s.searchInput} autoCorrect={false} autoCapitalize="none" />
        {q || asked ? <Pressable onPress={() => { setQ(''); setAsked(''); setPage(1); }} hitSlop={8}><Icon name="close-circle" size={16} color={colors.faint} /></Pressable> : null}
      </View>
      <ScrollView contentContainerStyle={{ padding: 20, paddingTop: 8, paddingBottom: 60 }} refreshControl={<RefreshControl refreshing={loading} onRefresh={() => load()} tintColor={colors.accent} />} keyboardShouldPersistTaps="handled">
        {error ? <Txt variant="small" style={{ color: colors.danger, marginBottom: 10 }}>{error}</Txt> : null}
        {view ? (
          <View style={s.head}>
            <Txt variant="heading">{view.title}</Txt>
            <Txt variant="caption">{view.subtitle}{view.panel?.hidden_columns ? ` · ${view.panel.hidden_columns} private columns hidden` : ''}</Txt>
          </View>
        ) : null}
        {view?.panel?.totals && typeof view.panel.totals === 'object' && !Array.isArray(view.panel.totals) ? (
          <View style={s.totals}>
            {Object.entries(view.panel.totals as Record<string, unknown>).slice(0, 4).map(([k, v]) => (
              <View key={k} style={s.total}><Txt style={s.totalValue}>{String(v)}</Txt><Txt variant="caption">{k.replace(/_/g, ' ')}</Txt></View>
            ))}
          </View>
        ) : null}
        {!view && !error ? <Txt variant="small" style={{ textAlign: 'center', marginTop: 30 }}>{loading ? 'Reading the panel…' : ''}</Txt> : null}
        {view && !view.rows.length ? <Empty text={asked ? `Nothing in ${label.toLowerCase()} for “${asked}”.` : `Nothing in ${label.toLowerCase()}.`} /> : null}
        {view?.rows.map((row) => {
          const isOpen = open === row.id;
          const shown = isOpen ? allColumns : allColumns.slice(1, 4);
          return (
            <Pressable key={row.id} onPress={() => setOpen(isOpen ? null : row.id)} style={({ pressed }) => [s.row, pressed && { opacity: 0.85 }]}>
              <View style={{ flex: 1 }}>
                <Txt variant="heading" numberOfLines={isOpen ? undefined : 1}>{row.title}</Txt>
                {shown.map(([key, colLabel]) => (cell(row, key) ? (
                  <View key={key} style={s.line}>
                    <Txt variant="caption" style={{ width: 110 }} numberOfLines={1}>{colLabel}</Txt>
                    <Txt variant="small" style={{ flex: 1 }} numberOfLines={isOpen ? undefined : 1}>{cell(row, key)}</Txt>
                  </View>
                ) : null))}
              </View>
              <Icon name={isOpen ? 'chevron-up' : 'chevron-down'} size={16} color={colors.faint} />
            </Pressable>
          );
        })}
        {view && pages > 1 ? (
          <View style={s.pager}>
            <Pressable disabled={shownPage <= 1 || loading} onPress={() => { setOpen(null); setPage(Math.max(1, shownPage - 1)); }} style={[s.pageBtn, (shownPage <= 1 || loading) && { opacity: 0.4 }]}>
              <Icon name="chevron-back" size={16} color={colors.ink} />
              <Txt variant="small">Previous</Txt>
            </Pressable>
            <Txt variant="caption">Page {shownPage} of {pages}</Txt>
            <Pressable disabled={shownPage >= pages || loading} onPress={() => { setOpen(null); setPage(shownPage + 1); }} style={[s.pageBtn, (shownPage >= pages || loading) && { opacity: 0.4 }]}>
              <Txt variant="small">Next</Txt>
              <Icon name="chevron-forward" size={16} color={colors.ink} />
            </Pressable>
          </View>
        ) : null}
        {view ? <Txt variant="caption" style={{ textAlign: 'center', marginTop: 16 }}>View only · from the main software{view.panel?.as_of ? ` · as of ${new Date(view.panel.as_of).toLocaleTimeString('en-IN', { hour: 'numeric', minute: '2-digit' })}` : ''}</Txt> : null}
      </ScrollView>
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  top: { flexDirection: 'row', alignItems: 'center', paddingHorizontal: 12, paddingTop: 6 },
  search: { flexDirection: 'row', alignItems: 'center', backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, borderRadius: 999, paddingHorizontal: 14, paddingVertical: 8, marginHorizontal: 20, marginTop: 6 },
  searchInput: { flex: 1, marginLeft: 8, color: colors.text, fontFamily: fonts.regular, fontSize: 15, paddingVertical: 0 },
  head: { marginBottom: 10 },
  totals: { flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginBottom: 12 },
  total: { flexGrow: 1, minWidth: '45%', backgroundColor: colors.surface, borderRadius: radius.md, borderWidth: 1, borderColor: colors.line, padding: 10 },
  totalValue: { fontSize: 18, fontFamily: fonts.semibold, color: colors.ink, lineHeight: 22 },
  row: { flexDirection: 'row', alignItems: 'flex-start', backgroundColor: colors.surface, borderRadius: radius.md, borderWidth: 1, borderColor: colors.line, padding: 12, marginBottom: 8 },
  line: { flexDirection: 'row', alignItems: 'flex-start', marginTop: 4 },
  pager: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginTop: 12 },
  pageBtn: { flexDirection: 'row', alignItems: 'center', gap: 4, paddingVertical: 8, paddingHorizontal: 12, borderRadius: 999, borderWidth: 1, borderColor: colors.line, backgroundColor: colors.surface },
});
