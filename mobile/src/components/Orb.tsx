import { LinearGradient } from 'expo-linear-gradient';
import React, { useEffect, useMemo, useState } from 'react';
import { Animated, Easing, Pressable, StyleSheet, View } from 'react-native';
import Svg, { Path } from 'react-native-svg';

import type { OrbState } from '@/lib/conversation';
import { colors, gradients } from '@/lib/theme';

import { Icon } from './ui';

const timing = (v: Animated.Value, to: number, ms: number, easing = Easing.inOut(Easing.quad)) =>
  Animated.timing(v, { toValue: to, duration: ms, easing, useNativeDriver: true });

/** A sine wave as an SVG path, two widths long so it can slide one width and loop without a seam. */
export function wavePath(width: number, height: number, amplitude: number, wavelength: number, baseline: number): string {
  const total = width * 2;
  const step = Math.max(4, Math.round(wavelength / 12));
  let d = `M0,${baseline.toFixed(1)}`;
  for (let x = 0; x <= total; x += step) {
    const y = baseline + Math.sin((x / wavelength) * Math.PI * 2) * amplitude;
    d += ` L${x},${y.toFixed(1)}`;
  }
  d += ` L${total},${height} L0,${height} Z`;
  return d;
}

/** Layered waves that slide sideways (native driver) inside a clipped area; vertical offset follows `rise`. */
export function Waves({
  width,
  height,
  rise,
  speed = 1,
  color = 'rgba(255,255,255,0.28)',
  layers = 3,
  baseline = 0.6,
}: {
  width: number;
  height: number;
  /** Animated vertical offset in px (negative = higher water). */
  rise: Animated.AnimatedNode;
  speed?: number;
  color?: string;
  layers?: number;
  baseline?: number;
}) {
  const [slide] = useState(() => [new Animated.Value(0), new Animated.Value(0), new Animated.Value(0)]);
  useEffect(() => {
    const loops = slide.slice(0, layers).map((v, i) =>
      Animated.loop(timing(v, 1, (7000 - i * 1800) / speed, Easing.linear)),
    );
    loops.forEach((l) => l.start());
    return () => loops.forEach((l) => l.stop());
  }, [slide, speed, layers]);
  const paths = useMemo(() => {
    const divs = [1, 2, 3];
    return divs.slice(0, layers).map((div, i) =>
      wavePath(width, height, height * (0.045 + i * 0.02), width / div, height * (baseline + i * 0.045)),
    );
  }, [width, height, layers, baseline]);
  return (
    <View style={[StyleSheet.absoluteFill, { overflow: 'hidden' }]} pointerEvents="none">
      {paths.map((d, i) => (
        <Animated.View
          key={i}
          style={{
            position: 'absolute',
            left: 0,
            top: 0,
            width: width * 2,
            height,
            transform: [
              { translateX: slide[i].interpolate({ inputRange: [0, 1], outputRange: i % 2 ? [-width, 0] : [0, -width] }) },
              { translateY: rise },
            ],
          }}
        >
          <Svg width={width * 2} height={height}>
            <Path d={d} fill={color} opacity={1 - i * 0.22} />
          </Svg>
        </Animated.View>
      ))}
    </View>
  );
}

/**
 * The JARVIS orb: a glass sphere with waves moving inside it.
 * idle = slow waves, breathing · listening = rings, waves rise with his voice · thinking = quick waves and a
 * sheen · speaking = waves bob with the voice.
 */
export function Orb({
  state,
  size = 190,
  level,
  onPress,
}: {
  state: OrbState;
  size?: number;
  /** 0..1 loudness while listening (from the recogniser). */
  level?: Animated.Value;
  onPress: () => void;
}) {
  const [breath] = useState(() => new Animated.Value(1));
  const [ringA] = useState(() => new Animated.Value(0));
  const [ringB] = useState(() => new Animated.Value(0));
  const [sheen] = useState(() => new Animated.Value(0));
  const [bob] = useState(() => new Animated.Value(0));
  const [glow] = useState(() => new Animated.Value(0.45));
  const core = Math.round(size * 0.66);

  useEffect(() => {
    breath.setValue(1);
    ringA.setValue(0);
    ringB.setValue(0);
    const loops: Animated.CompositeAnimation[] = [];
    const bobbing = (ms: number) => Animated.loop(Animated.sequence([timing(bob, 1, ms), timing(bob, 0, ms)]));
    if (state === 'idle') {
      loops.push(Animated.loop(Animated.sequence([timing(breath, 1.035, 2600), timing(breath, 1, 2600)])));
      loops.push(bobbing(2200));
      loops.push(Animated.loop(timing(sheen, 1, 16000, Easing.linear)));
      loops.push(Animated.loop(Animated.sequence([timing(glow, 0.7, 2600), timing(glow, 0.4, 2600)])));
    } else if (state === 'listening') {
      loops.push(Animated.loop(timing(ringA, 1, 1600, Easing.out(Easing.quad))));
      loops.push(Animated.loop(Animated.sequence([Animated.delay(800), timing(ringB, 1, 1600, Easing.out(Easing.quad))])));
      loops.push(bobbing(900));
      loops.push(Animated.loop(timing(sheen, 1, 8000, Easing.linear)));
      loops.push(Animated.loop(Animated.sequence([timing(glow, 0.95, 900), timing(glow, 0.6, 900)])));
    } else if (state === 'thinking') {
      loops.push(bobbing(500));
      loops.push(Animated.loop(timing(sheen, 1, 1400, Easing.linear)));
      loops.push(Animated.loop(Animated.sequence([timing(breath, 1.05, 600), timing(breath, 0.985, 600)])));
      loops.push(Animated.loop(Animated.sequence([timing(glow, 0.9, 600), timing(glow, 0.5, 600)])));
    } else {
      loops.push(bobbing(360));
      loops.push(Animated.loop(timing(sheen, 1, 5000, Easing.linear)));
      loops.push(Animated.loop(Animated.sequence([timing(glow, 0.95, 360), timing(glow, 0.65, 360)])));
    }
    loops.forEach((l) => l.start());
    return () => loops.forEach((l) => l.stop());
  }, [state, breath, ringA, ringB, sheen, bob, glow]);

  const rotate = sheen.interpolate({ inputRange: [0, 1], outputRange: ['0deg', '360deg'] });
  const ringStyle = (v: Animated.Value) => ({
    transform: [{ scale: v.interpolate({ inputRange: [0, 1], outputRange: [1, 1.55] }) }],
    opacity: v.interpolate({ inputRange: [0, 0.5, 1], outputRange: [0.45, 0.18, 0] }),
  });
  // Water level: a gentle bob, plus his voice pushing it up while listening.
  const bobPx = bob.interpolate({ inputRange: [0, 1], outputRange: [0, -core * (state === 'speaking' ? 0.08 : state === 'thinking' ? 0.06 : 0.03)] });
  const voicePx = level ? level.interpolate({ inputRange: [0, 1], outputRange: [0, -core * 0.3] }) : new Animated.Value(0);
  const rise = state === 'listening' ? Animated.add(bobPx, voicePx) : bobPx;
  const palette = state === 'listening' ? gradients.orbListening : state === 'speaking' ? gradients.orbSpeaking : gradients.orb;
  const icon = state === 'listening' ? 'mic' : state === 'thinking' ? 'ellipsis-horizontal' : state === 'speaking' ? 'volume-high' : '';
  const waveSpeed = state === 'thinking' ? 2.6 : state === 'speaking' ? 2 : state === 'listening' ? 1.4 : 1;

  return (
    <Pressable onPress={onPress} accessibilityLabel="Talk to JARVIS" style={[s.wrap, { width: size, height: size }]}>
      <Animated.View style={[s.glow, { width: size, height: size, borderRadius: size / 2, opacity: glow }]} />
      {state === 'listening' ? (
        <>
          <Animated.View style={[s.ring, { width: core, height: core, borderRadius: core / 2 }, ringStyle(ringA)]} />
          <Animated.View style={[s.ring, { width: core, height: core, borderRadius: core / 2 }, ringStyle(ringB)]} />
        </>
      ) : null}
      <Animated.View style={[s.core, { width: core, height: core, borderRadius: core / 2, transform: [{ scale: breath }] }]}>
        <LinearGradient colors={[...palette]} start={{ x: 0.2, y: 0 }} end={{ x: 0.9, y: 1 }} style={StyleSheet.absoluteFill} />
        <Waves width={core} height={core} rise={rise} speed={waveSpeed} />
        <Animated.View style={[StyleSheet.absoluteFill, { transform: [{ rotate }] }]} pointerEvents="none">
          <LinearGradient colors={[...gradients.sheen]} start={{ x: 0, y: 0.5 }} end={{ x: 1, y: 0.5 }} style={s.sheen} />
        </Animated.View>
        <View style={[s.highlight, { width: core * 0.42, height: core * 0.24, borderRadius: core * 0.2, top: core * 0.09, left: core * 0.17 }]} pointerEvents="none" />
        {icon ? (
          <View style={s.iconWrap}>
            <Icon name={icon} size={Math.round(core * 0.24)} color={colors.white} />
          </View>
        ) : null}
      </Animated.View>
    </Pressable>
  );
}

/** The small floating orb in the corner of the phone's home screen, waves and all. */
export function OrbButton({ state, size = 64, onPress }: { state: OrbState; size?: number; onPress: () => void }) {
  const [pulse] = useState(() => new Animated.Value(1));
  const [bob] = useState(() => new Animated.Value(0));
  useEffect(() => {
    const quick = state !== 'idle';
    const loops = [
      Animated.loop(Animated.sequence([timing(pulse, quick ? 1.14 : 1.06, quick ? 520 : 1900), timing(pulse, 1, quick ? 520 : 1900)])),
      Animated.loop(Animated.sequence([timing(bob, 1, quick ? 600 : 2000), timing(bob, 0, quick ? 600 : 2000)])),
    ];
    loops.forEach((l) => l.start());
    return () => loops.forEach((l) => l.stop());
  }, [state, pulse, bob]);
  const palette = state === 'listening' ? gradients.orbListening : state === 'speaking' ? gradients.orbSpeaking : gradients.orb;
  const rise = bob.interpolate({ inputRange: [0, 1], outputRange: [0, -size * 0.06] });
  const icon = state === 'listening' ? 'mic' : state === 'thinking' ? 'ellipsis-horizontal' : state === 'speaking' ? 'volume-high' : '';
  return (
    <Pressable onPress={onPress} accessibilityLabel="Talk to JARVIS" style={{ width: size + 18, height: size + 18, alignItems: 'center', justifyContent: 'center' }}>
      <Animated.View style={[s.fabGlow, { width: size + 18, height: size + 18, borderRadius: (size + 18) / 2, transform: [{ scale: pulse }] }]} />
      <View style={[s.fab, { width: size, height: size, borderRadius: size / 2 }]}>
        <LinearGradient colors={[...palette]} start={{ x: 0.2, y: 0 }} end={{ x: 0.9, y: 1 }} style={StyleSheet.absoluteFill} />
        <Waves width={size} height={size} rise={rise} speed={state === 'idle' ? 1 : 2} layers={2} baseline={0.58} />
        <View style={[s.highlight, { width: size * 0.44, height: size * 0.22, borderRadius: size * 0.2, top: size * 0.09, left: size * 0.16 }]} pointerEvents="none" />
        {icon ? (
          <View style={s.iconWrap}>
            <Icon name={icon} size={Math.round(size * 0.36)} color={colors.white} />
          </View>
        ) : null}
      </View>
    </Pressable>
  );
}

const s = StyleSheet.create({
  wrap: { alignItems: 'center', justifyContent: 'center' },
  glow: { position: 'absolute', backgroundColor: colors.accentSoft },
  ring: { position: 'absolute', borderWidth: 2, borderColor: colors.accentGlow },
  core: {
    overflow: 'hidden',
    alignItems: 'center',
    justifyContent: 'center',
    shadowColor: colors.accentDeep,
    shadowOpacity: 0.35,
    shadowRadius: 22,
    shadowOffset: { width: 0, height: 10 },
    elevation: 10,
  },
  sheen: { position: 'absolute', left: '-20%', right: '-20%', top: '36%', height: '22%' },
  highlight: { position: 'absolute', backgroundColor: 'rgba(255,255,255,0.26)' },
  iconWrap: { position: 'absolute', alignItems: 'center', justifyContent: 'center', opacity: 0.95 },
  fabGlow: { position: 'absolute', backgroundColor: colors.accentSoft },
  fab: {
    overflow: 'hidden',
    alignItems: 'center',
    justifyContent: 'center',
    shadowColor: colors.accentDeep,
    shadowOpacity: 0.35,
    shadowRadius: 16,
    shadowOffset: { width: 0, height: 8 },
    elevation: 8,
  },
});
