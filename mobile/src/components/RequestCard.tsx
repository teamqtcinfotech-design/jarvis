import React, { useState } from 'react';
import { StyleSheet, TextInput, View } from 'react-native';

import { ApiError, api, newIdempotencyKey } from '@/lib/api';
import { dataChanged } from '@/lib/bus';
import { twin } from '@/lib/lang';
import { addressHiOf, addressOf } from '@/lib/names';
import { useSession } from '@/lib/session';
import { say } from '@/lib/speech';
import { colors, radius } from '@/lib/theme';
import type { RequestItem } from '@/lib/types';

import { Button, Card, PriorityTag, Tag, Txt } from './ui';

const STATE_TAG: Record<string, { label: string; color: string; bg: string }> = {
  PENDING: { label: 'Waiting', color: colors.warn, bg: colors.warnSoft },
  ACCEPTED: { label: 'Accepted', color: colors.success, bg: colors.successSoft },
  DECLINED: { label: 'Declined', color: colors.danger, bg: colors.dangerSoft },
  WITHDRAWN: { label: 'Withdrawn', color: colors.muted, bg: colors.surfaceAlt },
  CONFLICT: { label: 'Needs resend', color: colors.danger, bg: colors.dangerSoft },
};

export function RequestCard({ request, mode }: { request: RequestItem; mode: 'md' | 'pa' }) {
  const { user } = useSession();
  const address = addressOf(user);
  const addressHi = addressHiOf(user);
  const [busy, setBusy] = useState<'' | 'accept' | 'decline' | 'withdraw'>('');
  const [error, setError] = useState('');
  // Declining asks for a word first: the other person hears why, and a slip of the thumb declines nothing.
  const [declining, setDeclining] = useState(false);
  const [note, setNote] = useState('');
  const tag = STATE_TAG[request.state];

  const act = async (what: 'accept' | 'decline' | 'withdraw') => {
    setBusy(what);
    setError('');
    try {
      await api(`/requests/${request.id}/${what}/`, {
        method: 'POST',
        body: what === 'decline' ? { note: note.trim() } : {},
        idempotencyKey: what === 'withdraw' ? undefined : newIdempotencyKey(),
      });
      const t = what === 'accept'
        ? twin(`Accepted, ${address}. It's on your list.`, `मंज़ूर कर दिया, ${addressHi}। आपकी लिस्ट में आ गया।`)
        : what === 'decline' ? twin(`Declined, ${address}. I've informed them.`, `मना कर दिया, ${addressHi}। उन्हें बता दिया है।`) : null;
      if (t) say(t.text, { interrupt: true, lang: t.lang, fallback: t.fallback });
      setDeclining(false);
      dataChanged.emit();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not reach the server.');
      dataChanged.emit();
    } finally {
      setBusy('');
    }
  };

  return (
    <Card style={s.card}>
      <View style={s.top}>
        <Txt variant="label" style={{ color: colors.inkSoft }}>
          {request.kind === 'CREATE' ? `From ${request.requested_by}` : `Change by ${request.requested_by}`}
        </Txt>
        {mode === 'pa' && tag ? <Tag label={tag.label} color={tag.color} bg={tag.bg} /> : <PriorityTag priority={request.priority} />}
      </View>
      <Txt variant="title" style={{ fontSize: 18, marginTop: 6 }}>{request.title}</Txt>
      {request.kind === 'CHANGE' && request.target_title ? (
        <Txt variant="small" style={{ marginTop: 2 }}>For: {request.target_title}</Txt>
      ) : null}
      {request.due_label ? <Txt variant="small" style={{ marginTop: 4 }}>Due {request.due_label}</Txt> : null}
      {request.reason ? <Txt variant="small" style={{ marginTop: 4, fontStyle: 'italic' }}>“{request.reason}”</Txt> : null}
      {request.decision_note ? <Txt variant="small" style={{ marginTop: 4 }}>MD note: {request.decision_note}</Txt> : null}
      {error ? <Txt variant="small" style={{ color: colors.danger, marginTop: 6 }}>{error}</Txt> : null}
      {mode === 'md' && request.state === 'PENDING' && !declining ? (
        <View style={s.actions}>
          <Button title="Decline" kind="danger" compact onPress={() => setDeclining(true)} style={{ flex: 1, marginRight: 8 }} />
          <Button title="Accept" icon="checkmark" compact onPress={() => act('accept')} loading={busy === 'accept'} style={{ flex: 1.4 }} />
        </View>
      ) : null}
      {mode === 'md' && request.state === 'PENDING' && declining ? (
        <View style={{ marginTop: 12 }}>
          <TextInput
            value={note}
            onChangeText={setNote}
            placeholder={`A word for ${request.requested_by} (optional)`}
            placeholderTextColor={colors.faint}
            style={s.input}
            autoFocus
          />
          <View style={s.actions}>
            <Button title="Keep it" kind="ghost" compact onPress={() => setDeclining(false)} style={{ flex: 1, marginRight: 8 }} />
            <Button title="Decline" kind="danger" compact onPress={() => act('decline')} loading={busy === 'decline'} style={{ flex: 1.4 }} />
          </View>
        </View>
      ) : null}
      {mode === 'pa' && request.state === 'PENDING' ? (
        <View style={s.actions}>
          <Button title="Withdraw" kind="ghost" compact onPress={() => act('withdraw')} loading={busy === 'withdraw'} />
        </View>
      ) : null}
    </Card>
  );
}

const s = StyleSheet.create({
  card: { marginBottom: 10, borderLeftWidth: 3, borderLeftColor: colors.gold },
  top: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' },
  actions: { flexDirection: 'row', marginTop: 14, justifyContent: 'flex-end' },
  input: { backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, borderRadius: radius.md, paddingHorizontal: 14, paddingVertical: 10, fontSize: 15, color: colors.text },
});
