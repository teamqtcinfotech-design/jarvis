/**
 * First-time setup on this phone: every Android permission JARVIS needs, asked one after another, then
 * "Hey JARVIS" switched on. Dialogs are answered in place; the two settings Android only offers as a page
 * (display over other apps, battery) open that page and the flow continues when the user comes back.
 */
import * as Notifications from 'expo-notifications';
import { useRouter } from 'expo-router';
import { ExpoSpeechRecognitionModule } from 'expo-speech-recognition';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AppState, ScrollView, StyleSheet, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { Button, Card, Icon, Txt } from '@/components/ui';
import { currentServer, deviceTokens } from '@/lib/api';
import { addressHiOf, addressOf } from '@/lib/names';
import { usePrefs } from '@/lib/prefs';
import { useSession } from '@/lib/session';
import { markSetupDone } from '@/lib/setup';
import { colors } from '@/lib/theme';
import { type WakeStatus, wakeSettings, wakeStart, wakeStatus, wakeSupported } from '@/lib/wake';

type StepKey = 'mic' | 'notifications' | 'overlay' | 'battery' | 'autostart' | 'popups';
type StepState = 'todo' | 'done' | 'skipped' | 'opened';

interface Step {
  key: StepKey;
  title: string;
  detail: string;
  icon: string;
}

export default function Setup() {
  const router = useRouter();
  const { user } = useSession();
  const { update, prefs } = usePrefs();
  const [status, setStatus] = useState<WakeStatus | null>(null);
  const [states, setStates] = useState<Partial<Record<StepKey, StepState>>>({});
  const [running, setRunning] = useState(false);
  const [note, setNote] = useState('');
  const waitingFor = useRef<StepKey | null>(null); // a system page is open; continue when the user is back
  const isMD = user?.role === 'MD';
  const xiaomi = /xiaomi|redmi|poco/i.test(status?.manufacturer ?? '');

  const steps = useMemo<Step[]>(() => [
    { key: 'mic', title: 'Microphone', detail: 'To hear your commands and the words “Hey JARVIS”.', icon: 'mic-outline' },
    { key: 'notifications', title: 'Notifications', detail: 'Reminders and messages when the app is closed.', icon: 'notifications-outline' },
    { key: 'overlay', title: 'Display over other apps', detail: 'The JARVIS orb can appear over whatever is open.', icon: 'albums-outline' },
    { key: 'battery', title: 'Run in the background', detail: 'So Android does not put JARVIS to sleep.', icon: 'battery-charging-outline' },
    ...(xiaomi
      ? [
          { key: 'autostart' as StepKey, title: 'Autostart (Xiaomi)', detail: 'Lets JARVIS start again after a restart.', icon: 'power-outline' },
          { key: 'popups' as StepKey, title: 'Pop-ups from background (Xiaomi)', detail: 'Switch on “Display pop-up windows while running in the background”.', icon: 'open-outline' },
        ]
      : []),
  ], [xiaomi]);

  const refresh = useCallback(async () => {
    const st = await wakeStatus();
    setStatus(st);
    setStates((prev) => ({
      ...prev,
      mic: st.micAllowed ? 'done' : prev.mic ?? 'todo',
      notifications: st.notificationsAllowed ? 'done' : prev.notifications ?? 'todo',
      overlay: st.overlayAllowed ? 'done' : prev.overlay ?? 'todo',
      battery: st.batteryUnrestricted ? 'done' : prev.battery ?? 'todo',
    }));
    return st;
  }, []);

  useEffect(() => {
    const kick = setTimeout(refresh, 0);
    return () => clearTimeout(kick);
  }, [refresh]);

  const finish = useCallback(async () => {
    setRunning(true);
    try {
      const st = await refresh();
      if (wakeSupported && user) {
        const tokens = await deviceTokens();
        const listening = await wakeStart({
          serverUrl: currentServer(),
          accessToken: tokens.access,
          refreshToken: tokens.refresh,
          address: addressOf(user),
          addressHi: addressHiOf(user),
          isMd: isMD,
          voiceAccent: prefs.voiceAccent,
          listenLang: prefs.listenLang === 'hi-IN' ? 'hi-IN' : 'en-IN',
          replyLang: prefs.replyLanguage === 'auto' ? user.language : prefs.replyLanguage,
          speakWhenLocked: prefs.privateAloudWhenLocked,
          wakeWord: st.micAllowed,
          bargeIn: prefs.bargeIn,
          voiceName: prefs.voiceName,
        });
        update({ wakeWordEnabled: listening });
      }
    } catch {
      setNote('Could not reach the server to switch on “Hey JARVIS”. You can do it later in Settings.');
    } finally {
      await markSetupDone();
      setRunning(false);
      router.replace('/');
    }
  }, [refresh, user, isMD, prefs.privateAloudWhenLocked, prefs.voiceAccent, prefs.listenLang, prefs.replyLanguage, prefs.bargeIn, prefs.voiceName, update, router]);

  /** Runs the steps in order; returns after opening a system page (the flow continues on return). */
  const runFrom = useCallback(
    async (from: number) => {
      setRunning(true);
      setNote('');
      try {
        for (let i = from; i < steps.length; i++) {
          const step = steps[i];
          if (states[step.key] === 'done' || states[step.key] === 'opened') continue;
          if (step.key === 'mic') {
            const r = await ExpoSpeechRecognitionModule.requestPermissionsAsync();
            setStates((p) => ({ ...p, mic: r.granted ? 'done' : 'skipped' }));
            if (!r.granted) setNote('Without the microphone JARVIS cannot hear you. You can allow it later in Android settings.');
          } else if (step.key === 'notifications') {
            const r = await Notifications.requestPermissionsAsync().catch(() => null);
            setStates((p) => ({ ...p, notifications: r?.granted ? 'done' : 'skipped' }));
          } else if (step.key === 'overlay' || step.key === 'battery' || step.key === 'autostart' || step.key === 'popups') {
            waitingFor.current = step.key;
            if (step.key === 'overlay') wakeSettings.overlay();
            else if (step.key === 'battery') wakeSettings.battery();
            else if (step.key === 'autostart') {
              if (!wakeSettings.autostart()) {
                waitingFor.current = null;
                setStates((p) => ({ ...p, autostart: 'skipped' }));
                continue;
              }
            } else if (!wakeSettings.miuiPermissions()) {
              waitingFor.current = null;
              setStates((p) => ({ ...p, popups: 'skipped' }));
              continue;
            }
            if (step.key === 'autostart' || step.key === 'popups') setStates((p) => ({ ...p, [step.key]: 'opened' }));
            setRunning(false);
            return; // back from the system page -> AppState 'active' -> continue below
          }
        }
        await finish();
      } catch {
        setRunning(false);
      }
    },
    // steps and states are read fresh on every run
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [states, finish],
  );

  // Back from a system settings page: re-check and carry on with the next step.
  useEffect(() => {
    const sub = AppState.addEventListener('change', (s) => {
      if (s !== 'active' || !waitingFor.current) return;
      const key = waitingFor.current;
      waitingFor.current = null;
      refresh().then(() => {
        const idx = steps.findIndex((st) => st.key === key);
        const kick = setTimeout(() => runFrom(idx + 1), 350);
        return () => clearTimeout(kick);
      });
    });
    return () => sub.remove();
  }, [refresh, runFrom, steps]);

  const allDone = steps.every((st) => states[st.key] === 'done' || states[st.key] === 'opened');

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }}>
      <ScrollView contentContainerStyle={s.wrap}>
        <View style={s.crest}>
          <Icon name="shield-checkmark-outline" size={30} color={colors.gold} />
        </View>
        <Txt variant="title" style={{ marginTop: 14, textAlign: 'center' }}>Set up JARVIS on this phone</Txt>
        <Txt variant="small" style={{ textAlign: 'center', marginTop: 6, color: colors.inkSoft }}>
          A few one-time permissions so JARVIS can hear “Hey JARVIS”, speak reminders and show up over other apps.
          Tap Allow on each question; JARVIS switches itself on at the end.
        </Txt>

        <Card style={{ marginTop: 22, alignSelf: 'stretch' }}>
          {steps.map((st, i) => {
            const state = states[st.key] ?? 'todo';
            const ok = state === 'done' || state === 'opened';
            return (
              <View key={st.key} style={[s.step, i < steps.length - 1 && s.stepLine]}>
                <View style={[s.stepIcon, ok && { backgroundColor: colors.successSoft }]}>
                  <Icon name={ok ? 'checkmark' : st.icon} size={18} color={ok ? colors.success : colors.ink} />
                </View>
                <View style={{ flex: 1 }}>
                  <Txt variant="heading" style={{ fontSize: 15 }}>{st.title}</Txt>
                  <Txt variant="small">{st.detail}</Txt>
                  {state === 'skipped' ? <Txt variant="caption" style={{ color: colors.warn, marginTop: 2 }}>Not allowed yet</Txt> : null}
                </View>
                {!ok ? <Button title="Allow" kind="ghost" compact onPress={() => runFrom(i)} disabled={running} /> : null}
              </View>
            );
          })}
        </Card>

        {note ? <Txt variant="small" style={{ color: colors.warn, marginTop: 12 }}>{note}</Txt> : null}

        <Button
          title={allDone ? 'Switch JARVIS on' : 'Allow everything'}
          icon={allDone ? 'sparkles-outline' : 'checkmark-done-outline'}
          onPress={() => (allDone ? finish() : runFrom(0))}
          loading={running}
          style={{ marginTop: 22, alignSelf: 'stretch' }}
        />
        <Button title="Not now" kind="ghost" compact onPress={finish} disabled={running} loading={running} style={{ marginTop: 10 }} />
        <Txt variant="caption" style={{ textAlign: 'center', marginTop: 14 }}>
          Nothing is recorded or sent until you speak to JARVIS. You can change any of this later in Settings.
        </Txt>
      </ScrollView>
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  wrap: { flexGrow: 1, alignItems: 'center', padding: 24, paddingTop: 36 },
  crest: { width: 64, height: 64, borderRadius: 32, borderWidth: 1, borderColor: colors.gold, backgroundColor: colors.goldSoft, alignItems: 'center', justifyContent: 'center' },
  step: { flexDirection: 'row', alignItems: 'center', paddingVertical: 12 },
  stepLine: { borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: colors.line },
  stepIcon: { width: 36, height: 36, borderRadius: 18, backgroundColor: colors.surfaceAlt, alignItems: 'center', justifyContent: 'center', marginRight: 12 },
});
