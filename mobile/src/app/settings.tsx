import { useRouter } from 'expo-router';
import React, { useEffect, useState } from 'react';
import { Pressable, ScrollView, StyleSheet, Switch, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { Button, Card, Chip, Icon, IconButton, Tag, Txt } from '@/components/ui';
import { useAlertEngine } from '@/lib/alertEngine';
import { api } from '@/lib/api';
import { relativeAgo } from '@/lib/format';
import { usePrefs } from '@/lib/prefs';
import { presenceLabel } from '@/lib/presence';
import { useSession } from '@/lib/session';
import { say } from '@/lib/speech';
import { colors } from '@/lib/theme';
import type { AiUsageSummary, MemoryItem } from '@/lib/types';

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
  const [memories, setMemories] = useState<MemoryItem[] | null>(null);
  const [usage, setUsage] = useState<AiUsageSummary | null>(null);

  useEffect(() => {
    const kick = setTimeout(() => {
      api<Health>('/health/').then((r) => setHealth(r.data)).catch(() => setHealth(null));
      api<{ results: MemoryItem[] }>('/memories/').then((r) => setMemories(r.data.results)).catch(() => setMemories([]));
      api<AiUsageSummary>('/assistant/usage/').then((r) => setUsage(r.data)).catch(() => setUsage(null));
    }, 0);
    return () => clearTimeout(kick);
  }, []);
  const tp = health?.taskpanel;

  const forget = async (id: string) => {
    setMemories((list) => (list ?? []).filter((m) => m.id !== id));
    await api(`/memories/${id}/`, { method: 'DELETE' }).catch(() => undefined);
  };

  const testVoice = () => {
    say(isMD ? 'नमस्ते सर, मैं जार्विस हूँ। बताइए, मैं क्या करूँ?' : `नमस्ते, मैं जार्विस हूँ।`, { interrupt: true, lang: 'hi' });
    say(isMD ? 'Good day, Sir. JARVIS is ready.' : `Hello ${user?.name}. JARVIS is ready.`);
  };

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }}>
      <View style={s.top}>
        <IconButton icon="chevron-back" label="Back" onPress={() => router.back()} />
        <Txt variant="title" style={{ flex: 1, textAlign: 'center', marginRight: 40 }}>Settings</Txt>
      </View>
      <ScrollView contentContainerStyle={{ padding: 20, paddingBottom: 60 }}>
        <Txt variant="label">Voice</Txt>
        <Card style={s.card}>
          <Row title="I speak in" detail="English (India) also understands Hinglish." right={null} />
          <View style={s.chips}>
            <Chip label="English / Hinglish" selected={prefs.voiceLang === 'en-IN'} onPress={() => update({ voiceLang: 'en-IN' })} />
            <Chip label="हिन्दी" selected={prefs.voiceLang === 'hi-IN'} onPress={() => update({ voiceLang: 'hi-IN' })} />
          </View>
          <Row title="JARVIS voice" detail="Hindi replies always use the Hindi voice." right={null} />
          <View style={s.chips}>
            <Chip label="Hindi accent" selected={prefs.voiceAccent === 'hi-IN'} onPress={() => update({ voiceAccent: 'hi-IN' })} />
            <Chip label="Indian English" selected={prefs.voiceAccent === 'en-IN'} onPress={() => update({ voiceAccent: 'en-IN' })} />
          </View>
          {isMD ? (
            <>
              <Row
                title="Say private reminders aloud"
                detail="While JARVIS is open on screen."
                right={<Switch value={prefs.speakPrivateDetails} onValueChange={(v) => update({ speakPrivateDetails: v })} trackColor={{ true: colors.gold }} />}
              />
              <Row
                title="…even when the phone is locked"
                detail="Off: JARVIS only says “you have a private reminder”."
                right={<Switch value={prefs.privateAloudWhenLocked} disabled={!prefs.speakPrivateDetails} onValueChange={(v) => update({ privateAloudWhenLocked: v })} trackColor={{ true: colors.gold }} />}
              />
            </>
          ) : null}
          <Button title="Test voice" kind="secondary" icon="volume-high-outline" compact onPress={testVoice} style={{ alignSelf: 'flex-start', marginTop: 10 }} />
          <Txt variant="caption" style={{ marginTop: 8 }}>
            No Hindi voice? Android Settings → Text-to-speech → Google → Install voice data → Hindi (India).
          </Txt>
        </Card>

        <Txt variant="label" style={{ marginTop: 22 }}>What JARVIS remembers about you</Txt>
        <Card style={s.card}>
          {memories === null ? <Txt variant="small">Loading…</Txt> : null}
          {memories?.length === 0 ? (
            <Txt variant="small">Nothing yet. Say “yaad rakhna ki…” or “remember that…” and JARVIS will keep it here.</Txt>
          ) : null}
          {memories?.map((m) => (
            <View key={m.id} style={s.memory}>
              <Icon name="bookmark-outline" size={16} color={colors.gold} />
              <Txt variant="body" style={{ flex: 1, marginLeft: 8 }}>{m.text}</Txt>
              <Pressable onPress={() => forget(m.id)} hitSlop={10} accessibilityLabel="Forget this">
                <Icon name="trash-outline" size={18} color={colors.muted} />
              </Pressable>
            </View>
          ))}
          <Txt variant="caption" style={{ marginTop: 8 }}>Only you can see these notes.</Txt>
        </Card>

        <Txt variant="label" style={{ marginTop: 22 }}>Smart alerts</Txt>
        <Card style={s.card}>
          <Row title="Treat me as away after" detail="If the phone hasn't moved for this long, alerts wait until you pick it up." right={null} />
          <View style={s.chips}>
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
            <Txt variant="small" style={{ color: colors.inkSoft }}>
              Right now: {presenceLabel[p.state].title} · phone {p.faceDown ? 'face down' : 'face up'}
            </Txt>
            <Txt variant="caption" style={{ marginTop: 4 }}>
              {engine.upcomingCount} reminder{engine.upcomingCount === 1 ? '' : 's'} set on this phone · updated {relativeAgo(engine.lastSync)}
            </Txt>
            {engine.notificationsGranted === false ? (
              <Tag label="Notifications are blocked — allow them in Android settings" color={colors.danger} bg={colors.dangerSoft} />
            ) : null}
          </View>
        </Card>

        <Txt variant="label" style={{ marginTop: 22 }}>Staff task app (TaskPanel)</Txt>
        <Card style={s.card}>
          <Row
            title={tp ? tp.label : 'Checking…'}
            detail={tp ? `${tp.linked ? 'Your account is connected' : 'Your account is NOT connected yet'} · last update ${tp.last_sync ? relativeAgo(Date.parse(tp.last_sync)) : 'not yet'}` : undefined}
            right={tp ? <Tag label={tp.linked ? 'Connected' : 'Not connected'} color={tp.linked ? colors.success : colors.danger} bg={tp.linked ? colors.successSoft : colors.dangerSoft} /> : null}
          />
          {tp?.unconfirmed_commands ? (
            <Txt variant="small" style={{ color: colors.warn }}>{tp.unconfirmed_commands} task(s) still being sent.</Txt>
          ) : null}
          {tp?.last_sync_error ? <Txt variant="small" style={{ color: colors.danger }}>Last problem: {tp.last_sync_error}</Txt> : null}
          <Txt variant="caption" style={{ marginTop: 6 }}>JARVIS says “given” only after the staff app confirms.</Txt>
        </Card>

        {usage ? (
          <>
            <Txt variant="label" style={{ marginTop: 22 }}>AI use · {usage.month}</Txt>
            <Card style={s.card}>
              <Txt variant="body">{usage.requests} requests · {usage.web_searches} internet searches</Txt>
              <Txt variant="small" style={{ marginTop: 4 }}>
                Internet questions today: {usage.questions_today} of {usage.daily_question_limit}
              </Txt>
            </Card>
          </>
        ) : null}

        <Txt variant="label" style={{ marginTop: 22 }}>Account & security</Txt>
        <Card style={s.card}>
          <Row title={isMD ? 'Managing Director' : `PA · ${user?.name}`} detail={`Signed in as ${user?.username}`} right={null} />
          <Row title="Server" detail={server} right={null} />
          <Row title="Fingerprint lock" detail={biometricAvailable ? 'On — needed after 1 minute away' : 'No fingerprint or face set up on this phone'} right={null} />
          {!isMD ? <Txt variant="small" style={{ marginTop: 8 }}>Sir’s personal tasks are private and never shown in this app.</Txt> : null}
          <Button title="Sign out" kind="danger" icon="log-out-outline" onPress={signOut} style={{ marginTop: 14 }} />
        </Card>
        <Txt variant="caption" style={{ textAlign: 'center', marginTop: 20 }}>JARVIS 1.1 · private build for QTC INFOTECH</Txt>
      </ScrollView>
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  top: { flexDirection: 'row', alignItems: 'center', paddingHorizontal: 12, paddingTop: 6 },
  card: { marginTop: 8 },
  row: { flexDirection: 'row', alignItems: 'center', paddingVertical: 8 },
  chips: { flexDirection: 'row', flexWrap: 'wrap', marginTop: 2 },
  memory: { flexDirection: 'row', alignItems: 'center', paddingVertical: 8, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: colors.line },
  live: { marginTop: 12, padding: 12, borderRadius: 10, backgroundColor: colors.surfaceAlt },
});
