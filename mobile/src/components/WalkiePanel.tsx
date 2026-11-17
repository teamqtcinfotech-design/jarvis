/**
 * Settings → Walkie-talkie: the switch (the line to the relay stays open while it is on), quiet for a while, who is
 * around right now, and (the MD) who may start a talk.
 */
import React from 'react';
import { StyleSheet, Switch, View } from 'react-native';

import { usePrefs } from '@/lib/prefs';
import { useSession } from '@/lib/session';
import { colors } from '@/lib/theme';
import { useWalkie } from '@/lib/walkie';

import { Button, Card, Chip, Tag, Txt } from './ui';

const quietLabel = (iso: string) => {
  if (!iso || Date.parse(iso) < Date.now()) return '';
  const d = new Date(iso);
  const h = d.getHours() % 12 || 12;
  return `${h}:${String(d.getMinutes()).padStart(2, '0')} ${d.getHours() < 12 ? 'AM' : 'PM'}`;
};

export function WalkiePanel() {
  const { user } = useSession();
  const { prefs, update } = usePrefs();
  const w = useWalkie();
  const isMD = user?.role === 'MD';
  const quietTill = quietLabel(prefs.walkieQuietUntil);
  const setQuiet = (minutes: number) => {
    w.quiet(minutes);
    update({ walkieQuietUntil: minutes ? new Date(Date.now() + minutes * 60_000).toISOString() : '' });
  };
  return (
    <>
      <Card>
        <View style={s.row}>
          <View style={{ flex: 1, paddingRight: 12 }}>
            <Txt variant="heading" style={{ fontSize: 15 }}>Walkie-talkie</Txt>
            <Txt variant="small">
              {!w.supported ? 'Needs the 2.6.0 app on this phone.' : prefs.walkieEnabled
                ? (w.connected ? 'On · the line is open. Hold the button on the home screen to talk.' : 'On · connecting…')
                : 'Off. Switch on to talk live with the team, even on a locked phone.'}
            </Txt>
          </View>
          <Switch value={prefs.walkieEnabled} disabled={!w.supported} onValueChange={(v) => update({ walkieEnabled: v })} trackColor={{ true: colors.gold }} />
        </View>
        {prefs.walkieEnabled ? (
          <>
            <View style={[s.row, { marginTop: 8 }]}>
              <View style={{ flex: 1 }}>
                <Txt variant="heading" style={{ fontSize: 15 }}>Quiet</Txt>
                <Txt variant="small">{quietTill ? `Quiet till ${quietTill}: nobody can talk to you; they're told you were quiet.` : 'Not quiet. Say “walkie quiet for 30 minutes” too.'}</Txt>
              </View>
            </View>
            <View style={s.chips}>
              <Chip label="30 min" selected={false} onPress={() => setQuiet(30)} />
              <Chip label="1 hour" selected={false} onPress={() => setQuiet(60)} />
              <Chip label="2 hours" selected={false} onPress={() => setQuiet(120)} />
              {quietTill ? <Chip label="Quiet off" icon="volume-high-outline" selected onPress={() => setQuiet(0)} /> : null}
            </View>
          </>
        ) : null}
      </Card>
      {isMD ? (
        <Card style={{ marginTop: 12 }}>
          <View style={s.row}>
            <View style={{ flex: 1, paddingRight: 12 }}>
              <Txt variant="heading" style={{ fontSize: 15 }}>Anyone may start a talk</Txt>
              <Txt variant="small">Off: only you can start one; the others can reply.</Txt>
            </View>
            <Switch value={prefs.walkieAnyoneStarts} onValueChange={(v) => update({ walkieAnyoneStarts: v })} trackColor={{ true: colors.gold }} />
          </View>
        </Card>
      ) : null}
      {prefs.walkieEnabled && w.supported ? (
        <Card style={{ marginTop: 12 }}>
          <Txt variant="label" style={{ marginBottom: 6 }}>Who is around</Txt>
          <View style={s.chips}>
            {w.people.map((p) => (
              <Tag
                key={p.id}
                label={`${p.name} · ${p.status === 'available' ? 'available' : p.status === 'quiet' ? 'quiet' : p.status === 'off' ? 'off' : "can't reach"}`}
                color={p.status === 'available' ? colors.success : p.status === 'quiet' ? colors.warn : p.status === 'off' ? colors.muted : colors.danger}
                bg={p.status === 'available' ? colors.successSoft : p.status === 'quiet' ? colors.warnSoft : p.status === 'off' ? colors.surfaceAlt : colors.dangerSoft}
              />
            ))}
            {!w.people.length ? <Txt variant="caption">{w.connected ? 'Nobody else yet.' : 'Connecting…'}</Txt> : null}
          </View>
          <Button title="Refresh" kind="secondary" compact icon="refresh-outline" onPress={w.refresh} style={{ alignSelf: 'flex-start', marginTop: 10 }} />
        </Card>
      ) : null}
    </>
  );
}

const s = StyleSheet.create({
  row: { flexDirection: 'row', alignItems: 'center', paddingVertical: 6 },
  chips: { flexDirection: 'row', flexWrap: 'wrap', gap: 6, marginTop: 8 },
});
