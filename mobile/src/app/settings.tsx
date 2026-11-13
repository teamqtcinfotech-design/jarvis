import Constants from 'expo-constants';
import { Redirect, useLocalSearchParams, useRouter } from 'expo-router';
import { ExpoSpeechRecognitionModule } from 'expo-speech-recognition';
import React, { useEffect, useState } from 'react';
import { AppState, Pressable, ScrollView, StyleSheet, Switch, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { Button, Card, Chip, Icon, IconButton, Tag, Txt } from '@/components/ui';
import { useAlertEngine } from '@/lib/alertEngine';
import { api, currentServer, deviceTokens } from '@/lib/api';
import { timeLabel } from '@/lib/format';
import { addressHiOf, addressOf, mdWord } from '@/lib/names';
import { usePrefs } from '@/lib/prefs';
import { useSession } from '@/lib/session';
import { say } from '@/lib/speech';
import { colors } from '@/lib/theme';
import type { ContactItem, HabitItem, MemoryItem } from '@/lib/types';
import { useDashboard } from '@/lib/useDashboard';
import { type WakeStatus, wakeSetListening, wakeSetVoice, wakeSettings, wakeStart, wakeStatus, wakeSupported } from '@/lib/wake';

/**
 * Settings: one short page (who is signed in, six sections, sign out) and each section on its own short page
 * (`/settings?section=voice`), so nothing needs a long scroll. Nothing technical (server, sync, usage) is shown.
 */

type SectionKey = 'names' | 'voice' | 'alerts' | 'routine' | 'background' | 'memory';

const SECTIONS: { key: SectionKey; title: string; detail: string; icon: string; mdOnly?: boolean }[] = [
  { key: 'names', title: 'Names', detail: 'You, your PA and your manager', icon: 'people-outline', mdOnly: true },
  { key: 'voice', title: 'Voice & language', detail: 'Hindi or English, JARVIS’s voice', icon: 'mic-outline' },
  { key: 'alerts', title: 'Reminders & alerts', detail: 'Snooze, sound, quiet hours', icon: 'notifications-outline' },
  { key: 'routine', title: 'Work routine', detail: 'Office hours, weekly off, briefing', icon: 'calendar-outline' },
  { key: 'background', title: 'Background & “Hey JARVIS”', detail: 'Alerts and voice with the app closed', icon: 'phone-portrait-outline' },
  { key: 'memory', title: 'What JARVIS knows', detail: 'Your notes, habits and people', icon: 'bookmark-outline' },
];

/** JARVIS's male voices (Grok). Naksh is the Indian one. */
const VOICES: { id: string; label: string }[] = [
  { id: 'naksh', label: 'Naksh (Indian)' },
  { id: 'rex', label: 'Rex' },
  { id: 'leo', label: 'Leo' },
  { id: 'orion', label: 'Orion' },
  { id: 'atlas', label: 'Atlas' },
  { id: 'sirius', label: 'Sirius' },
];

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const dayNames = (days: number[]) => (days.length ? days.map((d) => WEEKDAYS[d] ?? '').filter(Boolean).join(', ') : 'none');

function Row({ title, detail, right }: { title: string; detail?: string; right?: React.ReactNode }) {
  return (
    <View style={s.row}>
      <View style={{ flex: 1, paddingRight: 12 }}>
        <Txt variant="heading" style={{ fontSize: 15 }}>{title}</Txt>
        {detail ? <Txt variant="small">{detail}</Txt> : null}
      </View>
      {right ?? null}
    </View>
  );
}

/** A name, typed and saved with one button (the server re-reads it on every phone). */
function NameField({ title, detail, value, onSave }: { title: string; detail: string; value: string; onSave: (v: string) => Promise<string> }) {
  const [text, setText] = useState(value);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [seen, setSeen] = useState(value);
  if (value !== seen) {
    // The saved name changed (on the server, or from another phone): show it unless he is typing a new one.
    setSeen(value);
    if (text === seen) setText(value);
  }
  const changed = text.trim() !== '' && text.trim() !== value;
  const save = async () => {
    setBusy(true);
    setNote(await onSave(text.trim()));
    setBusy(false);
  };
  return (
    <View style={{ paddingVertical: 8 }}>
      <Txt variant="heading" style={{ fontSize: 15 }}>{title}</Txt>
      <Txt variant="small">{note || detail}</Txt>
      <View style={{ flexDirection: 'row', alignItems: 'center', marginTop: 6 }}>
        <TextInput value={text} onChangeText={setText} style={s.input} autoCapitalize="words" maxLength={40} placeholder="Name" placeholderTextColor={colors.faint} />
        <Button title="Save" compact kind={changed ? 'primary' : 'secondary'} disabled={!changed || busy} loading={busy} onPress={save} style={{ marginLeft: 8 }} />
      </View>
    </View>
  );
}

/** Sign out (the button below, or an expired login) removes the user, and with it the alert engine this screen
 *  reads. Going to the login page first is what keeps the release build from crashing at that moment. */
export default function Settings() {
  const { user } = useSession();
  if (!user) return <Redirect href="/login" />;
  return <SettingsScreen />;
}

function SettingsScreen() {
  const router = useRouter();
  const { section } = useLocalSearchParams<{ section?: string }>();
  const { user } = useSession();
  const isMD = user?.role === 'MD';
  const open = SECTIONS.find((x) => x.key === section && (!x.mdOnly || isMD));

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }}>
      <View style={s.top}>
        <IconButton icon="chevron-back" label="Back" onPress={() => router.back()} />
        <Txt variant="title" numberOfLines={1} style={{ flex: 1, textAlign: 'center', marginRight: 40 }}>{open ? open.title : 'Settings'}</Txt>
      </View>
      {open ? (
        <ScrollView contentContainerStyle={s.page}>
          {open.key === 'names' ? <NamesSection /> : null}
          {open.key === 'voice' ? <VoiceSection /> : null}
          {open.key === 'alerts' ? <AlertsSection /> : null}
          {open.key === 'routine' ? <RoutineSection /> : null}
          {open.key === 'background' ? <BackgroundSection /> : null}
          {open.key === 'memory' ? <MemorySection /> : null}
        </ScrollView>
      ) : (
        <Menu />
      )}
    </SafeAreaView>
  );
}

/** The first page: who is signed in, the sections, sign out. Fits one screen. */
function Menu() {
  const router = useRouter();
  const { user, signOut } = useSession();
  const { data: dash } = useDashboard();
  const isMD = user?.role === 'MD';
  const role = isMD ? 'Managing Director' : user?.role === 'MG' ? 'Manager' : 'PA';
  const name = user?.name || user?.username || '';

  return (
    <ScrollView contentContainerStyle={s.page}>
      <Card style={s.account}>
        <View style={s.avatar}>
          <Txt style={s.avatarText}>{name.slice(0, 1).toUpperCase()}</Txt>
        </View>
        <View style={{ flex: 1, marginLeft: 12 }}>
          <Txt variant="heading" numberOfLines={1}>{name}</Txt>
          <Txt variant="small" numberOfLines={1}>{role} · signed in as {user?.username}</Txt>
        </View>
      </Card>

      <Card style={s.list}>
        {SECTIONS.filter((x) => !x.mdOnly || isMD).map((x, i) => (
          <Pressable
            key={x.key}
            accessibilityRole="button"
            onPress={() => router.push({ pathname: '/settings', params: { section: x.key } })}
            style={({ pressed }) => [s.item, i > 0 && s.itemLine, pressed && { opacity: 0.6 }]}
          >
            <View style={s.itemIcon}>
              <Icon name={x.icon} size={19} color={colors.gold} />
            </View>
            <View style={{ flex: 1, marginLeft: 12 }}>
              <Txt variant="heading" style={{ fontSize: 15 }}>{x.title}</Txt>
              <Txt variant="small" numberOfLines={1}>{x.detail}</Txt>
            </View>
            <Icon name="chevron-forward" size={18} color={colors.faint} />
          </Pressable>
        ))}
      </Card>

      {!isMD ? <Txt variant="small" style={s.note}>{mdWord(dash)}’s personal tasks are private and never shown in this app.</Txt> : null}
      <Button title="Sign out" kind="danger" icon="log-out-outline" onPress={signOut} style={{ marginTop: 18 }} />
      <Txt variant="caption" style={{ textAlign: 'center', marginTop: 14 }}>JARVIS {Constants.expoConfig?.version ?? ''}</Txt>
    </ScrollView>
  );
}

function NamesSection() {
  const { user } = useSession();
  const { prefs, update } = usePrefs();
  const { data: dash, reload } = useDashboard();

  /** Save a name on the server; every phone picks it up from there. Returns the line to show under the field. */
  const saveName = (key: 'myName' | 'paName' | 'managerName') => async (value: string): Promise<string> => {
    try {
      await api('/me/prefs/', { method: 'PATCH', body: { [key]: value } });
      await reload();
      return `Saved: JARVIS now says “${value}”.`;
    } catch {
      return 'Could not save. Check the connection and try again.';
    }
  };

  return (
    <Card>
      <NameField title="Your name" detail="The name JARVIS uses for you." value={user?.name ?? ''} onSave={saveName('myName')} />
      <Row title="JARVIS calls you" detail="Also how your PA and manager hear you named." />
      <View style={s.chips}>
        {['Sir', "Ma'am"].map((a) => (
          <Chip key={a} label={a} selected={prefs.addressAs === a} onPress={() => update({ addressAs: a })} />
        ))}
      </View>
      <NameField title="Your PA's name" detail="How JARVIS names your PA, on every phone." value={dash?.pa_name ?? ''} onSave={saveName('paName')} />
      <Row title={`${dash?.pa_name || 'Your PA'} is`} detail="So JARVIS says he or she correctly." />
      <View style={s.chips}>
        <Chip label="A man" selected={prefs.paGender === 'male'} onPress={() => update({ paGender: 'male' })} />
        <Chip label="A woman" selected={prefs.paGender === 'female'} onPress={() => update({ paGender: 'female' })} />
      </View>
      <NameField title="Your manager's name" detail="How JARVIS names your manager, on every phone." value={dash?.manager_name ?? ''} onSave={saveName('managerName')} />
    </Card>
  );
}

function VoiceSection() {
  const { user } = useSession();
  const { prefs, update } = usePrefs();
  const isMD = user?.role === 'MD';

  /** A new voice: used from the next sentence, with a short line in it so the change is heard at once. */
  const chooseVoice = (id: string) => {
    update({ voiceName: id });
    wakeSetVoice(id);
    setTimeout(() => say(isMD ? `Ji ${user?.address || 'Sir'}, main JARVIS hoon.` : `Ji ${user?.name || ''}, main JARVIS hoon.`, { interrupt: true, lang: 'hi' }), 150);
  };

  const testVoice = () => {
    const address = addressOf(user);
    say(isMD ? `नमस्ते ${addressHiOf(user)}, मैं जार्विस हूँ। बताइए, मैं क्या करूँ?` : `नमस्ते, मैं जार्विस हूँ।`, {
      interrupt: true, lang: 'hi', fallback: isMD ? `Namaste ${address}, main JARVIS hoon. Bataiye, main kya karoon?` : 'Namaste, main JARVIS hoon.',
    });
    say(isMD ? `Good day, ${address}. JARVIS is ready.` : `Hello ${address}. JARVIS is ready.`);
  };

  return (
    <Card>
      <Row title="JARVIS replies in" detail="“Match me” answers in the language you just used." />
      <View style={s.chips}>
        <Chip label="Match me" selected={prefs.replyLanguage === 'auto'} onPress={() => update({ replyLanguage: 'auto' })} />
        <Chip label="Always Hindi" selected={prefs.replyLanguage === 'hi'} onPress={() => update({ replyLanguage: 'hi' })} />
        <Chip label="Always English" selected={prefs.replyLanguage === 'en'} onPress={() => update({ replyLanguage: 'en' })} />
      </View>
      <Row title="Listening" detail="Speak Hindi, English or Hinglish." />
      <View style={s.chips}>
        <Chip label="Automatic" selected={prefs.listenLang === 'auto'} onPress={() => update({ listenLang: 'auto' })} />
        <Chip label="Mostly pure Hindi" selected={prefs.listenLang === 'hi-IN'} onPress={() => update({ listenLang: 'hi-IN' })} />
      </View>
      <Row title="JARVIS's voice" detail="Tap one to hear it." />
      <View style={s.chips}>
        {VOICES.map((v) => (
          <Chip key={v.id} label={v.label} selected={prefs.voiceName === v.id} onPress={() => chooseVoice(v.id)} />
        ))}
      </View>
      <Row
        title="Talk over JARVIS to interrupt"
        detail="Start talking while JARVIS speaks and it stops to listen."
        right={<Switch value={prefs.bargeIn} onValueChange={(v) => update({ bargeIn: v })} trackColor={{ true: colors.gold }} />}
      />
      <Button title="Test voice" kind="secondary" icon="volume-high-outline" compact onPress={testVoice} style={{ alignSelf: 'flex-start', marginTop: 10 }} />
    </Card>
  );
}

function AlertsSection() {
  const { user } = useSession();
  const { prefs, update } = usePrefs();
  const engine = useAlertEngine();
  const isMD = user?.role === 'MD';

  return (
    <Card>
      {engine.notificationsGranted === false ? (
        <Tag label="Notifications are blocked — allow them in Android settings" color={colors.danger} bg={colors.dangerSoft} />
      ) : null}
      <Row title="“Later” on a reminder" detail={`When no time is said: ${prefs.snoozeMinutes} minutes.`} />
      <View style={s.chips}>
        {[5, 10, 15, 30, 60].map((m) => (
          <Chip key={m} label={m === 60 ? '1 hour' : `${m} min`} selected={prefs.snoozeMinutes === m} onPress={() => update({ snoozeMinutes: m })} />
        ))}
      </View>
      <Row title="Reminder sound (app closed)" />
      <View style={s.chips}>
        <Chip label="Phone's sound" selected={prefs.reminderSound === 'default'} onPress={() => update({ reminderSound: 'default' })} />
        <Chip label="Silent · vibrate" selected={prefs.reminderSound === 'silent'} onPress={() => update({ reminderSound: 'silent' })} />
      </View>
      <Row title="Treat me as away after" detail="If the phone hasn't moved for this long, reminders wait until you pick it up." />
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
    </Card>
  );
}

function RoutineSection() {
  const { prefs, update } = usePrefs();
  return (
    <Card>
      <Row title="Office hours" detail={`${timeLabel(prefs.officeStart)} – ${timeLabel(prefs.officeEnd)}`} />
      <Row title="Weekly off" detail={dayNames(prefs.weeklyOff)} />
      <Row
        title="Fixed routine"
        detail={prefs.routineBlocks.length
          ? prefs.routineBlocks.map((b) => `${b.label} ${timeLabel(b.start)}–${timeLabel(b.end)}${b.days.length === 7 ? '' : ` (${dayNames(b.days)})`}`).join(' · ')
          : 'None yet.'}
      />
      <Row
        title="Morning briefing"
        detail={`At ${timeLabel(prefs.briefingTime)}: the day ahead, spoken when you pick up the phone.`}
        right={<Switch value={prefs.briefingEnabled} onValueChange={(v) => update({ briefingEnabled: v })} trackColor={{ true: colors.gold }} />}
      />
      <Row
        title="Reminders before"
        detail={`Meetings ${prefs.remindBeforeMeeting} min · calls ${prefs.remindBeforeCall} min · tasks ${prefs.remindBeforeTask} min`}
      />
      <Txt variant="caption" style={{ marginTop: 6 }}>
        Change any of these by voice: “office 10 se 7”, “Sunday off”, “briefing 8 baje”, “meeting reminder 30 minute pehle”.
      </Txt>
    </Card>
  );
}

function BackgroundSection() {
  const router = useRouter();
  const { user } = useSession();
  const { prefs, update } = usePrefs();
  const { data: dash } = useDashboard();
  const isMD = user?.role === 'MD';
  const [wake, setWake] = useState<WakeStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');

  useEffect(() => {
    const refresh = () => wakeStatus().then(setWake).catch(() => undefined);
    const kick = setTimeout(refresh, 0);
    const sub = AppState.addEventListener('change', (st) => st === 'active' && refresh()); // back from Android settings
    return () => {
      clearTimeout(kick);
      sub.remove();
    };
  }, []);
  const xiaomi = /xiaomi|redmi|poco/i.test(wake?.manufacturer ?? '');

  const toggleWake = async (on: boolean) => {
    setNote('');
    if (!on) {
      // Only the microphone part goes off; reminders and messages keep arriving in the background.
      await wakeSetListening(false);
      update({ wakeWordEnabled: false });
      setWake(await wakeStatus());
      return;
    }
    setBusy(true);
    try {
      const mic = await ExpoSpeechRecognitionModule.requestPermissionsAsync();
      if (!mic.granted) {
        setNote('Microphone permission is needed.');
        return;
      }
      const st = await wakeStatus();
      if (!st.supported) {
        setNote('Not available on this phone.');
        return;
      }
      if (!st.overlayAllowed) {
        setNote('Allow “Display over other apps” for JARVIS, then come back and switch this on.');
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
        reminderSound: prefs.reminderSound,
        bargeIn: prefs.bargeIn,
        voiceName: prefs.voiceName,
        speakWhenLocked: prefs.privateAloudWhenLocked,
        wakeWord: true,
      });
      update({ wakeWordEnabled: ok });
      if (!ok) setNote('Could not start. Check the microphone permission and try again.');
      setWake(await wakeStatus());
    } catch {
      setNote('Could not set this up just now. Try again in a moment.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <Row
        title="Alerts with the app closed"
        detail={
          !wakeSupported ? 'Not available on this phone.'
            : wake?.running ? 'On. Messages, tasks and reminders reach this phone even when JARVIS is closed.'
            : 'Starting… (open the app once after a restart).'
        }
        right={wake?.running ? <Tag label="On" color={colors.success} bg={colors.successSoft} /> : null}
      />
      <Row
        title="Listen for “Hey JARVIS”"
        detail={wake?.listening ? 'On. Say “Hey JARVIS” any time, even with the app closed.' : 'Off. Switch on to wake JARVIS by voice from anywhere.'}
        right={<Switch value={prefs.wakeWordEnabled && !!wake?.listening} disabled={!wakeSupported || busy} onValueChange={toggleWake} trackColor={{ true: colors.gold }} />}
      />
      {note ? <Txt variant="small" style={{ color: colors.warn, marginTop: 4 }}>{note}</Txt> : null}
      {wakeSupported ? (
        <View style={[s.chips, { marginTop: 8 }]}>
          <Chip label="Phone setup" icon="construct-outline" onPress={() => router.push('/setup')} />
          {!wake?.overlayAllowed ? <Chip label="Allow display over other apps" icon="open-outline" onPress={() => wakeSettings.overlay()} /> : null}
          {!wake?.batteryUnrestricted ? <Chip label="Battery: no restrictions" icon="battery-charging-outline" onPress={() => wakeSettings.battery()} /> : null}
          {xiaomi ? <Chip label="Autostart (Xiaomi)" icon="power-outline" onPress={() => { if (!wakeSettings.autostart()) setNote('Open Security app → Autostart and allow JARVIS.'); }} /> : null}
          {xiaomi ? (
            <Chip
              label="Pop-ups from background (Xiaomi)"
              icon="albums-outline"
              onPress={() => {
                setNote('On the page that opens, switch on “Display pop-up windows while running in the background”.');
                wakeSettings.miuiPermissions();
              }}
            />
          ) : null}
        </View>
      ) : null}
      <Txt variant="caption" style={{ marginTop: 6 }}>
        Listening happens on the phone; nothing is sent until you say the words. After a phone restart, open JARVIS once.
        {xiaomi ? ' On Xiaomi, allow Autostart, Battery and Pop-ups from background.' : ''}
      </Txt>
    </Card>
  );
}

function MemorySection() {
  const [memories, setMemories] = useState<MemoryItem[] | null>(null);
  const [contacts, setContacts] = useState<ContactItem[] | null>(null);
  const [habits, setHabits] = useState<HabitItem[] | null>(null);

  useEffect(() => {
    const kick = setTimeout(() => {
      api<{ results: MemoryItem[] }>('/memories/').then((r) => setMemories(r.data.results)).catch(() => setMemories([]));
      api<{ results: ContactItem[] }>('/contacts/').then((r) => setContacts(r.data.results)).catch(() => setContacts([]));
      api<{ results: HabitItem[] }>('/me/habits/').then((r) => setHabits(r.data.results)).catch(() => setHabits([]));
    }, 0);
    return () => clearTimeout(kick);
  }, []);

  const forget = async (id: string) => {
    setMemories((list) => (list ?? []).filter((m) => m.id !== id));
    await api(`/memories/${id}/`, { method: 'DELETE' }).catch(() => undefined);
  };
  const forgetContact = async (id: number) => {
    setContacts((list) => (list ?? []).filter((c) => c.id !== id));
    await api(`/contacts/${id}/`, { method: 'DELETE' }).catch(() => undefined);
  };
  const forgetHabit = async (key: string) => {
    try {
      const r = await api<{ results: HabitItem[] }>(`/me/habits/${key}/`, { method: 'DELETE' });
      setHabits(r.data.results);
    } catch {
      // keep the list as it is
    }
  };

  return (
    <>
      <Txt variant="label">Notes</Txt>
      <Card style={s.card}>
        {memories === null ? <Txt variant="small">Loading…</Txt> : null}
        {memories?.length === 0 ? <Txt variant="small">Nothing yet. Say “yaad rakhna ki…” and JARVIS keeps it here.</Txt> : null}
        {memories?.map((m) => (
          <View key={m.id} style={s.memory}>
            <Icon name="bookmark-outline" size={16} color={colors.gold} />
            <Txt variant="body" style={{ flex: 1, marginLeft: 8 }}>{m.text}</Txt>
            <Pressable onPress={() => forget(m.id)} hitSlop={10} accessibilityLabel="Forget this">
              <Icon name="trash-outline" size={18} color={colors.muted} />
            </Pressable>
          </View>
        ))}
      </Card>

      <Txt variant="label" style={s.label}>Habits</Txt>
      <Card style={s.card}>
        {habits === null ? <Txt variant="small">Loading…</Txt> : null}
        {habits?.map((h) => (
          <View key={h.key} style={s.memory}>
            <Icon name={h.set ? 'sparkles-outline' : 'ellipse-outline'} size={16} color={h.set ? colors.gold : colors.faint} />
            <View style={{ flex: 1, marginLeft: 8 }}>
              <Txt variant="body">{h.label}: <Txt variant="heading" style={{ fontSize: 14 }}>{h.value}</Txt></Txt>
              <Txt variant="caption">{h.learned}</Txt>
            </View>
            {h.set ? (
              <Pressable onPress={() => forgetHabit(h.key)} hitSlop={10} accessibilityLabel={`Forget ${h.label}`}>
                <Icon name="refresh-outline" size={18} color={colors.muted} />
              </Pressable>
            ) : null}
          </View>
        ))}
      </Card>

      <Txt variant="label" style={s.label}>People</Txt>
      <Card style={s.card}>
        {contacts === null ? <Txt variant="small">Loading…</Txt> : null}
        {contacts?.length === 0 ? <Txt variant="small">Nobody yet. When you name someone new, JARVIS asks once and keeps them here.</Txt> : null}
        {contacts?.map((c) => (
          <View key={c.id} style={s.memory}>
            <Icon name="person-outline" size={16} color={colors.gold} />
            <Txt variant="body" style={{ flex: 1, marginLeft: 8 }}>{c.note ? `${c.name} · ${c.note}` : c.name}</Txt>
            <Pressable onPress={() => forgetContact(c.id)} hitSlop={10} accessibilityLabel={`Remove ${c.name}`}>
              <Icon name="trash-outline" size={18} color={colors.muted} />
            </Pressable>
          </View>
        ))}
      </Card>
      <Txt variant="caption" style={{ marginTop: 10 }}>Only you can see these.</Txt>
    </>
  );
}

const s = StyleSheet.create({
  top: { flexDirection: 'row', alignItems: 'center', paddingHorizontal: 12, paddingTop: 6 },
  page: { padding: 20, paddingBottom: 48 },
  account: { flexDirection: 'row', alignItems: 'center' },
  avatar: { width: 44, height: 44, borderRadius: 22, backgroundColor: colors.goldSoft, alignItems: 'center', justifyContent: 'center' },
  avatarText: { fontSize: 18, fontWeight: '700', color: colors.gold, lineHeight: 24 },
  list: { marginTop: 16, paddingVertical: 4 },
  item: { flexDirection: 'row', alignItems: 'center', paddingVertical: 12 },
  itemLine: { borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: colors.line },
  itemIcon: { width: 36, height: 36, borderRadius: 10, backgroundColor: colors.goldSoft, alignItems: 'center', justifyContent: 'center' },
  note: { marginTop: 14, textAlign: 'center' },
  card: { marginTop: 8 },
  label: { marginTop: 20 },
  row: { flexDirection: 'row', alignItems: 'center', paddingVertical: 8 },
  chips: { flexDirection: 'row', flexWrap: 'wrap', marginTop: 2 },
  input: { flex: 1, borderWidth: 1, borderColor: colors.line, borderRadius: 10, paddingHorizontal: 12, paddingVertical: 8, fontSize: 15, color: colors.text, backgroundColor: colors.surface },
  memory: { flexDirection: 'row', alignItems: 'center', paddingVertical: 8, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: colors.line },
});
