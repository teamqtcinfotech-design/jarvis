import React, { useState } from 'react';
import { StyleSheet, View } from 'react-native';

import { ApiError, api, newIdempotencyKey } from '@/lib/api';
import { dataChanged } from '@/lib/bus';
import { addressOf } from '@/lib/names';
import { useSession } from '@/lib/session';
import { say } from '@/lib/speech';
import { colors } from '@/lib/theme';
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
  const [busy, setBusy] = useState<'' | 'accept' | 'decline' | 'withdraw'>('');
  const [error, setError] = useState('');
  const tag = STATE_TAG[request.state];

  const act = async (what: 'accept' | 'decline' | 'withdraw') => {
    setBusy(what);
    setError('');
    try {
      await api(`/requests/${request.id}/${what}/`, {
        method: 'POST',
        body: {},
        idempotencyKey: what === 'withdraw' ? undefined : newIdempotencyKey(),
      });
      if (what === 'accept') say(`Accepted, ${address}. It's on your list.`, { interrupt: true });
      if (what === 'decline') say(`Declined, ${address}. I've informed them.`, { interrupt: true });
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
      {mode === 'md' && request.state === 'PENDING' ? (
        <View style={s.actions}>
          <Button title="Decline" kind="danger" compact onPress={() => act('decline')} loading={busy === 'decline'} style={{ flex: 1, marginRight: 8 }} />
          <Button title="Accept" icon="checkmark" compact onPress={() => act('accept')} loading={busy === 'accept'} style={{ flex: 1.4 }} />
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
});
