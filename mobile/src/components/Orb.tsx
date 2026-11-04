import React, { useEffect, useState } from 'react';
import { Animated, Easing, Pressable, StyleSheet, View } from 'react-native';

import type { OrbState } from '@/lib/conversation';
import { colors } from '@/lib/theme';

import { Icon } from './ui';

const timing = (v: Animated.Value, to: number, ms: number, easing = Easing.inOut(Easing.quad)) =>
  Animated.timing(v, { toValue: to, duration: ms, easing, useNativeDriver: true });

/** The JARVIS orb: breathes when idle, pulses gold when listening, spins when thinking, ripples when speaking. */
export function Orb({ state, size = 190, onPress }: { state: OrbState; size?: number; onPress: () => void }) {
  const [breath] = useState(() => new Animated.Value(1));
  const [ring] = useState(() => new Animated.Value(0));
  const [spin] = useState(() => new Animated.Value(0));
  const [wave] = useState(() => new Animated.Value(0));

  useEffect(() => {
    breath.setValue(1);
    ring.setValue(0);
    spin.setValue(0);
    wave.setValue(0);
    let loop: Animated.CompositeAnimation;
    if (state === 'idle') loop = Animated.loop(Animated.sequence([timing(breath, 1.035, 2300), timing(breath, 1, 2300)]));
    else if (state === 'listening') loop = Animated.loop(timing(ring, 1, 1300, Easing.out(Easing.quad)));
    else if (state === 'thinking') loop = Animated.loop(timing(spin, 1, 1100, Easing.linear));
    else loop = Animated.loop(Animated.sequence([timing(wave, 1, 420), timing(wave, 0, 420)]));
    loop.start();
    return () => loop.stop();
  }, [state, breath, ring, spin, wave]);

  const ringScale = ring.interpolate({ inputRange: [0, 1], outputRange: [1, 1.5] });
  const ringOpacity = ring.interpolate({ inputRange: [0, 0.6, 1], outputRange: [0.5, 0.18, 0] });
  const rotate = spin.interpolate({ inputRange: [0, 1], outputRange: ['0deg', '360deg'] });
  const waveScale = wave.interpolate({ inputRange: [0, 1], outputRange: [1, 1.09] });
  const core = size * 0.62;
  const icon = state === 'listening' ? 'mic' : state === 'thinking' ? 'ellipsis-horizontal' : state === 'speaking' ? 'volume-high' : 'sparkles';

  return (
    <Pressable onPress={onPress} accessibilityLabel="Talk to JARVIS" style={[s.wrap, { width: size, height: size }]}>
      {/* soft halo */}
      <View style={[s.halo, { width: size, height: size, borderRadius: size / 2 }]} />
      {state === 'listening' ? (
        <Animated.View style={[s.ring, { width: core, height: core, borderRadius: core / 2, transform: [{ scale: ringScale }], opacity: ringOpacity }]} />
      ) : null}
      {state === 'thinking' ? (
        <Animated.View style={[s.spinner, { width: core + 18, height: core + 18, borderRadius: (core + 18) / 2, transform: [{ rotate }] }]} />
      ) : null}
      <Animated.View
        style={[
          s.core,
          { width: core, height: core, borderRadius: core / 2, transform: [{ scale: state === 'speaking' ? waveScale : breath }] },
          state === 'listening' && { backgroundColor: colors.gold },
        ]}
      >
        <View style={[s.glow, { width: core * 0.7, height: core * 0.7, borderRadius: core * 0.35 }]} />
        <Icon name={icon} size={core * 0.3} color={colors.white} />
      </Animated.View>
    </Pressable>
  );
}

const s = StyleSheet.create({
  wrap: { alignItems: 'center', justifyContent: 'center' },
  halo: { position: 'absolute', backgroundColor: colors.goldSoft, opacity: 0.55 },
  ring: { position: 'absolute', backgroundColor: colors.gold },
  spinner: { position: 'absolute', borderWidth: 3, borderColor: colors.gold, borderTopColor: 'transparent', borderLeftColor: 'transparent' },
  core: {
    backgroundColor: colors.ink,
    alignItems: 'center',
    justifyContent: 'center',
    shadowColor: colors.ink,
    shadowOpacity: 0.25,
    shadowRadius: 18,
    shadowOffset: { width: 0, height: 8 },
    elevation: 8,
  },
  glow: { position: 'absolute', top: '8%', left: '12%', backgroundColor: colors.gold, opacity: 0.18 },
});
