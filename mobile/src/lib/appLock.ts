/**
 * The fingerprint lock (Nov 2026): JARVIS asks for the phone's fingerprint / face / PIN when it comes back to the
 * front after more than LOCK_AFTER_MS away, and always before the Personal list and the Devices page. Per phone
 * (kept here, not on the server), on by default where the phone has a fingerprint or face enrolled.
 */
import * as LocalAuthentication from 'expo-local-authentication';
import * as SecureStore from 'expo-secure-store';

import { noteHere } from './presence';

const K_LOCK = 'jarvis.lock';            // "on" | "off"
export const LOCK_AFTER_MS = 5 * 60_000;  // away this long -> ask again

let lastUnlockAt = 0;

// The cover itself (AppLock.tsx): while it is up, nothing opens and nothing is said (his rule, 27 Nov 2026); what
// was about to happen waits here and runs the moment he unlocks.
let covered = false;
const waiters: (() => void)[] = [];
const listeners = new Set<(on: boolean) => void>();

/** The lock cover is on the screen right now. */
export function isLockedNow(): boolean {
  return covered;
}

/** Told by the cover when it shows and when it goes. */
export function setCovered(on: boolean) {
  if (covered === on) return;
  covered = on;
  for (const l of Array.from(listeners)) {
    try {
      l(on);
    } catch {
      // one listener must not stop the others
    }
  }
  if (!on) {
    const due = waiters.splice(0);
    for (const fn of due) {
      try {
        fn();
      } catch {
        // ignore
      }
    }
  }
}

/** Run now, or the moment the cover goes. */
export function onceUnlocked(fn: () => void) {
  if (!covered) fn();
  else waiters.push(fn);
}

/** Every change of the cover (true = shown, false = gone). Returns the unsubscribe. */
export function onLockChange(listener: (on: boolean) => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export async function lockAvailable(): Promise<boolean> {
  try {
    return (await LocalAuthentication.hasHardwareAsync()) && (await LocalAuthentication.isEnrolledAsync());
  } catch {
    return false;
  }
}

export async function lockEnabled(): Promise<boolean> {
  try {
    const v = await SecureStore.getItemAsync(K_LOCK);
    if (v === 'off') return false;
    if (v === 'on') return true;
    return await lockAvailable(); // never set: on when the phone can do it
  } catch {
    return false;
  }
}

export async function setLockEnabled(on: boolean) {
  try {
    await SecureStore.setItemAsync(K_LOCK, on ? 'on' : 'off');
  } catch {
    // ignore
  }
}

/** True when the last unlock is fresh enough that no prompt is needed. */
export function recentlyUnlocked(): boolean {
  return Date.now() - lastUnlockAt < LOCK_AFTER_MS;
}

export function markUnlocked() {
  lastUnlockAt = Date.now();
  noteHere('unlock'); // his fingerprint: he is here
}

export function markLocked() {
  lastUnlockAt = 0;
}

/** Asks the phone. Resolves true when the person is who they say (or the lock is off / not available). */
export async function unlock(reason = 'Unlock JARVIS'): Promise<boolean> {
  if (!(await lockEnabled())) return true;
  if (!(await lockAvailable())) return true;
  try {
    const res = await LocalAuthentication.authenticateAsync({
      promptMessage: reason,
      cancelLabel: 'Cancel',
      disableDeviceFallback: false, // the phone's PIN / pattern works too
    });
    if (res.success) markUnlocked();
    return res.success;
  } catch {
    return false;
  }
}
