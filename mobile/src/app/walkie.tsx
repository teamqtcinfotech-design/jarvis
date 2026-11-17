/**
 * The walkie-talkie screen: who to talk to (one, several, a department, everyone), the big HOLD TO TALK button,
 * and what is happening ("Ready", "Talking", "Mansi is talking…"). Comes up by itself, over the lock screen too,
 * when somebody talks to this phone.
 */
import { Redirect, useLocalSearchParams, useRouter } from 'expo-router';
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Animated, Pressable, ScrollView, StyleSheet, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { Chip, Icon, IconButton, Txt } from '@/components/ui';
import { useSession } from '@/lib/session';
import { colors, radius, shadow } from '@/lib/theme';
import { useDashboard } from '@/lib/useDashboard';
import { type WalkiePerson, useWalkie } from '@/lib/walkie';

const STATUS: Record<WalkiePerson['status'], { label: string; color: string }> = {
  available: { label: 'Available', color: colors.success },
  quiet: { label: 'Quiet', color: colors.warn },
  off: { label: 'Off', color: colors.faint },
  reach: { label: "Can't reach", color: colors.danger },
};

const timeOf = (iso: string) => {
  if (!iso) return '';
  const d = new Date(iso);
  const h = d.getHours() % 12 || 12;
  return `${h}:${String(d.getMinutes()).padStart(2, '0')} ${d.getHours() < 12 ? 'AM' : 'PM'}`;
};

export default function WalkieRoute() {
  const { user } = useSession();
  if (!user) return <Redirect href="/login" />;
  return <WalkieScreen />;
}

function WalkieScreen() {
  const router = useRouter();
  const w = useWalkie();
  const { data: dash } = useDashboard();
  const params = useLocalSearchParams<{ to?: string; quick?: string }>();
  const [selected, setSelected] = useState<number[]>(() => (params.to ? params.to.split(',').map(Number).filter(Boolean) : params.quick ? w.lastTo : []));
  const [ring] = useState(() => new Animated.Value(0));
  const [pulse] = useState(() => new Animated.Value(0));

  // The people, with their departments for the group chips (the manager and the PA on their own line).
  const departments = useMemo(() => {
    const map = new Map<string, number[]>();
    for (const p of dash?.team ?? []) {
      if (p.you || p.role === 'MD') continue;
      for (const d of p.departments ?? []) map.set(d, [...(map.get(d) ?? []), p.id]);
    }
    return [...map.entries()];
  }, [dash?.team]);
  const management = (dash?.team ?? []).filter((p) => !p.you && (p.role === 'MG' || p.role === 'PA')).map((p) => p.id);
  // Before the new app is on this phone there is no line: the team is shown as a preview, nothing can be held.
  const preview = !w.supported;
  const people: WalkiePerson[] = useMemo(() => (preview
    ? (dash?.team ?? []).filter((p) => !p.you).map((p) => ({ id: p.id, name: p.name, title: p.title, role: p.role, status: 'available' as const, quiet_until: '', talking: false }))
    : w.people), [preview, dash?.team, w.people]);
  const everyone = people.filter((p) => p.status !== 'off').map((p) => p.id);

  // The people picked are always the speaker's choice. When a talk reaches this phone, its people are picked for
  // the reply, but they can be changed at any time: a press to the same people replies in that talk, a press to
  // different people starts a new talk with them.
  const inTalk = !!w.talk;
  const others = useMemo(() => w.members.filter((id) => id !== w.you), [w.members, w.you]);
  const [lastTalk, setLastTalk] = useState('');
  if (w.talk && w.talk !== lastTalk) {
    // A new talk reached this phone: its people are picked (React's way to adjust state when a value changes).
    setLastTalk(w.talk);
    if (others.length) setSelected(others);
  }
  const to = selected;
  const sameAsTalk = inTalk && others.length > 0 && to.length === others.length && others.every((id) => to.includes(id));
  const names = (ids: number[]) => ids.map((id) => people.find((p) => p.id === id)?.name ?? '').filter(Boolean);

  // The button's glow follows the voice while talking; the other side's avatar pulses while they talk.
  useEffect(() => {
    Animated.timing(ring, { toValue: w.state === 'talking' ? 0.3 + w.level * 0.7 : 0, duration: 90, useNativeDriver: true }).start();
  }, [w.level, w.state, ring]);
  useEffect(() => {
    if (w.state !== 'listening') {
      pulse.setValue(0);
      return undefined;
    }
    const loop = Animated.loop(Animated.sequence([
      Animated.timing(pulse, { toValue: 1, duration: 500, useNativeDriver: true }),
      Animated.timing(pulse, { toValue: 0.2, duration: 500, useNativeDriver: true }),
    ]));
    loop.start();
    return () => loop.stop();
  }, [w.state, pulse]);

  const holdRef = useRef(false);
  const down = () => {
    if (holdRef.current) return;
    holdRef.current = true;
    if (sameAsTalk) w.reply();
    else w.talkStart(to);
  };
  const up = () => {
    if (!holdRef.current) return;
    holdRef.current = false;
    w.talkEnd();
  };
  const toggle = (id: number) => setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));
  const pick = (ids: number[]) => setSelected(ids);

  const canTalk = w.enabled && w.supported && to.length > 0 && w.state !== 'listening' && w.state !== 'busy';
  const status =
    preview ? 'Preview · live talk comes with the new app'
    : !w.enabled ? 'Switched off in Settings'
    : w.state === 'listening' ? `${w.incomingFrom || 'Someone'} is talking…`
    : w.state === 'talking' ? `Talking · ${names(to).join(', ') || 'them'}`
    : w.state === 'connecting' ? 'Connecting…'
    : w.state === 'busy' ? w.note || 'Wait…'
    : !w.connected ? 'Connecting…'
    : to.length ? `Ready · ${names(to).join(', ')}`
    : 'Pick who to talk to';
  const buttonLabel = w.state === 'listening' ? 'LISTENING' : sameAsTalk ? 'HOLD TO\nREPLY' : 'HOLD TO\nTALK';
  const buttonColor = w.state === 'listening' ? colors.success : w.state === 'talking' ? colors.danger : canTalk ? colors.accent : colors.faint;

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }}>
      <View style={s.top}>
        <IconButton icon="chevron-back" label="Back" onPress={() => router.back()} />
        <Txt variant="title" style={{ flex: 1, textAlign: 'center' }}>Walkie-talkie</Txt>
        <Pressable onPress={() => router.push('/settings?section=walkie')} hitSlop={10} style={{ width: 40, alignItems: 'center' }}>
          <Icon name="settings-outline" size={20} color={colors.inkSoft} />
        </Pressable>
      </View>

      <ScrollView contentContainerStyle={s.body} keyboardShouldPersistTaps="handled">
        <View style={s.toRow}>
          <Txt variant="label">To</Txt>
          <Txt variant="heading" numberOfLines={2} style={{ flex: 1, marginLeft: 10 }}>{names(to).join(', ') || 'nobody yet'}</Txt>
          {selected.length ? (
            <Pressable onPress={() => setSelected([])} hitSlop={8} accessibilityLabel="Clear"><Icon name="close-circle" size={20} color={colors.faint} /></Pressable>
          ) : null}
        </View>
        {inTalk && others.length ? (
          <Txt variant="caption" style={{ marginTop: 6, marginLeft: 4 }}>
            {sameAsTalk ? `Replying in the talk with ${names(others).join(', ')}. Pick someone else to start a new talk.`
              : `This starts a new talk; the one with ${names(others).join(', ')} stays open for a while.`}
          </Txt>
        ) : null}

        <View style={s.chips}>
          <Chip label="Everyone" icon="people-outline" selected={everyone.length > 0 && everyone.every((id) => selected.includes(id))} onPress={() => pick(everyone)} />
          {management.length ? <Chip label="Management" icon="briefcase-outline" selected={management.every((id) => selected.includes(id))} onPress={() => pick(management)} /> : null}
          {departments.map(([name, ids]) => (
            <Chip key={name} label={name} icon="git-network-outline" selected={ids.every((id) => selected.includes(id))} onPress={() => pick(ids)} />
          ))}
        </View>

        <View style={s.people}>
          {people.map((p) => {
            const st = preview ? { label: 'On the team', color: colors.muted } : STATUS[p.status];
            const on = to.includes(p.id);
            const speaking = w.state === 'listening' && p.name === w.incomingFrom;
            return (
              <Pressable key={p.id} onPress={() => toggle(p.id)} disabled={p.status === 'off'} style={({ pressed }) => [s.person, on && s.personOn, pressed && { opacity: 0.7 }]}>
                <Animated.View style={[s.avatar, { backgroundColor: on ? colors.accent : colors.surfaceAlt }, speaking && { transform: [{ scale: pulse.interpolate({ inputRange: [0, 1], outputRange: [1, 1.12] }) }] }]}>
                  <Txt variant="heading" style={{ color: on ? colors.white : colors.text }}>{p.name.slice(0, 1).toUpperCase()}</Txt>
                </Animated.View>
                <View style={{ flex: 1, marginLeft: 12 }}>
                  <Txt variant="heading" style={{ fontSize: 15 }}>{p.name}</Txt>
                  <Txt variant="caption">{p.title}</Txt>
                </View>
                <View style={s.status}>
                  <View style={[s.dot, { backgroundColor: st.color }]} />
                  <Txt variant="small" style={{ color: st.color }}>
                    {speaking ? 'Talking' : p.talking ? 'In a talk' : st.label}{p.status === 'quiet' && p.quiet_until ? ` till ${timeOf(p.quiet_until)}` : ''}
                  </Txt>
                </View>
              </Pressable>
            );
          })}
          {!people.length ? <Txt variant="small" style={{ textAlign: 'center', marginTop: 20 }}>{w.connected || preview ? 'Nobody else is on the team yet.' : status}</Txt> : null}
        </View>
      </ScrollView>

      {/* the button: fills the thumb zone; press and hold */}
      <View style={s.bottom}>
        <Txt variant="small" style={{ textAlign: 'center', marginBottom: 10, color: w.state === 'listening' ? colors.success : colors.inkSoft }}>{status}</Txt>
        <View style={{ alignItems: 'center' }}>
          <Animated.View pointerEvents="none" style={[s.glow, { backgroundColor: buttonColor, opacity: ring, transform: [{ scale: ring.interpolate({ inputRange: [0, 1], outputRange: [1, 1.35] }) }] }]} />
          <Pressable
            onPressIn={canTalk ? down : undefined}
            onPressOut={up}
            disabled={!canTalk}
            style={({ pressed }) => [s.button, { backgroundColor: buttonColor }, pressed && canTalk && { transform: [{ scale: 0.96 }] }]}
          >
            <Icon name={w.state === 'listening' ? 'volume-high-outline' : 'mic-outline'} size={34} color={colors.white} />
            <Txt variant="heading" style={{ color: colors.white, textAlign: 'center', marginTop: 6, letterSpacing: 1 }}>{buttonLabel}</Txt>
          </Pressable>
        </View>
        {w.note && w.state !== 'busy' ? <Txt variant="caption" style={{ textAlign: 'center', marginTop: 10 }}>{w.note}</Txt> : null}
        {sameAsTalk && others.length > 1 ? (
          <Txt variant="caption" style={{ textAlign: 'center', marginTop: 8 }}>Your reply goes to everyone in this talk.</Txt>
        ) : null}
      </View>
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  top: { flexDirection: 'row', alignItems: 'center', paddingHorizontal: 10, paddingTop: 2 },
  body: { paddingHorizontal: 20, paddingTop: 6, paddingBottom: 20 },
  toRow: { flexDirection: 'row', alignItems: 'center', backgroundColor: colors.surface, borderRadius: radius.md, borderWidth: 1, borderColor: colors.line, paddingHorizontal: 14, paddingVertical: 10 },
  chips: { flexDirection: 'row', flexWrap: 'wrap', gap: 6, marginTop: 12 },
  people: { marginTop: 12 },
  person: { flexDirection: 'row', alignItems: 'center', backgroundColor: colors.surface, borderRadius: radius.md, borderWidth: 1, borderColor: colors.line, paddingHorizontal: 12, paddingVertical: 10, marginBottom: 8 },
  personOn: { borderColor: colors.accent, backgroundColor: colors.accentSoft },
  avatar: { width: 38, height: 38, borderRadius: 19, alignItems: 'center', justifyContent: 'center' },
  status: { flexDirection: 'row', alignItems: 'center', marginLeft: 8 },
  dot: { width: 8, height: 8, borderRadius: 4, marginRight: 6 },
  bottom: { paddingHorizontal: 20, paddingBottom: 18, paddingTop: 8, borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: colors.line, backgroundColor: colors.bg },
  glow: { position: 'absolute', width: 170, height: 170, borderRadius: 85 },
  button: { width: 170, height: 170, borderRadius: 85, alignItems: 'center', justifyContent: 'center', ...shadow, shadowOpacity: 0.25, elevation: 10 },
});
