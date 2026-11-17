/**
 * Settings → Departments: the groups of people on the team. Everyone may look; only the MD changes them, here or by
 * voice ("Farm department banao aur Kiran ko add kar do"). The manager and the PA are never inside one: they sit on
 * their own Management line.
 */
import React, { useEffect, useState } from 'react';
import { Alert, Pressable, StyleSheet, TextInput, View } from 'react-native';

import { api } from '@/lib/api';
import { useSession } from '@/lib/session';
import { colors, radius } from '@/lib/theme';
import type { DepartmentItem, TeamMember } from '@/lib/types';
import { useDashboard } from '@/lib/useDashboard';

import { Button, Card, Chip, Icon, Tag, Txt } from './ui';

const roleWord = (p: TeamMember) => (p.role === 'MG' ? 'Manager' : p.role === 'PA' ? 'PA' : p.title);

export function DepartmentsPanel() {
  const { user } = useSession();
  const isMD = user?.role === 'MD';
  const { reload } = useDashboard();
  const [data, setData] = useState<{ results: DepartmentItem[]; management: TeamMember[] } | null>(null);
  const [team, setTeam] = useState<TeamMember[]>([]);
  const [error, setError] = useState('');
  const [newName, setNewName] = useState('');
  const [busy, setBusy] = useState('');
  const [renaming, setRenaming] = useState<number | null>(null);
  const [renameText, setRenameText] = useState('');
  const [adding, setAdding] = useState<number | null>(null);

  const load = async () => {
    try {
      const [d, t] = await Promise.all([
        api<{ results: DepartmentItem[]; management: TeamMember[] }>('/departments/'),
        api<{ results: TeamMember[] }>('/team/'),
      ]);
      setData(d.data);
      setTeam(t.data.results.filter((p) => p.active && !['MD', 'PA', 'MG'].includes(p.role)));
      setError('');
    } catch {
      setError('Could not load the departments.');
    }
  };
  useEffect(() => {
    const kick = setTimeout(load, 0);
    return () => clearTimeout(kick);
  }, []);

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label);
    setError('');
    try {
      await fn();
      await load();
      reload().catch(() => undefined);
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save.');
      return false;
    } finally {
      setBusy('');
    }
  };
  const create = async () => {
    if (await act('new', () => api('/departments/', { method: 'POST', body: { name: newName.trim() } }))) setNewName('');
  };
  const addMember = (d: DepartmentItem, p: TeamMember) => act(`add-${d.id}`, () => api(`/departments/${d.id}/`, { method: 'PATCH', body: { add: [p.id] } }));
  const removeMember = (d: DepartmentItem, p: TeamMember) =>
    Alert.alert(`Take ${p.name} out of ${d.name}?`, 'They stay on the team, just not in this department.', [
      { text: 'Keep', style: 'cancel' },
      { text: 'Take out', style: 'destructive', onPress: () => act(`rm-${d.id}`, () => api(`/departments/${d.id}/`, { method: 'PATCH', body: { remove: [p.id] } })) },
    ]);
  const rename = async (d: DepartmentItem) => {
    if (await act(`rename-${d.id}`, () => api(`/departments/${d.id}/`, { method: 'PATCH', body: { name: renameText.trim() } }))) setRenaming(null);
  };
  const remove = (d: DepartmentItem) =>
    Alert.alert(`Delete the ${d.name} department?`, `Its ${d.members.length} ${d.members.length === 1 ? 'person stays' : 'people stay'} on the team, just not in it. Its section keeps its work.`, [
      { text: 'Keep it', style: 'cancel' },
      { text: 'Delete', style: 'destructive', onPress: () => act(`del-${d.id}`, () => api(`/departments/${d.id}/`, { method: 'DELETE' })) },
    ]);

  if (!data) return <Txt variant="small" style={{ textAlign: 'center', marginTop: 30 }}>{error || 'Loading…'}</Txt>;
  const departments = data.results.filter((d) => d.active);
  return (
    <>
      <Txt variant="small" style={{ marginBottom: 10 }}>
        {isMD ? 'Say it by voice too: “Farm department banao aur Kiran ko add kar do”, “Nikki ko Accounts se hata do”.' : `Only ${user?.address || 'Sir'} changes departments. Ask JARVIS “Accounts mein kaun hai” any time.`}
      </Txt>
      <Card style={{ marginBottom: 12 }}>
        <Txt variant="label">Management</Txt>
        <View style={s.chips}>
          {data.management.map((p) => <Tag key={p.id} label={`${p.name} · ${roleWord(p)}`} icon="person-outline" color={colors.accentDeep} bg={colors.accentSoft} />)}
          {!data.management.length ? <Txt variant="caption">No manager or PA yet.</Txt> : null}
        </View>
        <Txt variant="caption" style={{ marginTop: 6 }}>They work across every department and are never inside one.</Txt>
      </Card>
      {isMD ? (
        <View style={s.newRow}>
          <TextInput value={newName} onChangeText={setNewName} placeholder="New department, e.g. Farm" placeholderTextColor={colors.faint} style={[s.input, { flex: 1 }]} autoCapitalize="words" maxLength={60} />
          <Button title="Add" compact disabled={!newName.trim() || !!busy} loading={busy === 'new'} onPress={create} style={{ marginLeft: 8 }} />
        </View>
      ) : null}
      {error ? <Txt variant="small" style={{ color: colors.danger, marginBottom: 8 }}>{error}</Txt> : null}
      {!departments.length ? <Txt variant="small" style={{ textAlign: 'center', marginTop: 20 }}>No departments yet.</Txt> : null}
      {departments.map((d) => {
        const memberIds = new Set(d.members.map((m) => m.id));
        const candidates = team.filter((p) => !memberIds.has(p.id));
        return (
          <Card key={d.id} style={{ marginBottom: 12 }}>
            <View style={s.head}>
              <View style={[s.dot, { backgroundColor: d.colour || colors.accent }]} />
              {renaming === d.id ? (
                <>
                  <TextInput value={renameText} onChangeText={setRenameText} style={[s.input, { flex: 1 }]} autoCapitalize="words" maxLength={60} autoFocus />
                  <Button title="Save" compact disabled={!renameText.trim()} loading={busy === `rename-${d.id}`} onPress={() => rename(d)} style={{ marginLeft: 6 }} />
                  <Button title="Cancel" kind="ghost" compact onPress={() => setRenaming(null)} style={{ marginLeft: 4 }} />
                </>
              ) : (
                <View style={{ flex: 1 }}>
                  <Txt variant="heading">{d.name}</Txt>
                  <Txt variant="caption">
                    {d.members.length} {d.members.length === 1 ? 'person' : 'people'} · {d.open} open{d.late ? ` · ${d.late} late` : ''}{d.section ? ` · section: ${d.section}` : ''}
                  </Txt>
                </View>
              )}
            </View>
            <View style={s.chips}>
              {d.members.map((m) => (
                <Chip key={m.id} label={`${m.name}${m.title ? ` · ${m.title}` : ''}`} icon={isMD ? 'close-circle-outline' : 'person-outline'} onPress={isMD ? () => removeMember(d, m) : undefined} />
              ))}
              {!d.members.length ? <Txt variant="caption">Nobody in it yet.</Txt> : null}
            </View>
            {isMD ? (
              <>
                <View style={s.actions}>
                  <Pressable onPress={() => setAdding(adding === d.id ? null : d.id)} style={s.link} hitSlop={6}>
                    <Icon name="person-add-outline" size={15} color={colors.accentDeep} />
                    <Txt variant="small" style={{ color: colors.accentDeep, marginLeft: 5 }}>Add person</Txt>
                  </Pressable>
                  <Pressable onPress={() => { setRenaming(d.id); setRenameText(d.name); }} style={s.link} hitSlop={6}>
                    <Icon name="create-outline" size={15} color={colors.inkSoft} />
                    <Txt variant="small" style={{ marginLeft: 5 }}>Rename</Txt>
                  </Pressable>
                  <Pressable onPress={() => remove(d)} style={s.link} hitSlop={6}>
                    <Icon name="trash-outline" size={15} color={colors.danger} />
                    <Txt variant="small" style={{ color: colors.danger, marginLeft: 5 }}>Delete</Txt>
                  </Pressable>
                </View>
                {adding === d.id ? (
                  <View style={s.chips}>
                    {candidates.map((p) => <Chip key={p.id} label={p.name} icon="add-circle-outline" onPress={() => addMember(d, p)} />)}
                    {!candidates.length ? <Txt variant="caption">Everyone on the team is in it already.</Txt> : null}
                  </View>
                ) : null}
              </>
            ) : null}
          </Card>
        );
      })}
    </>
  );
}

const s = StyleSheet.create({
  newRow: { flexDirection: 'row', alignItems: 'center', marginBottom: 12 },
  input: { backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, borderRadius: radius.md, paddingHorizontal: 12, paddingVertical: 9, fontSize: 15, color: colors.text },
  head: { flexDirection: 'row', alignItems: 'center' },
  dot: { width: 10, height: 10, borderRadius: 5, marginRight: 10 },
  chips: { flexDirection: 'row', flexWrap: 'wrap', marginTop: 8, gap: 6 },
  actions: { flexDirection: 'row', marginTop: 10, gap: 18 },
  link: { flexDirection: 'row', alignItems: 'center' },
});
