/**
 * The staff panel's people (TaskPanel), department by department: who they are, whether they are on leave, how much
 * open work each one holds. A tap opens the person: their tasks, their progress, and "give a task".
 */
import { Redirect, useRouter } from 'expo-router';
import React, { useEffect, useMemo, useState } from 'react';
import { Pressable, RefreshControl, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { Empty, Icon, IconButton, Tag, Txt } from '@/components/ui';
import { ApiError, api } from '@/lib/api';
import { useSession } from '@/lib/session';
import { deptTone, initials, leaveLabel } from '@/lib/staff';
import { colors, fonts, radius } from '@/lib/theme';
import type { StaffOption } from '@/lib/types';

export default function StaffScreen() {
  const { user } = useSession();
  if (!user) return <Redirect href="/login" />;
  return <StaffBody />;
}

function StaffBody() {
  const router = useRouter();
  const [staff, setStaff] = useState<StaffOption[] | null>(null);
  const [connected, setConnected] = useState(true);
  const [error, setError] = useState('');
  const [q, setQ] = useState('');
  const [syncing, setSyncing] = useState(false);

  const load = async () => {
    try {
      const r = await api<{ results: StaffOption[]; connected?: boolean }>('/staff/');
      setStaff(r.data.results);
      setConnected(r.data.connected !== false);
      setError('');
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not load the staff list.');
    }
  };
  useEffect(() => {
    const kick = setTimeout(load, 0);
    return () => clearTimeout(kick);
  }, []);
  const resync = async () => {
    setSyncing(true);
    try {
      await api('/staff/sync/', { method: 'POST' });
    } catch {
      // the list below still loads from what JARVIS has
    }
    await load();
    setSyncing(false);
  };

  const groups = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const rows = (staff ?? []).filter((s) => !needle || `${s.name} ${s.department} ${s.level ?? ''} ${s.role ?? ''}`.toLowerCase().includes(needle));
    const byDept = new Map<string, StaffOption[]>();
    for (const s of rows) {
      const d = s.department || 'Other';
      byDept.set(d, [...(byDept.get(d) ?? []), s]);
    }
    return [...byDept.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([dept, people]) => ({ dept, people: people.sort((a, b) => a.name.localeCompare(b.name)) }));
  }, [staff, q]);
  const total = staff?.length ?? 0;
  const away = staff?.filter((s) => s.on_leave).length ?? 0;
  const busy = staff?.filter((s) => (s.open_tasks ?? 0) > 0).length ?? 0;
  const back = () => (router.canGoBack() ? router.back() : router.replace('/'));

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }}>
      <View style={s.top}>
        <IconButton icon="chevron-back" label="Back" onPress={back} />
        <Txt variant="title" style={{ flex: 1, textAlign: 'center' }}>Staff</Txt>
        <IconButton icon={syncing ? 'hourglass-outline' : 'refresh-outline'} label="Refresh from the staff panel" onPress={resync} />
      </View>
      <ScrollView contentContainerStyle={{ padding: 20, paddingBottom: 60 }} refreshControl={<RefreshControl refreshing={false} onRefresh={load} tintColor={colors.accent} />}>
        {staff ? (
          <View style={s.statRow}>
            <Stat label="People" value={total} />
            <Stat label="With work" value={busy} tone={colors.accent} />
            <Stat label="On leave" value={away} tone={away ? colors.warn : colors.muted} />
          </View>
        ) : null}
        <View style={s.search}>
          <Icon name="search-outline" size={16} color={colors.muted} />
          <TextInput value={q} onChangeText={setQ} placeholder="Search a name or department" placeholderTextColor={colors.faint} style={s.searchInput} autoCorrect={false} />
          {q ? <Pressable onPress={() => setQ('')} hitSlop={8}><Icon name="close-circle" size={16} color={colors.faint} /></Pressable> : null}
        </View>
        {!connected ? <Txt variant="caption" style={{ color: colors.warn, marginBottom: 10 }}>The staff panel is not connected on this server; this is the test list.</Txt> : null}
        {error ? <Txt variant="small" style={{ color: colors.danger, marginBottom: 10 }}>{error}</Txt> : null}
        {!staff && !error ? <Txt variant="small" style={{ textAlign: 'center', marginTop: 30 }}>Loading…</Txt> : null}
        {staff && !groups.length ? <Empty text={q ? `Nobody matches “${q}”.` : 'No staff in the panel yet.'} /> : null}
        {groups.map((g) => (
          <View key={g.dept} style={{ marginBottom: 18 }}>
            <View style={s.deptHead}>
              <View style={[s.deptDot, { backgroundColor: deptTone(g.dept) }]} />
              <Txt variant="label">{g.dept}</Txt>
              <Txt variant="caption" style={{ marginLeft: 8 }}>{g.people.length}</Txt>
            </View>
            <View style={s.listCard}>
              {g.people.map((p, i) => (
                <Pressable key={p.id} onPress={() => router.push({ pathname: '/staff/[id]', params: { id: String(p.id) } })}
                  style={({ pressed }) => [s.row, i < g.people.length - 1 && s.rowLine, pressed && { backgroundColor: colors.surfaceAlt }]}>
                  <View style={[s.avatar, { backgroundColor: deptTone(g.dept) + '22' }]}>
                    <Txt style={{ color: deptTone(g.dept), fontFamily: fonts.semibold, fontSize: 14 }}>{initials(p.name)}</Txt>
                  </View>
                  <View style={{ flex: 1 }}>
                    <View style={{ flexDirection: 'row', alignItems: 'center' }}>
                      <Txt variant="heading" numberOfLines={1} style={{ flexShrink: 1 }}>{p.name}</Txt>
                      {p.self ? <Tag label="You" color={colors.accent} bg={colors.accentSoft} /> : null}
                    </View>
                    <Txt variant="caption" numberOfLines={1}>{[p.level, p.role].filter(Boolean).join(' · ') || p.designation || ''}</Txt>
                    {p.on_leave ? <Tag label={leaveLabel(p)} icon="airplane-outline" color={colors.warn} bg={colors.warnSoft} /> : null}
                  </View>
                  <View style={{ alignItems: 'flex-end', marginLeft: 8 }}>
                    <Txt style={[s.count, { color: p.open_tasks ? colors.ink : colors.faint }]}>{p.open_tasks ?? 0}</Txt>
                    <Txt variant="caption">open</Txt>
                  </View>
                  <Icon name="chevron-forward" size={16} color={colors.faint} />
                </Pressable>
              ))}
            </View>
          </View>
        ))}
        {staff ? <Txt variant="caption" style={{ textAlign: 'center', marginTop: 6 }}>Say “Hey JARVIS, Nikki ko bolo…” to give anyone work by voice.</Txt> : null}
      </ScrollView>
    </SafeAreaView>
  );
}

function Stat({ label, value, tone = colors.ink }: { label: string; value: number; tone?: string }) {
  return (
    <View style={s.stat}>
      <Txt style={[s.statValue, { color: tone }]}>{String(value)}</Txt>
      <Txt variant="caption">{label}</Txt>
    </View>
  );
}

const s = StyleSheet.create({
  top: { flexDirection: 'row', alignItems: 'center', paddingHorizontal: 12, paddingTop: 6 },
  statRow: { flexDirection: 'row', gap: 10, marginBottom: 14 },
  stat: { flex: 1, backgroundColor: colors.surface, borderRadius: radius.md, borderWidth: 1, borderColor: colors.line, padding: 12 },
  statValue: { fontSize: 22, fontFamily: fonts.semibold, lineHeight: 28 },
  search: { flexDirection: 'row', alignItems: 'center', backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, borderRadius: 999, paddingHorizontal: 14, paddingVertical: 8, marginBottom: 16 },
  searchInput: { flex: 1, marginLeft: 8, color: colors.text, fontFamily: fonts.regular, fontSize: 15, paddingVertical: 0 },
  deptHead: { flexDirection: 'row', alignItems: 'center', marginBottom: 8, marginLeft: 2 },
  deptDot: { width: 8, height: 8, borderRadius: 4, marginRight: 8 },
  listCard: { backgroundColor: colors.surface, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line, overflow: 'hidden' },
  row: { flexDirection: 'row', alignItems: 'center', paddingVertical: 12, paddingHorizontal: 12 },
  rowLine: { borderBottomWidth: 1, borderBottomColor: colors.line },
  avatar: { width: 40, height: 40, borderRadius: 20, alignItems: 'center', justifyContent: 'center', marginRight: 12 },
  count: { fontSize: 18, fontFamily: fonts.semibold, lineHeight: 22 },
});
