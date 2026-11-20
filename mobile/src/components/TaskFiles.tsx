/**
 * The photos, documents and voice notes on a task (Nov 2026): listed with who added them, opened in place (a photo
 * full-screen, anything else through the phone's own apps), added from the gallery or the camera, removed by
 * whoever added them or the task's people. Every byte travels with the sign-in and is encrypted on the server.
 */
import { File, Paths } from 'expo-file-system';
import * as ImagePicker from 'expo-image-picker';
import * as Sharing from 'expo-sharing';
import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Image, Modal, Pressable, StyleSheet, View } from 'react-native';

import { ApiError, api, apiUpload, authHeaders, fileUrl } from '@/lib/api';
import { dataChanged } from '@/lib/bus';
import { colors, radius } from '@/lib/theme';
import type { TaskFile } from '@/lib/types';

import { Button, Icon, Txt } from './ui';

const sizeLabel = (n: number) => (n >= 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
const fileIcon: Record<TaskFile['kind'], string> = { photo: 'image-outline', voice: 'mic-outline', document: 'document-text-outline' };

export function TaskFiles({ scope, taskId, canAdd, initialCount }: { scope: 'private' | 'shared'; taskId: string; canAdd: boolean; initialCount?: number }) {
  // Nothing on it and nobody here may add one: no request at all; otherwise the list is fetched.
  const nothingToShow = initialCount === 0 && !canAdd;
  const [files, setFiles] = useState<TaskFile[] | null>(nothingToShow ? [] : null);
  const [busy, setBusy] = useState('');
  const [message, setMessage] = useState('');
  const [open, setOpen] = useState<TaskFile | null>(null);
  const base = `/${scope === 'private' ? 'private-tasks' : 'shared-tasks'}/${taskId}/files/`;

  const refresh = useCallback(async () => {
    try {
      const r = await api<{ results: TaskFile[] }>(base);
      setFiles(r.data.results);
    } catch {
      setFiles((prev) => prev ?? []);
    }
  }, [base]);

  useEffect(() => {
    if (nothingToShow) return undefined;
    let alive = true;
    api<{ results: TaskFile[] }>(base)
      .then((r) => alive && setFiles(r.data.results))
      .catch(() => alive && setFiles((prev) => prev ?? []));
    return () => {
      alive = false;
    };
  }, [base, nothingToShow]);

  const upload = async (picked: ImagePicker.ImagePickerAsset) => {
    setBusy('upload');
    setMessage('');
    try {
      const name = picked.fileName || `photo-${Date.now()}.jpg`;
      const type = picked.mimeType || 'image/jpeg';
      await apiUpload(base, { uri: picked.uri, name, type });
      await refresh();
      dataChanged.emit();
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : 'Could not upload. Check the connection.');
    } finally {
      setBusy('');
    }
  };

  const pick = async (camera: boolean) => {
    try {
      const perm = camera ? await ImagePicker.requestCameraPermissionsAsync() : await ImagePicker.requestMediaLibraryPermissionsAsync();
      if (!perm.granted) {
        setMessage(camera ? 'Camera permission is needed to take a photo.' : 'Photo permission is needed to pick a photo.');
        return;
      }
      const result = camera
        ? await ImagePicker.launchCameraAsync({ quality: 0.7, allowsEditing: false })
        : await ImagePicker.launchImageLibraryAsync({ mediaTypes: ['images'], quality: 0.7, allowsMultipleSelection: false });
      if (result.canceled || !result.assets?.[0]) return;
      await upload(result.assets[0]);
    } catch {
      setMessage('Could not open the picker.');
    }
  };

  const openFile = async (f: TaskFile) => {
    if (f.kind === 'photo') {
      setOpen(f);
      return;
    }
    // Anything else: fetched with the sign-in into the cache and handed to the phone's own apps.
    setBusy(`open:${f.id}`);
    try {
      const target = new File(Paths.cache, f.name.replace(/[^\w.\- ]+/g, '_'));
      if (target.exists) target.delete();
      const saved = await File.downloadFileAsync(fileUrl(f.id), target, { headers: authHeaders() });
      if (await Sharing.isAvailableAsync()) await Sharing.shareAsync(saved.uri, { mimeType: f.content_type, dialogTitle: f.name });
    } catch {
      setMessage('Could not open that file.');
    } finally {
      setBusy('');
    }
  };

  const removeFile = (f: TaskFile) => {
    Alert.alert('Remove this file?', f.name, [
      { text: 'Keep', style: 'cancel' },
      {
        text: 'Remove',
        style: 'destructive',
        onPress: async () => {
          try {
            await api(`/files/${f.id}/`, { method: 'DELETE' });
            await refresh();
            dataChanged.emit();
          } catch (e) {
            setMessage(e instanceof ApiError ? e.message : 'Could not remove it.');
          }
        },
      },
    ]);
  };

  if (files === null && !canAdd) return null;
  if (!canAdd && files && files.length === 0) return null;
  return (
    <View style={s.wrap}>
      <View style={s.head}>
        <Txt variant="label">Files{files?.length ? ` · ${files.length}` : ''}</Txt>
        {canAdd ? (
          <View style={{ flexDirection: 'row' }}>
            <Pressable onPress={() => pick(true)} hitSlop={8} disabled={!!busy} style={s.addBtn} accessibilityLabel="Take a photo">
              <Icon name="camera-outline" size={16} color={colors.accent} />
            </Pressable>
            <Pressable onPress={() => pick(false)} hitSlop={8} disabled={!!busy} style={[s.addBtn, { marginLeft: 6 }]} accessibilityLabel="Add a photo">
              <Icon name="image-outline" size={16} color={colors.accent} />
              <Txt variant="caption" style={{ marginLeft: 4, color: colors.accent }}>{busy === 'upload' ? 'Uploading…' : 'Add photo'}</Txt>
            </Pressable>
          </View>
        ) : null}
      </View>
      {(files ?? []).map((f) => (
        <Pressable key={f.id} onPress={() => openFile(f)} onLongPress={canAdd ? () => removeFile(f) : undefined} style={s.row}>
          {f.kind === 'photo' ? (
            <Image source={{ uri: fileUrl(f.id), headers: authHeaders() }} style={s.thumb} />
          ) : (
            <View style={[s.thumb, s.thumbIcon]}><Icon name={fileIcon[f.kind]} size={18} color={colors.inkSoft} /></View>
          )}
          <View style={{ flex: 1, minWidth: 0 }}>
            <Txt variant="small" numberOfLines={1}>{f.name}</Txt>
            <Txt variant="caption">{sizeLabel(f.size)}{f.uploader ? ` · ${f.uploader}` : ''}</Txt>
          </View>
          <Icon name={busy === `open:${f.id}` ? 'hourglass-outline' : 'chevron-forward'} size={16} color={colors.faint} />
        </Pressable>
      ))}
      {files === null ? <Txt variant="caption">Loading…</Txt> : null}
      {canAdd && files && files.length === 0 ? <Txt variant="caption">A photo of the bill, the signboard, the receipt: add it here.</Txt> : null}
      {canAdd && files && files.length > 0 ? <Txt variant="caption" style={{ marginTop: 4 }}>Long-press a file to remove it.</Txt> : null}
      {message ? <Txt variant="small" style={{ marginTop: 6, color: colors.warn }}>{message}</Txt> : null}
      <Modal visible={!!open} transparent animationType="fade" onRequestClose={() => setOpen(null)}>
        <Pressable style={s.viewer} onPress={() => setOpen(null)}>
          {open ? <Image source={{ uri: fileUrl(open.id), headers: authHeaders() }} style={s.full} resizeMode="contain" /> : null}
          <View style={s.viewerBar}>
            <Txt variant="small" style={{ color: colors.white, flex: 1 }} numberOfLines={1}>{open?.name}</Txt>
            <Button title="Close" kind="ghost" compact onPress={() => setOpen(null)} />
          </View>
        </Pressable>
      </Modal>
    </View>
  );
}

const s = StyleSheet.create({
  wrap: { marginTop: 12 },
  head: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 },
  addBtn: { flexDirection: 'row', alignItems: 'center', paddingHorizontal: 8, paddingVertical: 4, borderRadius: 999, backgroundColor: colors.accentSoft },
  row: { flexDirection: 'row', alignItems: 'center', paddingVertical: 6 },
  thumb: { width: 40, height: 40, borderRadius: radius.sm, marginRight: 10, backgroundColor: colors.surfaceAlt },
  thumbIcon: { alignItems: 'center', justifyContent: 'center' },
  viewer: { flex: 1, backgroundColor: 'rgba(0,0,0,0.92)', justifyContent: 'center' },
  full: { width: '100%', height: '80%' },
  viewerBar: { position: 'absolute', bottom: 24, left: 16, right: 16, flexDirection: 'row', alignItems: 'center' },
});
