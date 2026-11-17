/**
 * Settings → Sections: the folders work is filed in (GST, Accounts, Franchise…) and the person's own personal
 * folders. Anyone makes a work section by naming it (here or by voice: "GST section banao"); renaming, merging and
 * deleting a work section is the MD's. JARVIS files every task from its words and learns each section's words.
 */
import React, { useEffect, useState } from 'react';
import { Alert, Pressable, StyleSheet, TextInput, View } from 'react-native';

import { api } from '@/lib/api';
import { useSession } from '@/lib/session';
import { colors, radius } from '@/lib/theme';
import type { SectionItem } from '@/lib/types';
import { useDashboard } from '@/lib/useDashboard';

import { Button, Card, Chip, Icon, Txt } from './ui';

export function SectionsPanel() {
  const { user } = useSession();
  const isMD = user?.role === 'MD';
  const { reload } = useDashboard();
  const [list, setList] = useState<SectionItem[] | null>(null);
  const [error, setError] = useState('');
  const [newName, setNewName] = useState('');
  const [personal, setPersonal] = useState(false);
  const [busy, setBusy] = useState('');
  const [renaming, setRenaming] = useState<number | null>(null);
  const [renameText, setRenameText] = useState('');
  const [merging, setMerging] = useState<number | null>(null);

  const load = async () => {
    try {
      const r = await api<{ results: SectionItem[] }>('/sections/');
      setList(r.data.results.filter((sct) => sct.active));
      setError('');
    } catch {
      setError('Could not load the sections.');
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
    if (await act('new', () => api('/sections/', { method: 'POST', body: { name: newName.trim(), kind: personal ? 'PERSONAL' : 'WORK' } }))) setNewName('');
  };
  const rename = async (sct: SectionItem) => {
    if (await act(`rename-${sct.id}`, () => api(`/sections/${sct.id}/`, { method: 'PATCH', body: { name: renameText.trim() } }))) setRenaming(null);
  };
  const merge = (src: SectionItem, dst: SectionItem) =>
    Alert.alert(`Merge ${src.name} into ${dst.name}?`, `Its ${src.open} open ${src.open === 1 ? 'task moves' : 'tasks move'} to ${dst.name}. This cannot be undone.`, [
      { text: 'Keep both', style: 'cancel' },
      { text: 'Merge', style: 'destructive', onPress: async () => { if (await act(`merge-${src.id}`, () => api(`/sections/${src.id}/`, { method: 'PATCH', body: { merge_into: dst.id } }))) setMerging(null); } },
    ]);
  const remove = (sct: SectionItem) =>
    Alert.alert(`Delete the ${sct.name} section?`, sct.open ? `Its ${sct.open} open ${sct.open === 1 ? 'task keeps' : 'tasks keep'} going, without a section.` : 'Nothing is in it.', [
      { text: 'Keep it', style: 'cancel' },
      { text: 'Delete', style: 'destructive', onPress: () => act(`del-${sct.id}`, () => api(`/sections/${sct.id}/`, { method: 'DELETE' })) },
    ]);

  if (!list) return <Txt variant="small" style={{ textAlign: 'center', marginTop: 30 }}>{error || 'Loading…'}</Txt>;
  const work = list.filter((sct) => sct.kind === 'WORK');
  const mine = list.filter((sct) => sct.kind === 'PERSONAL');

  const row = (sct: SectionItem, canChange: boolean) => (
    <View key={sct.id} style={s.row}>
      <View style={s.head}>
        <View style={[s.dot, { backgroundColor: sct.colour || colors.accent }]} />
        {renaming === sct.id ? (
          <>
            <TextInput value={renameText} onChangeText={setRenameText} style={[s.input, { flex: 1 }]} autoCapitalize="words" maxLength={60} autoFocus />
            <Button title="Save" compact disabled={!renameText.trim()} loading={busy === `rename-${sct.id}`} onPress={() => rename(sct)} style={{ marginLeft: 6 }} />
            <Button title="Cancel" kind="ghost" compact onPress={() => setRenaming(null)} style={{ marginLeft: 4 }} />
          </>
        ) : (
          <View style={{ flex: 1 }}>
            <Txt variant="heading" style={{ fontSize: 15 }}>{sct.name}</Txt>
            <Txt variant="caption">{sct.open} open{sct.department ? ` · ${sct.department} department` : ''}{sct.aliases.length ? ` · knows: ${sct.aliases.slice(0, 5).join(', ')}` : ''}</Txt>
          </View>
        )}
      </View>
      {canChange && renaming !== sct.id ? (
        <View style={s.actions}>
          <Pressable onPress={() => { setRenaming(sct.id); setRenameText(sct.name); }} style={s.link} hitSlop={6}>
            <Icon name="create-outline" size={15} color={colors.inkSoft} />
            <Txt variant="small" style={{ marginLeft: 5 }}>Rename</Txt>
          </Pressable>
          {sct.kind === 'WORK' && work.length > 1 ? (
            <Pressable onPress={() => setMerging(merging === sct.id ? null : sct.id)} style={s.link} hitSlop={6}>
              <Icon name="git-merge-outline" size={15} color={colors.inkSoft} />
              <Txt variant="small" style={{ marginLeft: 5 }}>Merge into…</Txt>
            </Pressable>
          ) : null}
          <Pressable onPress={() => remove(sct)} style={s.link} hitSlop={6}>
            <Icon name="trash-outline" size={15} color={colors.danger} />
            <Txt variant="small" style={{ color: colors.danger, marginLeft: 5 }}>Delete</Txt>
          </Pressable>
        </View>
      ) : null}
      {merging === sct.id ? (
        <View style={s.chips}>
          {work.filter((o) => o.id !== sct.id).map((o) => <Chip key={o.id} label={o.name} icon="folder-outline" onPress={() => merge(sct, o)} />)}
        </View>
      ) : null}
    </View>
  );

  return (
    <>
      <Txt variant="small" style={{ marginBottom: 10 }}>
        JARVIS files every task into a section from its words, and learns. By voice: “GST section banao”, “ise Partners mein daal do”, “GST ka pending dikhao”.
      </Txt>
      <View style={s.newRow}>
        <TextInput value={newName} onChangeText={setNewName} placeholder={personal ? 'New personal section, e.g. Ghar' : 'New section, e.g. Franchise'} placeholderTextColor={colors.faint} style={[s.input, { flex: 1 }]} autoCapitalize="words" maxLength={60} />
        <Button title="Add" compact disabled={!newName.trim() || !!busy} loading={busy === 'new'} onPress={create} style={{ marginLeft: 8 }} />
      </View>
      <View style={[s.chips, { marginTop: 0, marginBottom: 12 }]}>
        <Chip label="Work (shared)" selected={!personal} onPress={() => setPersonal(false)} />
        <Chip label="Personal (only me)" selected={personal} onPress={() => setPersonal(true)} />
      </View>
      {error ? <Txt variant="small" style={{ color: colors.danger, marginBottom: 8 }}>{error}</Txt> : null}
      <Card style={{ marginBottom: 12 }}>
        <Txt variant="label" style={{ marginBottom: 4 }}>Work sections</Txt>
        {work.length ? work.map((sct) => row(sct, isMD)) : <Txt variant="caption">None yet.</Txt>}
        {!isMD ? <Txt variant="caption" style={{ marginTop: 8 }}>Only {user?.address || 'Sir'} renames, merges or deletes these; you can add one.</Txt> : null}
      </Card>
      <Card>
        <Txt variant="label" style={{ marginBottom: 4 }}>Your personal sections</Txt>
        {mine.length ? mine.map((sct) => row(sct, true)) : <Txt variant="caption">None yet. Only you ever see these.</Txt>}
      </Card>
    </>
  );
}

const s = StyleSheet.create({
  newRow: { flexDirection: 'row', alignItems: 'center', marginBottom: 8 },
  input: { backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, borderRadius: radius.md, paddingHorizontal: 12, paddingVertical: 9, fontSize: 15, color: colors.text },
  row: { paddingVertical: 8, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: colors.line },
  head: { flexDirection: 'row', alignItems: 'center' },
  dot: { width: 10, height: 10, borderRadius: 5, marginRight: 10 },
  chips: { flexDirection: 'row', flexWrap: 'wrap', marginTop: 8, gap: 6 },
  actions: { flexDirection: 'row', marginTop: 8, gap: 18 },
  link: { flexDirection: 'row', alignItems: 'center' },
});
