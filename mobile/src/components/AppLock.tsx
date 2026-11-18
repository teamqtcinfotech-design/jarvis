/**
 * The locked screen: shown over everything when JARVIS comes back after more than five minutes away (and at a cold
 * start), until the fingerprint / face / PIN is given. The person's data never shows behind it.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AppState, type AppStateStatus, Pressable, StyleSheet, View } from 'react-native';

import { lockEnabled, markLocked, recentlyUnlocked, unlock } from '@/lib/appLock';
import { useSession } from '@/lib/session';
import { colors, fonts } from '@/lib/theme';

import { Icon, Txt } from './ui';

export function AppLock() {
  const { user } = useSession();
  const [locked, setLocked] = useState(false);
  const [asking, setAsking] = useState(false);
  const wentBackAt = useRef(0);

  const ask = useCallback(async () => {
    if (asking) return;
    setAsking(true);
    const ok = await unlock('Unlock JARVIS');
    setAsking(false);
    if (ok) setLocked(false);
  }, [asking]);

  // A cold start, and every return to the front after a long enough time away.
  useEffect(() => {
    if (!user) return undefined; // signed out: nothing to lock (the cover is not drawn without a user)
    let alive = true;
    (async () => {
      if (!(await lockEnabled())) return;
      if (!recentlyUnlocked() && alive) {
        setLocked(true);
        ask().catch(() => undefined);
      }
    })();
    const sub = AppState.addEventListener('change', async (s: AppStateStatus) => {
      if (s === 'background' || s === 'inactive') {
        if (!wentBackAt.current) wentBackAt.current = Date.now();
        return;
      }
      if (s === 'active') {
        const away = wentBackAt.current ? Date.now() - wentBackAt.current : 0;
        wentBackAt.current = 0;
        if (!(await lockEnabled())) return;
        if (away >= 5 * 60_000 || !recentlyUnlocked()) {
          markLocked();
          setLocked(true);
          ask().catch(() => undefined);
        }
      }
    });
    return () => {
      alive = false;
      sub.remove();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- ask is stable enough; re-arming on it would re-prompt
  }, [user]);

  if (!user || !locked) return null;
  return (
    <View style={s.cover} pointerEvents="auto">
      <View style={s.crest}>
        <Icon name="finger-print-outline" size={40} color={colors.gold} />
      </View>
      <Txt style={s.brand}>JARVIS</Txt>
      <Txt variant="small" style={{ color: colors.muted, marginTop: 6 }}>Locked</Txt>
      <Pressable onPress={ask} style={({ pressed }) => [s.button, pressed && { opacity: 0.8 }]}>
        <Icon name="lock-open-outline" size={18} color={colors.white} />
        <Txt variant="heading" style={{ color: colors.white, marginLeft: 8 }}>{asking ? 'Checking…' : 'Unlock'}</Txt>
      </Pressable>
    </View>
  );
}

const s = StyleSheet.create({
  cover: { position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, backgroundColor: colors.bg, alignItems: 'center', justifyContent: 'center', zIndex: 1000, elevation: 30 },
  crest: { width: 88, height: 88, borderRadius: 44, borderWidth: 1, borderColor: colors.gold, backgroundColor: colors.goldSoft, alignItems: 'center', justifyContent: 'center' },
  brand: { fontFamily: fonts.serif, fontSize: 28, fontWeight: '700', lineHeight: 40, letterSpacing: 3, paddingLeft: 4, color: colors.ink, marginTop: 16 },
  button: { flexDirection: 'row', alignItems: 'center', backgroundColor: colors.accent, borderRadius: 24, paddingHorizontal: 22, paddingVertical: 12, marginTop: 28 },
});
