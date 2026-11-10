import { LinearGradient } from 'expo-linear-gradient';
import React, { useEffect, useMemo, useState } from 'react';
import { Animated, Easing, Pressable, StyleSheet, View } from 'react-native';
import Svg, { Circle, Defs, Ellipse, Path, RadialGradient, Stop } from 'react-native-svg';

import type { OrbState } from '@/lib/conversation';
import { colors, gradients } from '@/lib/theme';

import { Icon } from './ui';

const timing = (v: Animated.Value, to: number, ms: number, easing = Easing.inOut(Easing.quad)) =>
  Animated.timing(v, { toValue: to, duration: ms, easing, useNativeDriver: true });

/** A sine wave as an SVG path, two widths long so it can slide one width and loop without a seam. */
export function wavePath(width: number, bottom: number, amplitude: number, wavelength: number, baseline: number): string {
  const total = width * 2;
  const step = Math.max(4, Math.round(wavelength / 12));
  let d = `M0,${baseline.toFixed(1)}`;
  for (let x = 0; x <= total; x += step) {
    const y = baseline + Math.sin((x / wavelength) * Math.PI * 2) * amplitude;
    d += ` L${x},${y.toFixed(1)}`;
  }
  d += ` L${total},${bottom} L0,${bottom} Z`;
  return d;
}

/**
 * Layered waves that slide sideways (native driver) inside a clipped area; vertical offset follows `rise`.
 * The water is drawn well past the bottom, so when it rises (his voice lifts it) no bare strip shows underneath.
 */
export function Waves({
  width,
  height,
  rise,
  speed = 1,
  color = 'rgba(255,255,255,0.17)',
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
  const deep = Math.round(height * 1.7);
  const paths = useMemo(() => {
    const divs = [1, 2, 3];
    return divs.slice(0, layers).map((div, i) =>
      wavePath(width, deep, height * (0.04 + i * 0.018), width / div, height * (baseline + i * 0.05)),
    );
  }, [width, height, deep, layers, baseline]);
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
            height: deep,
            transform: [
              { translateX: slide[i].interpolate({ inputRange: [0, 1], outputRange: i % 2 ? [-width, 0] : [0, -width] }) },
              { translateY: rise },
            ],
          }}
        >
          <Svg width={width * 2} height={deep}>
            <Path d={d} fill={color} />
          </Svg>
        </Animated.View>
      ))}
    </View>
  );
}

/** A soft halo that fades to nothing at its edge (a real glow, not a flat disc). */
function Halo({ size, color }: { size: number; color: string }) {
  return (
    <Svg width={size} height={size}>
      <Defs>
        <RadialGradient id="orbHalo" cx="50%" cy="50%" r="50%">
          <Stop offset="0.5" stopColor={color} stopOpacity={0.6} />
          <Stop offset="0.78" stopColor={color} stopOpacity={0.2} />
          <Stop offset="1" stopColor={color} stopOpacity={0} />
        </RadialGradient>
      </Defs>
      <Circle cx={size / 2} cy={size / 2} r={size / 2} fill="url(#orbHalo)" />
    </Svg>
  );
}

/** A thin bright arc that circles the sphere: slow at rest, quick while JARVIS is thinking. */
function OrbitArc({ size }: { size: number }) {
  const r = size / 2 - 2;
  const around = 2 * Math.PI * r;
  return (
    <Svg width={size} height={size}>
      <Circle cx={size / 2} cy={size / 2} r={r} stroke="rgba(142,142,242,0.28)" strokeWidth={1} fill="none" />
      <Circle
        cx={size / 2} cy={size / 2} r={r} stroke="#B9B6FF" strokeWidth={2} strokeLinecap="round" fill="none"
        strokeDasharray={`${around * 0.16} ${around}`}
      />
    </Svg>
  );
}

/**
 * What makes a flat disc read as a glass ball: the rim falls into shadow all the way round, and one soft shine
 * sits high on the left, fading out (never a hard-edged shape).
 */
function Glass({ size }: { size: number }) {
  const gx = size * 0.35;
  const gy = size * 0.23;
  return (
    <Svg width={size} height={size} style={StyleSheet.absoluteFill}>
      <Defs>
        <RadialGradient id="orbDepth" cx="48%" cy="40%" r="64%">
          <Stop offset="0.5" stopColor="#1A1466" stopOpacity={0} />
          <Stop offset="0.85" stopColor="#1A1466" stopOpacity={0.22} />
          <Stop offset="1" stopColor="#120C4A" stopOpacity={0.5} />
        </RadialGradient>
        <RadialGradient id="orbShine" cx="50%" cy="50%" r="50%">
          <Stop offset="0" stopColor="#FFFFFF" stopOpacity={0.62} />
          <Stop offset="0.6" stopColor="#FFFFFF" stopOpacity={0.14} />
          <Stop offset="1" stopColor="#FFFFFF" stopOpacity={0} />
        </RadialGradient>
      </Defs>
      <Circle cx={size / 2} cy={size / 2} r={size / 2} fill="url(#orbDepth)" />
      <Ellipse cx={gx} cy={gy} rx={size * 0.27} ry={size * 0.15} fill="url(#orbShine)" transform={`rotate(-26 ${gx} ${gy})`} />
    </Svg>
  );
}

const HALO: Record<OrbState, string> = { idle: '#8E8EF2', listening: '#A78BFA', thinking: '#8E8EF2', speaking: '#7DB7FF' };

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
  shift,
}: {
  state: OrbState;
  size?: number;
  /** 0..1 loudness while listening (from the recogniser). */
  level?: Animated.Value;
  onPress: () => void;
  /** The sphere slowly drifts between colours (indigo, violet-pink, sky blue): it looks alive. */
  shift?: boolean;
}) {
  // Two colour washes that fade in and out at different speeds, so the mix never quite repeats.
  const [washA] = useState(() => new Animated.Value(0));
  const [washB] = useState(() => new Animated.Value(0));
  useEffect(() => {
    if (!shift) return undefined;
    const loops = [
      Animated.loop(Animated.sequence([timing(washA, 1, 3600), timing(washA, 0, 3600)])),
      Animated.loop(Animated.sequence([Animated.delay(1700), timing(washB, 0.9, 5200), timing(washB, 0, 5200)])),
    ];
    loops.forEach((l) => l.start());
    return () => loops.forEach((l) => l.stop());
  }, [shift, washA, washB]);
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

  const orbit = core + 14; // the arc sits just outside the sphere
  return (
    <Pressable
      onPress={onPress}
      accessibilityLabel="Talk to JARVIS"
      style={({ pressed }) => [s.wrap, { width: size, height: size }, pressed && { transform: [{ scale: 0.95 }] }]}
    >
      <Animated.View style={[s.layer, { width: size, height: size, opacity: glow, transform: [{ scale: breath }] }]} pointerEvents="none">
        <Halo size={size} color={HALO[state]} />
      </Animated.View>
      {state === 'listening' ? (
        <>
          <Animated.View style={[s.ring, { width: core, height: core, borderRadius: core / 2 }, ringStyle(ringA)]} />
          <Animated.View style={[s.ring, { width: core, height: core, borderRadius: core / 2 }, ringStyle(ringB)]} />
        </>
      ) : (
        <Animated.View style={[s.layer, { width: orbit, height: orbit, left: (size - orbit) / 2, top: (size - orbit) / 2, transform: [{ rotate }] }]} pointerEvents="none">
          <OrbitArc size={orbit} />
        </Animated.View>
      )}
      <Animated.View style={[s.core, { width: core, height: core, borderRadius: core / 2, transform: [{ scale: breath }] }]}>
        <LinearGradient colors={[...palette]} start={{ x: 0.2, y: 0 }} end={{ x: 0.9, y: 1 }} style={StyleSheet.absoluteFill} />
        {shift ? (
          <>
            <Animated.View style={[StyleSheet.absoluteFill, { opacity: washA }]} pointerEvents="none">
              <LinearGradient colors={['#FF9BE0', '#B45CE6', '#5527B0']} start={{ x: 0.2, y: 0 }} end={{ x: 0.9, y: 1 }} style={StyleSheet.absoluteFill} />
            </Animated.View>
            <Animated.View style={[StyleSheet.absoluteFill, { opacity: washB }]} pointerEvents="none">
              <LinearGradient colors={['#8DF0FF', '#3C9CEA', '#27379E']} start={{ x: 0.2, y: 0 }} end={{ x: 0.9, y: 1 }} style={StyleSheet.absoluteFill} />
            </Animated.View>
          </>
        ) : null}
        <Waves width={core} height={core} rise={rise} speed={waveSpeed} />
        {/* a slow drifting light: wide, faint and soft-edged, so it never reads as a stripe across the ball */}
        <Animated.View style={[StyleSheet.absoluteFill, { transform: [{ rotate }] }]} pointerEvents="none">
          <LinearGradient
            colors={['rgba(255,255,255,0)', 'rgba(255,255,255,0.13)', 'rgba(255,255,255,0)']}
            start={{ x: 0.5, y: 0 }} end={{ x: 0.5, y: 1 }} style={s.sheen}
          />
        </Animated.View>
        <View style={StyleSheet.absoluteFill} pointerEvents="none">
          <Glass size={core} />
        </View>
        <View style={[StyleSheet.absoluteFill, { borderRadius: core / 2, borderWidth: 1.5, borderColor: 'rgba(255,255,255,0.4)' }]} pointerEvents="none" />
        {icon ? (
          <View style={s.iconWrap}>
            <Icon name={icon} size={Math.round(core * 0.22)} color={colors.white} />
          </View>
        ) : null}
      </Animated.View>
    </Pressable>
  );
}

const s = StyleSheet.create({
  wrap: { alignItems: 'center', justifyContent: 'center' },
  // Layers behind the sphere are placed by exact numbers (left / top), never left to "centre itself".
  layer: { position: 'absolute', left: 0, top: 0 },
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
  sheen: { position: 'absolute', left: '-20%', right: '-20%', top: '26%', height: '48%' },
  iconWrap: { position: 'absolute', alignItems: 'center', justifyContent: 'center', opacity: 0.95 },
});
