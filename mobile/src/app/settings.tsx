import { useRouter } from 'expo-router';
import React, { useEffect, useState } from 'react';
import { ScrollView, StyleSheet, Switch, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { Button, Card, Chip, IconButton, Tag, Txt } from '@/components/ui';
import { useAlertEngine } from '@/lib/alertEngine';
import { api } from '@/lib/api';
import { relativeAgo } from '@/lib/format';
import { usePrefs } from '@/lib/prefs';
import { presenceLabel } from '@/lib/presence';
import { useSession } from '@/lib/session';
import { say } from '@/lib/speech';
import { colors } from '@/lib/theme';

function Row({ title, detail, right }: { title: string; detail?: string; right: React.ReactNode }) {
  return (
    <View style={s.row}>
      <View style={{ flex: 1, paddingRight: 12 }}>
        <Txt variant="heading" style={{ fontSize: 15 }}>{title}</Txt>
        {detail ? <Txt variant="small">{detail}</Txt> : null}
      </View>
      {right}
    </View>
  );
}

interface Health {
  taskpanel: { label: string; mode: string; linked: boolean; unconfirmed_commands: number; last_sync: string | null; last_sync_error: string };
}

export default function Settings() {
  const router = useRouter();
  const { user, server, signOut, biometricAvailable } = useSession();
  const { prefs, update } = usePrefs();
  const engine = useAlertEngine();
  const p = engine.presence;
  const isMD = user?.role === 'MD';
  const [health, setHealth] = useState<Health | null>(null);

  useEffect(() => {
    const kick = setTimeout(() => {
      api<Health>('/health/').then((r) => setHealth(r.data)).catch(() => setHealth(null));
    }, 0);
    return () => clearTimeout(kick);
  }, []);
  const tp = health?.taskpanel;

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }}>
      <View style={s.top}>
        <IconButton icon="chevron-back" label="Back" onPress={() => router.back()} />
        <Txt variant="title" style={{ flex: 1, textAlign: 'center', marginRight: 40 }}>Settings</Txt>
      </View>
      <ScrollView contentContainerStyle={{ padding: 20, paddingBottom: 60 }}>
        <Txt variant="label">Voice</Txt>
        <Card style={s.card}>
          <Row
            title="Listening language"
            detail="Hinglish works with either; Hindi returns Devanagari text."
            right={
              <View style={{ flexDirection: 'row' }}>
                <Chip label="English (India)" selected={prefs.voiceLang === 'en-IN'} onPress={() => update({ voiceLang: 'en-IN' })} />
                <Chip label="हिन्दी" selected={prefs.voiceLang === 'hi-IN'} onPress={() => update({ voiceLang: 'hi-IN' })} />
              </View>
            }
          />
          {isMD ? (
            <Row
              title="Speak private details aloud"
              detail="When off, JARVIS only says “you have a private reminder”."
              right={<Switch value={prefs.speakPrivateDetails} onValueChange={(v) => update({ speakPrivateDetails: v })} trackColor={{ true: colors.gold }} />}
            />
          ) : null}
          <Button title="Test voice" kind="secondary" icon="volume-high-outline" compact onPress={() => say(isMD ? 'Good day, Sir. JARVIS is ready.' : `Hello ${user?.name}. JARVIS is ready.`, { interrupt: true })} style={{ alignSelf: 'flex-start', marginTop: 10 }} />
        </Card>

        <Txt variant="label" style={{ marginTop: 22 }}>Smart alerts</Txt>
        <Card style={s.card}>
          <Row
            title="Treat me as away after"
            detail="If the phone hasn't moved for this long, alerts are held until you pick it up."
            right={null}
          />
          <View style={{ flexDirection: 'row', flexWrap: 'wrap', marginTop: 4 }}>
            {[1, 5, 10, 15, 30].map((m) => (
              <Chip key={m} label={`${m} min`} selected={prefs.awayAfterMinutes === m} onPress={() => update({ awayAfterMinutes: m })} />
            ))}
          </View>
          <Row
            title="Quiet hours"
            detail={`${prefs.quietStartHour}:00 – ${prefs.quietEndHour}:00 · only urgent alerts, silently`}
            right={<Switch value={prefs.quietHoursEnabled} onValueChange={(v) => update({ quietHoursEnabled: v })} trackColor={{ true: colors.gold }} />}
          />
          <View style={s.live}>
            <Txt variant="label" style={{ color: colors.inkSoft }}>Live status</Txt>
            <Txt variant="body" style={{ marginTop: 4 }}>
              {presenceLabel[p.state].title} · phone {p.faceDown ? 'face down' : 'face up'} · still for {p.idleSeconds}s
            </Txt>
            <Txt variant="caption">Sensor z = {p.zValue.toFixed(2)} {p.sensorAvailable ? '' : '(sensor unavailable)'}</Txt>
            <Txt variant="caption" style={{ marginTop: 4 }}>
              {engine.upcomingCount} reminder{engine.upcomingCount === 1 ? '' : 's'} scheduled on this phone · synced {relativeAgo(engine.lastSync)}
            </Txt>
            {engine.notificationsGranted === false ? (
              <Tag label="Notifications are blocked — allow them in Android settings" color={colors.danger} bg={colors.dangerSoft} />
            ) : null}
          </View>
        </Card>

        <Txt variant="label" style={{ marginTop: 22 }}>TaskPanel connection</Txt>
        <Card style={s.card}>
          <Row
            title={tp ? tp.label : 'Checking…'}
            detail={
              tp
                ? `${tp.linked ? 'This account is linked' : 'This account is NOT linked yet'} · last sync ${tp.last_sync ? relativeAgo(Date.parse(tp.last_sync)) : 'not yet'}`
                : undefined
            }
            right={tp ? <Tag label={tp.linked ? 'Linked' : 'Not linked'} color={tp.linked ? colors.success : colors.danger} bg={tp.linked ? colors.successSoft : colors.dangerSoft} /> : null}
          />
          {tp?.unconfirmed_commands ? (
            <Txt variant="small" style={{ color: colors.warn }}>{tp.unconfirmed_commands} task(s) waiting for TaskPanel to confirm.</Txt>
          ) : null}
          {tp?.last_sync_error ? <Txt variant="small" style={{ color: colors.danger }}>Last sync problem: {tp.last_sync_error}</Txt> : null}
          <Txt variant="caption" style={{ marginTop: 6 }}>
            Staff tasks are saved in TaskPanel. JARVIS says “Assigned” only after TaskPanel confirms.
          </Txt>
        </Card>

        <Txt variant="label" style={{ marginTop: 22 }}>Account & security</Txt>
        <Card style={s.card}>
          <Row title={isMD ? 'Managing Director' : `PA · ${user?.name}`} detail={`Signed in as ${user?.username}`} right={null} />
          <Row title="Server" detail={server} right={null} />
          <Row title="Biometric lock" detail={biometricAvailable ? 'On — fingerprint/face required after 1 minute away' : 'No fingerprint or face enrolled on this phone'} right={null} />
          {!isMD ? <Txt variant="small" style={{ marginTop: 8 }}>MD’s personal tasks are private and never shown in this app.</Txt> : null}
          <Button title="Sign out" kind="danger" icon="log-out-outline" onPress={signOut} style={{ marginTop: 14 }} />
        </Card>
        <Txt variant="caption" style={{ textAlign: 'center', marginTop: 20 }}>JARVIS 1.0 · private build for QTC INFOTECH</Txt>
      </ScrollView>
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  top: { flexDirection: 'row', alignItems: 'center', paddingHorizontal: 12, paddingTop: 6 },
  card: { marginTop: 8 },
  row: { flexDirection: 'row', alignItems: 'center', paddingVertical: 8 },
  live: { marginTop: 12, padding: 12, borderRadius: 10, backgroundColor: colors.surfaceAlt },
});
