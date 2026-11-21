import Constants from 'expo-constants';
import { Redirect, useLocalSearchParams, useRouter } from 'expo-router';
import { ExpoSpeechRecognitionModule } from 'expo-speech-recognition';
import React, { useEffect, useState } from 'react';
import { AppState, Pressable, ScrollView, StyleSheet, Switch, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { DepartmentsPanel } from '@/components/DepartmentsPanel';
import { SectionsPanel } from '@/components/SectionsPanel';
import { WalkiePanel } from '@/components/WalkiePanel';
import { DevicesPanel, PersonDevices } from '@/components/DevicesPanel';
import { Button, Card, Chip, Icon, IconButton, Tag, Txt } from '@/components/ui';
import { useAlertEngine } from '@/lib/alertEngine';
import { api, currentServer, deviceTokens } from '@/lib/api';
import {
  checkForUpdate, installApk, onUpdates, restartIntoUpdate, runningUpdateCode, runningUpdateDate, updatesSnapshot,
  type UpdatesSnapshot,
} from '@/lib/appUpdates';
import { timeLabel } from '@/lib/format';
import { addressHiOf, addressOf, mdWord } from '@/lib/names';
import { usePrefs } from '@/lib/prefs';
import { useSession } from '@/lib/session';
import { say } from '@/lib/speech';
import { colors } from '@/lib/theme';
import type { ContactItem, HabitItem, MemoryItem } from '@/lib/types';
import { useDashboard } from '@/lib/useDashboard';
import { liveAvailable } from '@/lib/liveVoice';
import { type WakeStatus, wakeSetListening, wakeSetVoice, wakeSettings, wakeStart, wakeStatus, wakeSupported } from '@/lib/wake';
import { WAKE_WORDS, wakeWordOf } from '@/lib/wakeWords';

/**
 * Settings: one short page (who is signed in, six sections, sign out) and each section on its own short page
 * (`/settings?section=voice`), so nothing needs a long scroll. Nothing technical (server, sync, usage) is shown.
 */

type SectionKey = 'team' | 'departments' | 'sections' | 'walkie' | 'devices' | 'names' | 'voice' | 'alerts' | 'routine' | 'background' | 'memory';

const SECTIONS: { key: SectionKey; title: string; detail: string; icon: string; mdOnly?: boolean }[] = [
  { key: 'team', title: 'Team', detail: 'Add people, their jobs and logins', icon: 'people-outline', mdOnly: true },
  { key: 'departments', title: 'Departments', detail: 'Groups of people, here or by voice', icon: 'git-network-outline' },
  { key: 'sections', title: 'Sections', detail: 'Folders of work: GST, Franchise…', icon: 'folder-outline' },
  { key: 'walkie', title: 'Walkie-talkie', detail: 'Live voice with the team, quiet time', icon: 'radio-outline' },
  { key: 'devices', title: 'Devices & lock', detail: 'Where you are signed in, fingerprint', icon: 'finger-print-outline' },
  { key: 'names', title: 'Your name', detail: 'And how JARVIS addresses you', icon: 'person-circle-outline', mdOnly: true },
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
        <ScrollView contentContainerStyle={s.page} keyboardShouldPersistTaps="handled">
          {open.key === 'team' ? <TeamSection /> : null}
          {open.key === 'departments' ? <DepartmentsPanel /> : null}
          {open.key === 'sections' ? <SectionsPanel /> : null}
          {open.key === 'walkie' ? <WalkiePanel /> : null}
          {open.key === 'devices' ? <DevicesPanel /> : null}
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
  const role = user?.title || (isMD ? 'Managing Director' : user?.role === 'MG' ? 'Manager' : user?.role === 'PA' ? 'PA' : 'Team member');
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

      <UpdatesCard />
      {!isMD ? <Txt variant="small" style={s.note}>Personal items are private: yours only you see, and {mdWord(dash)}’s are never shown here.</Txt> : null}
      <Button title="Sign out" kind="danger" icon="log-out-outline" onPress={signOut} style={{ marginTop: 18 }} />
    </ScrollView>
  );
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const shortDate = (d: Date) => `${d.getDate()} ${MONTHS[d.getMonth()]}, ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;

/** Which version runs, whether a newer one is on its way, and a new APK when one is offered. */
function UpdatesCard() {
  const [u, setU] = useState<UpdatesSnapshot>(updatesSnapshot());
  const [restarting, setRestarting] = useState(false);
  useEffect(() => onUpdates(setU), []);
  const since = runningUpdateDate();
  const status: Record<UpdatesSnapshot['state'], string> = {
    off: 'Automatic updates are off in this build.',
    idle: 'New versions arrive by themselves.',
    checking: 'Checking for a new version…',
    downloading: 'Downloading a new version…',
    ready: 'A new version is ready. It switches on by itself the next time JARVIS is in the background.',
    current: 'Up to date.',
    error: 'Could not check just now. It tries again by itself.',
  };
  const canCheck = u.state !== 'off' && u.state !== 'checking' && u.state !== 'downloading';
  return (
    <Card style={{ marginTop: 16 }}>
      <Row
        title={`JARVIS ${Constants.expoConfig?.version ?? ''}`}
        detail={since ? `Updated ${shortDate(since)} · ${runningUpdateCode()}` : 'As installed'}
        right={u.tester ? <Tag label="Test phone" color={colors.gold} bg={colors.goldSoft} /> : null}
      />
      <Txt variant="small">{status[u.state]}</Txt>
      <View style={[s.chips, { marginTop: 8 }]}>
        {u.ready ? (
          <Button title="Restart now" compact icon="refresh-outline" loading={restarting}
            onPress={() => { setRestarting(true); restartIntoUpdate().catch(() => setRestarting(false)); }} style={{ marginRight: 8 }} />
        ) : null}
        {canCheck && !u.ready ? <Chip label="Check now" icon="cloud-download-outline" onPress={() => void checkForUpdate(true)} /> : null}
      </View>
      {u.apk ? (
        <View style={[s.itemLine, { marginTop: 12, paddingTop: 10 }]}>
          <Row title={`New version ${u.apk.version}`} detail={u.apk.notes || 'A new APK: download it, then tap Install.'} />
          {u.apkProgress !== null ? (
            <Txt variant="small">Downloading… {Math.round(u.apkProgress * 100)}%</Txt>
          ) : (
            <Button title="Download and install" compact icon="download-outline" onPress={() => void installApk()} style={{ alignSelf: 'flex-start' }} />
          )}
          {u.apkError ? <Txt variant="small" style={{ color: colors.danger, marginTop: 6 }}>{u.apkError}</Txt> : null}
        </View>
      ) : null}
      {u.deviceId ? <Txt variant="caption" style={{ marginTop: 10 }}>Phone ID {u.deviceId.slice(0, 8)}</Txt> : null}
    </Card>
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
      <Row title="JARVIS calls you" detail="Also how everyone on the team hears you named." />
      <View style={s.chips}>
        {['Sir', "Ma'am"].map((a) => (
          <Chip key={a} label={a} selected={prefs.addressAs === a} onPress={() => update({ addressAs: a })} />
        ))}
      </View>
      <Txt variant="caption" style={{ marginTop: 10 }}>Names, jobs and logins of {dash?.pa_name || 'your PA'} and everyone else are on the Team page.</Txt>
    </Card>
  );
}

interface TeamPerson {
  id: number;
  name: string;
  username: string;
  role: string;
  title: string;
  department: string;
  gender: '' | 'male' | 'female';
  active: boolean;
  you?: boolean;
  /** The number they sign in with ("+91 98765 43210"); the MD sees everyone's, each person their own. */
  phone?: string;
}

const GENDERS: { id: '' | 'male' | 'female'; label: string }[] = [
  { id: '', label: 'Not said' },
  { id: 'male', label: 'Man' },
  { id: 'female', label: 'Woman' },
];

/** The MD's Team page: everyone with a login, add a person, change a person's job / department / gender, reset a
 *  password, or switch an account off (nothing they had is deleted; switching on again brings it all back). */
function TeamSection() {
  const { reload } = useDashboard();
  const [people, setPeople] = useState<TeamPerson[] | null>(null);
  const [error, setError] = useState('');
  const [openId, setOpenId] = useState<number | 'new' | null>(null);
  const load = () => api<{ results: TeamPerson[] }>('/team/').then((r) => setPeople(r.data.results)).catch(() => setError('Could not load the team.'));
  useEffect(() => {
    const kick = setTimeout(load, 0);
    return () => clearTimeout(kick);
  }, []);
  const changed = async () => {
    await load();
    reload().catch(() => undefined);
  };
  if (!people) return <Txt variant="small" style={{ textAlign: 'center', marginTop: 30 }}>{error || 'Loading…'}</Txt>;
  const active = people.filter((p) => p.active);
  const off = people.filter((p) => !p.active);
  return (
    <>
      <Txt variant="small" style={{ marginBottom: 10 }}>
        {active.length} {active.length === 1 ? 'person' : 'people'} can sign in. Anyone can give anyone work or a message by voice; personal items stay with their owner.
      </Txt>
      {openId === 'new' ? (
        <PersonForm onDone={async () => { setOpenId(null); await changed(); }} onCancel={() => setOpenId(null)} />
      ) : (
        <Button title="Add a person" icon="person-add-outline" onPress={() => setOpenId('new')} style={{ marginBottom: 12 }} />
      )}
      <Card style={{ paddingVertical: 4 }}>
        {[...active, ...off].map((p, i) => (
          <View key={p.id} style={i > 0 ? s.itemLine : undefined}>
            <Pressable onPress={() => setOpenId(openId === p.id ? null : p.id)} style={({ pressed }) => [s.item, pressed && { opacity: 0.6 }]}>
              <View style={[s.avatar, { width: 36, height: 36, borderRadius: 18 }, !p.active && { opacity: 0.4 }]}>
                <Txt style={[s.avatarText, { fontSize: 15 }]}>{p.name.slice(0, 1).toUpperCase()}</Txt>
              </View>
              <View style={{ flex: 1, marginLeft: 12 }}>
                <Txt variant="heading" style={[{ fontSize: 15 }, !p.active && { color: colors.muted }]}>{p.name}{p.you ? ' (you)' : ''}</Txt>
                <Txt variant="small" numberOfLines={1}>{[p.title, p.department, p.phone || `login: ${p.username}`].filter(Boolean).join(' · ')}</Txt>
              </View>
              {!p.active ? <Tag label="Off" color={colors.muted} /> : <Icon name={openId === p.id ? 'chevron-up' : 'chevron-down'} size={18} color={colors.faint} />}
            </Pressable>
            {openId === p.id ? <PersonForm person={p} onDone={async () => { setOpenId(null); await changed(); }} onCancel={() => setOpenId(null)} /> : null}
          </View>
        ))}
      </Card>
    </>
  );
}

/** Add a person (no `person`), or change one. */
function PersonForm({ person, onDone, onCancel }: { person?: TeamPerson; onDone: () => void; onCancel: () => void }) {
  const isNew = !person;
  const [name, setName] = useState(person?.name ?? '');
  const [title, setTitle] = useState(person?.title ?? '');
  const [department, setDepartment] = useState(person?.department ?? '');
  const [gender, setGender] = useState<'' | 'male' | 'female'>(person?.gender ?? '');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [phone, setPhone] = useState(person?.phone ?? '');
  const [busy, setBusy] = useState('');
  const [note, setNote] = useState('');
  const save = async (extra: Record<string, unknown> = {}, label = 'save') => {
    setBusy(label);
    setNote('');
    try {
      if (isNew) {
        await api('/team/', { method: 'POST', body: { name, title, department, gender, phone, username: username.trim().toLowerCase(), password } });
      } else {
        const body: Record<string, unknown> = { name, title, department, gender, ...extra };
        if (phone !== (person.phone ?? '')) body.phone = phone;
        if (password) body.password = password;
        await api(`/team/${person.id}/`, { method: 'PATCH', body });
      }
      onDone();
    } catch (e) {
      setNote(e instanceof Error ? e.message : 'Could not save.');
    } finally {
      setBusy('');
    }
  };
  const ready = name.trim() && (!isNew || (username.trim().length >= 3 && password.length >= 3));
  return (
    <View style={s.personForm}>
      <Txt variant="label" style={s.formLabel}>Name</Txt>
      <TextInput value={name} onChangeText={setName} style={s.formInput} autoCapitalize="words" maxLength={40} placeholder="Sunil" placeholderTextColor={colors.faint} />
      <Txt variant="label" style={s.formLabel}>Job</Txt>
      <TextInput value={title} onChangeText={setTitle} style={s.formInput} autoCapitalize="words" maxLength={80} placeholder="Accountant, Farm Manager, Director…" placeholderTextColor={colors.faint} />
      <Txt variant="label" style={s.formLabel}>Mobile number (they sign in with it)</Txt>
      <TextInput value={phone} onChangeText={setPhone} style={s.formInput} keyboardType="phone-pad" maxLength={18} placeholder="98765 43210" placeholderTextColor={colors.faint} />
      <Txt variant="label" style={s.formLabel}>Department (optional)</Txt>
      <TextInput value={department} onChangeText={setDepartment} style={s.formInput} autoCapitalize="words" maxLength={80} placeholder="Accounts, Farm, Social Media…" placeholderTextColor={colors.faint} />
      <Txt variant="label" style={s.formLabel}>So JARVIS says he or she correctly</Txt>
      <View style={[s.chips, { marginTop: 6 }]}>
        {GENDERS.map((g) => <Chip key={g.id || 'none'} label={g.label} selected={gender === g.id} onPress={() => setGender(g.id)} />)}
      </View>
      {isNew ? (
        <>
          <Txt variant="label" style={s.formLabel}>Login username</Txt>
          <TextInput value={username} onChangeText={setUsername} style={s.formInput} autoCapitalize="none" autoCorrect={false} maxLength={30} placeholder="sunil" placeholderTextColor={colors.faint} />
        </>
      ) : null}
      <Txt variant="label" style={s.formLabel}>{isNew ? 'Password' : 'New password (leave empty to keep it)'}</Txt>
      <TextInput value={password} onChangeText={setPassword} style={s.formInput} autoCapitalize="none" autoCorrect={false} maxLength={60} placeholder={isNew ? 'At least 3 characters' : ''} placeholderTextColor={colors.faint} />
      {note ? <Txt variant="small" style={{ color: colors.danger, marginTop: 8 }}>{note}</Txt> : null}
      <View style={{ flexDirection: 'row', marginTop: 12 }}>
        <Button title={isNew ? 'Add' : 'Save'} compact disabled={!ready || !!busy} loading={busy === 'save'} onPress={() => save()} style={{ flex: 1, marginRight: 8 }} />
        <Button title="Close" kind="secondary" compact onPress={onCancel} style={{ flex: 1 }} />
      </View>
      {!isNew && person.role !== 'MD' ? (
        <Button
          title={person.active ? 'Switch this account off' : 'Switch this account on again'}
          kind={person.active ? 'danger' : 'secondary'}
          compact
          loading={busy === 'active'}
          onPress={() => save({ active: !person.active }, 'active')}
          style={{ marginTop: 8 }}
        />
      ) : null}
      {!isNew && person.active && person.role !== 'MD' ? (
        <Txt variant="caption" style={{ marginTop: 6 }}>Switched off, they cannot sign in; their tasks stay and come back if you switch them on again.</Txt>
      ) : null}
      {!isNew ? <PersonDevices userId={person.id} /> : null}
    </View>
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
      <Row
        title="Silent replies in a meeting"
        detail="In meeting mode (or a meeting in your diary) JARVIS still saves and sends, but answers on the screen with a vibration, not aloud."
        right={<Switch value={prefs.meetingQuietReplies} onValueChange={(v) => update({ meetingQuietReplies: v })} trackColor={{ true: colors.gold }} />}
      />
      {liveAvailable ? (
        <Row
          title="Live conversation"
          detail="JARVIS listens and answers on one open line: replies in about a second. Off: the usual turn by turn."
          right={<Switch value={prefs.liveVoice} onValueChange={(v) => update({ liveVoice: v })} trackColor={{ true: colors.gold }} />}
        />
      ) : null}
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
      <Row
        title="Ring until I tap Seen"
        detail={prefs.ringUntilSeen
          ? `Work and messages from the team ring every ${prefs.ringEveryMinutes} min (urgent every ${prefs.ringUrgentEveryMinutes} min) until you tap Seen. After ${prefs.ringForMinutes} min the sender hears it was not seen.`
          : 'Off: work and messages arrive once, as before. The sender still sees when you open them.'}
        right={<Switch value={prefs.ringUntilSeen} onValueChange={(v) => update({ ringUntilSeen: v })} trackColor={{ true: colors.gold }} />}
      />
      {prefs.ringUntilSeen ? (
        <View style={s.chips}>
          {[2, 5, 10, 15].map((m) => (
            <Chip key={m} label={`Every ${m} min`} selected={prefs.ringEveryMinutes === m} onPress={() => update({ ringEveryMinutes: m })} />
          ))}
        </View>
      ) : null}
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
  const wakeWord = wakeWordOf(prefs.wakeWord);

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
        title={`Listen for “${wakeWord.phrase}”`}
        detail={wake?.listening ? `On. Say “${wakeWord.phrase}” any time, even with the app closed.` : `Off. Switch on to wake ${wakeWord.name} by voice from anywhere.`}
        right={<Switch value={prefs.wakeWordEnabled && !!wake?.listening} disabled={!wakeSupported || busy} onValueChange={toggleWake} trackColor={{ true: colors.gold }} />}
      />
      <Row
        title="Wake word"
        detail={`Pick the words that wake it. The assistant is then called ${wakeWord.name} and answers to that name; the app stays JARVIS. By voice: “ab mujhe Hey Billy bolke bulao”.`}
      />
      <View style={[s.chips, { marginTop: 4 }]}>
        {WAKE_WORDS.map((w) => (
          <Chip key={w.key} label={w.phrase} icon={w.key === wakeWord.key ? 'checkmark-circle' : 'ellipse-outline'} onPress={() => update({ wakeWord: w.key })} />
        ))}
      </View>
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
  personForm: { paddingHorizontal: 4, paddingBottom: 14 },
  formLabel: { marginTop: 10, color: colors.inkSoft },
  formInput: { borderWidth: 1, borderColor: colors.line, borderRadius: 10, paddingHorizontal: 12, paddingVertical: 8, fontSize: 15,
    color: colors.text, backgroundColor: colors.surface, marginTop: 4 },
});
