import { Redirect, useRouter } from 'expo-router';
import { ExpoSpeechRecognitionModule } from 'expo-speech-recognition';
import React, { useEffect, useState } from 'react';
import { AppState, Pressable, ScrollView, StyleSheet, Switch, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { Button, Card, Chip, Icon, IconButton, Tag, Txt } from '@/components/ui';
import { useAlertEngine } from '@/lib/alertEngine';
import { api, currentServer, deviceTokens } from '@/lib/api';
import { relativeAgo, timeLabel } from '@/lib/format';
import { addressHiOf, addressOf, mdWord } from '@/lib/names';
import { usePrefs } from '@/lib/prefs';
import { presenceLabel } from '@/lib/presence';
import { useSession } from '@/lib/session';
import { say } from '@/lib/speech';
import { colors } from '@/lib/theme';
import type { AiUsageSummary, ContactItem, MemoryItem } from '@/lib/types';
import { useDashboard } from '@/lib/useDashboard';
import { type WakeStatus, wakeSetListening, wakeSettings, wakeStart, wakeStatus, wakeSupported } from '@/lib/wake';

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

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const dayNames = (days: number[]) => (days.length ? days.map((d) => WEEKDAYS[d] ?? '').filter(Boolean).join(', ') : 'none');

/** Sign out (the button below, or an expired login) removes the user, and with it the alert engine this screen
 *  reads. Going to the login page first is what keeps the release build from crashing at that moment. */
export default function Settings() {
  const { user } = useSession();
  if (!user) return <Redirect href="/login" />;
  return <SettingsScreen />;
}

function SettingsScreen() {
  const router = useRouter();
  const { user, server, signOut, biometricAvailable } = useSession();
  const { prefs, update } = usePrefs();
  const engine = useAlertEngine();
  const { data: dash } = useDashboard();
  const p = engine.presence;
  const isMD = user?.role === 'MD';
  const [health, setHealth] = useState<Health | null>(null);
  const [memories, setMemories] = useState<MemoryItem[] | null>(null);
  const [contacts, setContacts] = useState<ContactItem[] | null>(null);
  const [usage, setUsage] = useState<AiUsageSummary | null>(null);
  const [wake, setWake] = useState<WakeStatus | null>(null);
  const [wakeBusy, setWakeBusy] = useState(false);
  const [wakeNote, setWakeNote] = useState('');

  useEffect(() => {
    const refresh = () => wakeStatus().then(setWake).catch(() => undefined);
    const kick = setTimeout(() => {
      api<Health>('/health/').then((r) => setHealth(r.data)).catch(() => setHealth(null));
      api<{ results: MemoryItem[] }>('/memories/').then((r) => setMemories(r.data.results)).catch(() => setMemories([]));
      api<{ results: ContactItem[] }>('/contacts/').then((r) => setContacts(r.data.results)).catch(() => setContacts([]));
      api<AiUsageSummary>('/assistant/usage/').then((r) => setUsage(r.data)).catch(() => setUsage(null));
      refresh();
    }, 0);
    const sub = AppState.addEventListener('change', (s) => s === 'active' && refresh()); // back from Android settings
    return () => {
      clearTimeout(kick);
      sub.remove();
    };
  }, []);
  const tp = health?.taskpanel;
  const xiaomi = /xiaomi|redmi|poco/i.test(wake?.manufacturer ?? '');

  const toggleWake = async (on: boolean) => {
    setWakeNote('');
    if (!on) {
      // Only the microphone part goes off; reminders and messages keep arriving in the background.
      await wakeSetListening(false);
      update({ wakeWordEnabled: false });
      setWake(await wakeStatus());
      return;
    }
    setWakeBusy(true);
    try {
      const mic = await ExpoSpeechRecognitionModule.requestPermissionsAsync();
      if (!mic.granted) {
        setWakeNote('Microphone permission is needed.');
        return;
      }
      const st = await wakeStatus();
      if (!st.supported) {
        setWakeNote('This build has no background listener.');
        return;
      }
      if (!st.overlayAllowed) {
        setWakeNote('Allow “Display over other apps” for JARVIS, then come back and switch this on.');
        wakeSettings.overlay();
        return;
      }
      const tokens = await deviceTokens();
      const ok = await wakeStart({
        serverUrl: currentServer(),
        accessToken: tokens.access,
        refreshToken: tokens.refresh,
        address: addressOf(user),
        addressHi: addressHiOf(user),
        isMd: isMD,
        paName: dash?.pa_name ?? '',
        voiceAccent: prefs.voiceAccent,
        listenLang: prefs.listenLang === 'hi-IN' ? 'hi-IN' : 'en-IN',
        replyLang: prefs.replyLanguage === 'auto' ? user?.language : prefs.replyLanguage,
        speakWhenLocked: prefs.privateAloudWhenLocked,
        wakeWord: true,
      });
      update({ wakeWordEnabled: ok });
      if (!ok) setWakeNote('Could not start the listener. Check the microphone permission and try again.');
      setWake(await wakeStatus());
    } catch {
      setWakeNote('Could not reach the server to set this up. Try again in a moment.');
    } finally {
      setWakeBusy(false);
    }
  };

  const forget = async (id: string) => {
    setMemories((list) => (list ?? []).filter((m) => m.id !== id));
    await api(`/memories/${id}/`, { method: 'DELETE' }).catch(() => undefined);
  };

  const forgetContact = async (id: number) => {
    setContacts((list) => (list ?? []).filter((c) => c.id !== id));
    await api(`/contacts/${id}/`, { method: 'DELETE' }).catch(() => undefined);
  };

  const testVoice = () => {
    const address = addressOf(user);
    say(isMD ? `नमस्ते ${addressHiOf(user)}, मैं जार्विस हूँ। बताइए, मैं क्या करूँ?` : `नमस्ते, मैं जार्विस हूँ।`, {
      interrupt: true, lang: 'hi', fallback: isMD ? `Namaste ${address}, main JARVIS hoon. Bataiye, main kya karoon?` : 'Namaste, main JARVIS hoon.',
    });
    say(isMD ? `Good day, ${address}. JARVIS is ready.` : `Hello ${address}. JARVIS is ready.`);
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
          <Row title="JARVIS replies in" detail="Match me: the language you just used, Hindi or English, for everything JARVIS says — replies, reminders and messages. Or always one language." right={null} />
          <View style={s.chips}>
            <Chip label="Match me" selected={prefs.replyLanguage === 'auto'} onPress={() => update({ replyLanguage: 'auto' })} />
            <Chip label="Always Hindi" selected={prefs.replyLanguage === 'hi'} onPress={() => update({ replyLanguage: 'hi' })} />
            <Chip label="Always English" selected={prefs.replyLanguage === 'en'} onPress={() => update({ replyLanguage: 'en' })} />
          </View>
          <Row title="Listening" detail="Speak Hindi, English or Hinglish — no need to choose. “Mostly pure Hindi” helps when you rarely use English words." right={null} />
          <View style={s.chips}>
            <Chip label="Automatic (Hindi + English)" selected={prefs.listenLang === 'auto'} onPress={() => update({ listenLang: 'auto' })} />
            <Chip label="Mostly pure Hindi" selected={prefs.listenLang === 'hi-IN'} onPress={() => update({ listenLang: 'hi-IN' })} />
          </View>
          <Row title="JARVIS voice" detail="Hindi lines use the Hindi voice. For English lines, choose the accent." right={null} />
          <View style={s.chips}>
            <Chip label="Indian · Hindi accent" selected={prefs.voiceAccent === 'hi-IN'} onPress={() => update({ voiceAccent: 'hi-IN' })} />
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

        <Txt variant="label" style={{ marginTop: 22 }}>When the app is closed</Txt>
        <Card style={s.card}>
          <Row
            title="Reminders and messages in the background"
            detail={
              !wakeSupported ? 'Not included in this build.'
                : wake?.running ? `On. Anything from ${isMD ? (dash?.pa_name || 'your PA') : mdWord(dash)} reaches this phone within half a minute, spoken when it is unlocked.`
                : 'Starting… (open the app once after a restart).'
            }
            right={wake?.running ? <Tag label="On" color={colors.success} bg={colors.successSoft} /> : null}
          />
          <Row
            title="Listen for “Hey JARVIS”"
            detail={
              !wakeSupported ? 'Not included in this build.'
                : wake?.listening ? 'On. Say “Hey JARVIS” any time, even with the app closed.'
                : 'Off. Switch on to wake JARVIS by voice from anywhere.'
            }
            right={<Switch value={prefs.wakeWordEnabled && !!wake?.listening} disabled={!wakeSupported || wakeBusy} onValueChange={toggleWake} trackColor={{ true: colors.gold }} />}
          />
          {wakeNote ? <Txt variant="small" style={{ color: colors.warn, marginTop: 4 }}>{wakeNote}</Txt> : null}
          {wakeSupported ? (
            <View style={s.chips}>
              <Chip label="Run phone setup again" icon="construct-outline" onPress={() => router.push('/setup')} />
              {!wake?.overlayAllowed ? <Chip label="Allow display over other apps" icon="open-outline" onPress={() => wakeSettings.overlay()} /> : null}
              {!wake?.batteryUnrestricted ? <Chip label="Battery: no restrictions" icon="battery-charging-outline" onPress={() => wakeSettings.battery()} /> : null}
              {xiaomi ? <Chip label="Autostart (Xiaomi)" icon="power-outline" onPress={() => { if (!wakeSettings.autostart()) setWakeNote('Open Security app → Autostart and allow JARVIS.'); }} /> : null}
              {xiaomi ? (
                <Chip
                  label="Pop-ups from background (Xiaomi)"
                  icon="albums-outline"
                  onPress={() => {
                    setWakeNote('On the page that opens, switch on “Display pop-up windows while running in the background”. Without it, Xiaomi stops JARVIS from appearing when you say “Hey JARVIS”.');
                    wakeSettings.miuiPermissions();
                  }}
                />
              ) : null}
            </View>
          ) : null}
          <Txt variant="caption" style={{ marginTop: 6 }}>
            Listening happens on the phone; nothing is sent until you say the words. A small notification shows while it is on.
            After a phone restart, open JARVIS once (or tap the notification) to switch it on again.
            {xiaomi ? ' On Xiaomi, allow all three: Autostart, Battery, and Pop-ups from background.' : ''}
          </Txt>
          {wake && prefs.wakeWordEnabled ? (
            <Txt variant="caption" style={{ marginTop: 6 }}>
              Heard “Hey JARVIS” {wake.wakesToday} {wake.wakesToday === 1 ? 'time' : 'times'} today
              {wake.emptyWakesToday ? `, ${wake.emptyWakesToday} with nothing said after it` : ''}.
            </Txt>
          ) : null}
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

        <Txt variant="label" style={{ marginTop: 22 }}>People JARVIS knows</Txt>
        <Card style={s.card}>
          {contacts === null ? <Txt variant="small">Loading…</Txt> : null}
          {contacts?.length === 0 ? (
            <Txt variant="small">
              Nobody outside the staff list yet. When you name someone new, JARVIS asks who they are once and keeps them here.
            </Txt>
          ) : null}
          {contacts?.map((c) => (
            <View key={c.id} style={s.memory}>
              <Icon name="person-outline" size={16} color={colors.gold} />
              <Txt variant="body" style={{ flex: 1, marginLeft: 8 }}>{c.note ? `${c.name} · ${c.note}` : c.name}</Txt>
              <Pressable onPress={() => forgetContact(c.id)} hitSlop={10} accessibilityLabel={`Remove ${c.name}`}>
                <Icon name="trash-outline" size={18} color={colors.muted} />
              </Pressable>
            </View>
          ))}
          <Txt variant="caption" style={{ marginTop: 8 }}>
            Staff are known already. Only you can see this list; remove a name and JARVIS will ask about it again.
          </Txt>
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

        <Txt variant="label" style={{ marginTop: 22 }}>Work routine & reminders</Txt>
        <Card style={s.card}>
          <Row title="Office hours" detail={`${timeLabel(prefs.officeStart)} – ${timeLabel(prefs.officeEnd)} · free slots are suggested inside these`} right={null} />
          <Row title="Weekly off" detail={dayNames(prefs.weeklyOff)} right={null} />
          <Row
            title="Fixed routine"
            detail={prefs.routineBlocks.length
              ? prefs.routineBlocks.map((b) => `${b.label} ${timeLabel(b.start)}–${timeLabel(b.end)}${b.days.length === 7 ? '' : ` (${dayNames(b.days)})`}`).join(' · ')
              : 'None yet. Blocks here are kept free when JARVIS suggests a time.'}
            right={null}
          />
          <Row
            title="Morning briefing"
            detail={`At ${timeLabel(prefs.briefingTime)}: the day, clashes, pending requests — spoken when you pick up the phone.`}
            right={<Switch value={prefs.briefingEnabled} onValueChange={(v) => update({ briefingEnabled: v })} trackColor={{ true: colors.gold }} />}
          />
          <Row
            title="Reminders before"
            detail={`Meetings ${prefs.remindBeforeMeeting} min · calls ${prefs.remindBeforeCall} min · tasks ${prefs.remindBeforeTask} min · date-only items at ${timeLabel(prefs.dayReminderTime)}`}
            right={null}
          />
          <Txt variant="caption" style={{ marginTop: 6 }}>
            Change any of these by voice: “office 10 se 7”, “Sunday off”, “gym 7 se 8 roz”, “briefing 8 baje”, “meeting reminder 30 minute pehle”.
          </Txt>
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
          {!isMD ? <Txt variant="small" style={{ marginTop: 8 }}>{mdWord(dash)}’s personal tasks are private and never shown in this app.</Txt> : null}
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
