import { Accelerometer } from 'expo-sensors';
import { useCallback, useEffect, useRef, useState } from 'react';
import { AppState, Platform } from 'react-native';

export type PresenceState = 'present' | 'idle' | 'away' | 'facedown' | 'meeting' | 'quiet';

export interface PresenceInputs {
  awayAfterMinutes: number;
  meetingMode: boolean;
  busyWindows: { start: string; end: string }[];
  /** He switched meeting mode off during a diary meeting: the diary does not count as "in a meeting" until this time. */
  busyOverrideUntil?: number;
  quietHoursEnabled: boolean;
  quietStartHour: number;
  quietEndHour: number;
  /** Speak only when he is really here (a touch, a word, "Hey JARVIS" within the last few minutes); otherwise show
   *  and buzz, and count it when he is back. Off: the app being in front is enough. */
  speakOnlyWhenHere?: boolean;
}

const MOVE_THRESHOLD = 0.06; // g change between samples that counts as "picked up / moved"
const FACE_DOWN_Z = 0.8;
const FACE_DOWN_SAMPLES = 6; // ~3 s at 500 ms
const SNAPSHOT_MS = 1000;
/** How long a touch, a word or a wake keeps him "here" (his ask, 26 Nov 2026: "within the last few minutes"). */
export const HERE_MS = 3 * 60_000;

// Android reports +1 g on z when lying face up (SensorManager convention). Check on the device
// with the live indicator in Settings; flip here if a phone reports the opposite sign.
const FACE_UP_SIGN = Platform.OS === 'ios' ? -1 : 1;

// "He is here": the last moment he touched the screen, spoke to JARVIS, said "Hey JARVIS", unlocked JARVIS or
// brought the app to the front. Kept outside React so the root view, the conversation and the lock can all mark it.
let hereAt = Date.now();
export function noteHere(_reason: 'touch' | 'spoke' | 'wake' | 'unlock' | 'open' | 'tap' = 'touch') {
  hereAt = Date.now();
}
export function hereSince(): number {
  return hereAt;
}
export function isHere(now = Date.now()): boolean {
  return now - hereAt < HERE_MS;
}

interface Snapshot {
  now: number;
  idleMs: number;
  faceDown: boolean;
  z: number;
  /** JARVIS is open on the screen in front of him: he is here, however still the phone lies. */
  appActive: boolean;
  /** He touched, spoke, woke or opened JARVIS within the last few minutes. */
  here: boolean;
}

function inQuietHours(now: Date, start: number, end: number) {
  const h = now.getHours();
  return start > end ? h >= start || h < end : h >= start && h < end;
}

export function usePresence(inputs: PresenceInputs) {
  // Sensor callbacks write to refs only; a 1 s snapshot drives rendering (avoids re-rendering on every sample).
  const lastMove = useRef(0);
  const lastSample = useRef<{ x: number; y: number; z: number } | null>(null);
  const faceDownCount = useRef(0);
  const zRef = useRef(0);
  const [snap, setSnap] = useState<Snapshot>({ now: 0, idleMs: 0, faceDown: false, z: 0, appActive: false, here: true });
  const [sensorAvailable, setSensorAvailable] = useState(true);

  const takeSnapshot = useCallback(() => {
    const now = Date.now();
    if (!lastMove.current) lastMove.current = now;
    const next: Snapshot = {
      now,
      idleMs: now - lastMove.current,
      faceDown: faceDownCount.current >= FACE_DOWN_SAMPLES,
      z: Math.round(zRef.current * 100) / 100,
      appActive: AppState.currentState === 'active',
      here: isHere(now),
    };
    setSnap((prev) =>
      prev.faceDown === next.faceDown && Math.abs(prev.idleMs - next.idleMs) < SNAPSHOT_MS * 0.5 && prev.z === next.z
        && prev.appActive === next.appActive && prev.here === next.here
        ? prev
        : next,
    );
  }, []);

  const markActive = useCallback(() => {
    lastMove.current = Date.now();
    noteHere('open');
    takeSnapshot();
  }, [takeSnapshot]);

  useEffect(() => {
    let sub: { remove: () => void } | null = null;
    let cancelled = false;
    lastMove.current = Date.now();
    Accelerometer.isAvailableAsync()
      .catch(() => false)
      .then((available) => {
        if (cancelled) return;
        setSensorAvailable(available);
        if (!available) return;
        Accelerometer.setUpdateInterval(500);
        sub = Accelerometer.addListener(({ x, y, z }) => {
          const prev = lastSample.current;
          if (prev && Math.abs(x - prev.x) + Math.abs(y - prev.y) + Math.abs(z - prev.z) > MOVE_THRESHOLD) {
            lastMove.current = Date.now();
          }
          lastSample.current = { x, y, z };
          zRef.current = z;
          faceDownCount.current = z * FACE_UP_SIGN < -FACE_DOWN_Z ? faceDownCount.current + 1 : 0;
        });
      });
    const appSub = AppState.addEventListener('change', (s) => {
      if (s === 'active') markActive();
    });
    const timer = setInterval(takeSnapshot, SNAPSHOT_MS);
    return () => {
      cancelled = true;
      sub?.remove();
      appSub.remove();
      clearInterval(timer);
    };
  }, [markActive, takeSnapshot]);

  const now = snap.now;
  const diaryMeeting =
    now > 0 && now >= (inputs.busyOverrideUntil ?? 0) &&
    inputs.busyWindows.some((w) => new Date(w.start).getTime() <= now && now < new Date(w.end).getTime());
  const inMeeting = inputs.meetingMode || diaryMeeting;
  // Not moving does not mean gone: with JARVIS open on the screen in front of him, he is here.
  const away = snap.idleMs > inputs.awayAfterMinutes * 60_000 && !snap.appActive;
  const quiet = now > 0 && inputs.quietHoursEnabled && inQuietHours(new Date(now), inputs.quietStartHour, inputs.quietEndHour);
  // The app in front but nothing from him for a few minutes (no touch, no word): near, not with the phone. JARVIS
  // shows and buzzes instead of speaking, and counts it the moment he is back (his ask, 26 Nov 2026).
  const idle = now > 0 && (inputs.speakOnlyWhenHere ?? true) && !snap.here;

  // Order matters: face down is an explicit "silence"; a meeting beats "away" because he is in the room.
  let state: PresenceState = 'present';
  if (snap.faceDown) state = 'facedown';
  else if (inMeeting) state = 'meeting';
  else if (away) state = 'away';
  else if (quiet) state = 'quiet';
  else if (idle) state = 'idle';

  return {
    state,
    faceDown: snap.faceDown,
    inMeeting,
    /** "In a meeting" because of the diary (not the switch). */
    diaryMeeting,
    sensorAvailable,
    zValue: snap.z,
    idleSeconds: Math.round(snap.idleMs / 1000),
    markActive,
  };
}

export const presenceLabel: Record<PresenceState, { title: string; detail: string }> = {
  present: { title: 'With you', detail: 'Alerts will be spoken' },
  idle: { title: 'Nearby', detail: 'Shown and buzzed, said once you touch or speak' },
  away: { title: 'Away', detail: 'Holding alerts until you return' },
  facedown: { title: 'Face down', detail: 'Silent — urgent alerts vibrate only' },
  meeting: { title: 'In a meeting', detail: 'Silent — urgent alerts vibrate only' },
  quiet: { title: 'Quiet hours', detail: 'Only urgent alerts, silently' },
};
