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
