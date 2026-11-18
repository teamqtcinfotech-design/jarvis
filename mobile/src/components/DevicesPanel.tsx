/**
 * Settings → Devices & lock: the phones this account is signed in on (this one first), sign any of them out, sign
 * out everywhere else, and the fingerprint lock switch. The MD also sees each person's phones on the Team page
 * (PersonDevices) and can sign them out.
 */
import React, { useCallback, useEffect, useState } from 'react';
import { StyleSheet, Switch, View } from 'react-native';

import { api, revokeOtherSessions, revokeSession, type SessionRow, sessions as listSessions } from '@/lib/api';
import { lockAvailable, lockEnabled, setLockEnabled, unlock } from '@/lib/appLock';
import { relativeAgo } from '@/lib/format';
import { colors } from '@/lib/theme';

import { Button, Card, Icon, Tag, Txt } from './ui';

function when(iso: string): string {
  const t = Date.parse(iso);
  return Number.isFinite(t) ? relativeAgo(t) : '';
}

function SessionRowView({ s, onSignOut, busy }: { s: SessionRow; onSignOut?: () => void; busy: boolean }) {
  return (
    <View style={st.row}>
      <Icon name={s.how === 'phone' ? 'phone-portrait-outline' : 'key-outline'} size={20} color={s.active ? colors.accent : colors.faint} />
      <View style={{ flex: 1, marginLeft: 12 }}>
        <Txt variant="heading" style={[{ fontSize: 15 }, !s.active && { color: colors.muted }]}>
          {s.name}{s.this_phone ? ' · this phone' : ''}
        </Txt>
        <Txt variant="caption" numberOfLines={2}>
          {s.active
            ? `Signed in ${when(s.signed_in_at)} · last used ${when(s.last_seen_at)}`
            : `Signed out ${when(s.revoked_at ?? s.last_seen_at)}${s.revoked_why === 'md' ? ' by the MD' : s.revoked_why === 'replaced' ? ' (signed in again)' : ''}`}
        </Txt>
      </View>
      {s.this_phone ? <Tag label="You" color={colors.success} bg={colors.successSoft} />
        : s.active && onSignOut ? <Button title="Sign out" kind="danger" compact loading={busy} onPress={onSignOut} />
        : null}
    </View>
  );
}

export function DevicesPanel() {
  const [rows, setRows] = useState<SessionRow[] | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [lockOn, setLockOn] = useState<boolean | null>(null);
  const [canLock, setCanLock] = useState(false);
  const [opened, setOpened] = useState(false);

  const load = useCallback(async () => {
    try {
      setRows(await listSessions());
      setError('');
    } catch {
      setError('Could not load.');
    }
  }, []);

  // This page is behind the fingerprint too (when the lock is on).
  useEffect(() => {
    let alive = true;
    (async () => {
      const ok = await unlock('Devices');
      if (!alive) return;
      setOpened(ok);
      setCanLock(await lockAvailable());
      setLockOn(await lockEnabled());
      if (ok) await load();
    })();
    return () => {
      alive = false;
    };
  }, [load]);

  const signOutOne = async (s: SessionRow) => {
    setBusy(s.id);
    try {
      await revokeSession(s.id);
      await load();
    } catch {
      setError('Could not sign that phone out.');
    } finally {
      setBusy('');
    }
  };
  const signOutOthers = async () => {
    setBusy('all');
    try {
      await revokeOtherSessions();
      await load();
    } catch {
      setError('Could not sign the other phones out.');
    } finally {
      setBusy('');
    }
  };
  const toggleLock = async (on: boolean) => {
    if (!on) {
      const ok = await unlock('Switch the lock off');
      if (!ok) return;
    }
    await setLockEnabled(on);
    setLockOn(on);
  };

  if (!opened) return <Txt variant="small" style={{ textAlign: 'center', marginTop: 30 }}>Unlock to see your devices.</Txt>;
  const active = (rows ?? []).filter((r) => r.active);
  const others = active.filter((r) => !r.this_phone);
  return (
    <>
      <Card>
        <View style={st.switchRow}>
          <View style={{ flex: 1, paddingRight: 12 }}>
            <Txt variant="heading" style={{ fontSize: 15 }}>Lock JARVIS with fingerprint</Txt>
            <Txt variant="small">
              {!canLock ? 'This phone has no fingerprint or face set up.' : lockOn
                ? 'Asked when JARVIS comes back after 5 minutes away, and before the Personal list and this page.'
                : 'Off. Anyone holding the phone can open JARVIS.'}
            </Txt>
          </View>
          <Switch value={!!lockOn && canLock} disabled={!canLock} onValueChange={toggleLock} trackColor={{ true: colors.gold }} />
        </View>
      </Card>
      <Card style={{ marginTop: 12 }}>
        <Txt variant="label" style={{ marginBottom: 6 }}>Signed in on</Txt>
        {rows === null ? <Txt variant="small">{error || 'Loading…'}</Txt> : null}
        {active.map((s) => <SessionRowView key={s.id} s={s} busy={busy === s.id} onSignOut={() => signOutOne(s)} />)}
        {rows && !active.length ? <Txt variant="small">Only this phone.</Txt> : null}
        {others.length ? (
          <Button title={`Sign out the other ${others.length === 1 ? 'phone' : `${others.length} phones`}`} kind="danger" compact icon="log-out-outline"
            loading={busy === 'all'} onPress={signOutOthers} style={{ alignSelf: 'flex-start', marginTop: 10 }} />
        ) : null}
        {error ? <Txt variant="small" style={{ color: colors.danger, marginTop: 8 }}>{error}</Txt> : null}
      </Card>
      {rows && rows.some((r) => !r.active) ? (
        <Card style={{ marginTop: 12 }}>
          <Txt variant="label" style={{ marginBottom: 6 }}>Earlier</Txt>
          {rows.filter((r) => !r.active).slice(0, 6).map((s) => <SessionRowView key={s.id} s={s} busy={false} />)}
        </Card>
      ) : null}
      <Txt variant="caption" style={{ marginTop: 12 }}>
        A new sign-in on another phone is announced here and on your other phones, with “Sign it out” one tap away. A phone signed out stops at once: its login, its alerts and its walkie line.
      </Txt>
    </>
  );
}

/** The MD, on the Team page: where this person is signed in, with Sign out for each phone. */
export function PersonDevices({ userId }: { userId: number }) {
  const [rows, setRows] = useState<SessionRow[] | null>(null);
  const [busy, setBusy] = useState('');
  const [note, setNote] = useState('');
  const load = useCallback(async () => {
    try {
      const r = await api<{ results: SessionRow[] }>(`/team/${userId}/sessions/`);
      setRows(r.data.results);
    } catch {
      setRows([]);
    }
  }, [userId]);
  useEffect(() => {
    const kick = setTimeout(() => load().catch(() => undefined), 0);
    return () => clearTimeout(kick);
  }, [load]);
  const signOut = async (s: SessionRow) => {
    setBusy(s.id);
    setNote('');
    try {
      await revokeSession(s.id);
      await load();
      setNote(`${s.name} signed out; they have been told.`);
    } catch {
      setNote('Could not sign that phone out.');
    } finally {
      setBusy('');
    }
  };
  const active = (rows ?? []).filter((r) => r.active);
  return (
    <View style={{ marginTop: 12 }}>
      <Txt variant="label" style={{ marginBottom: 4 }}>Signed in on</Txt>
      {rows === null ? <Txt variant="caption">Loading…</Txt> : null}
      {rows && !active.length ? <Txt variant="caption">Not signed in anywhere right now.</Txt> : null}
      {active.map((s) => <SessionRowView key={s.id} s={{ ...s, this_phone: false }} busy={busy === s.id} onSignOut={() => signOut(s)} />)}
      {note ? <Txt variant="caption" style={{ marginTop: 4 }}>{note}</Txt> : null}
    </View>
  );
}

const st = StyleSheet.create({
  row: { flexDirection: 'row', alignItems: 'center', paddingVertical: 8 },
  switchRow: { flexDirection: 'row', alignItems: 'center', paddingVertical: 6 },
});
