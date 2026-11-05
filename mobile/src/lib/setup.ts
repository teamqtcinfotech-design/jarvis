/**
 * One-time phone setup (permissions + "Hey JARVIS" on). Remembered per phone, so the questions are asked
 * once after the first sign-in and never again; Settings can run the setup again.
 */
import * as SecureStore from 'expo-secure-store';
import { useEffect, useState } from 'react';

const KEY = 'jarvis.setup.v1';
let cached: boolean | null = null;

export async function isSetupDone(): Promise<boolean> {
  if (cached !== null) return cached;
  try {
    cached = (await SecureStore.getItemAsync(KEY)) === 'done';
  } catch {
    cached = false;
  }
  return cached;
}

export async function markSetupDone() {
  cached = true;
  await SecureStore.setItemAsync(KEY, 'done').catch(() => undefined);
}

/** null while unknown, then true / false. */
export function useSetupDone(): boolean | null {
  const [done, setDone] = useState<boolean | null>(cached);
  useEffect(() => {
    let cancelled = false;
    isSetupDone().then((v) => !cancelled && setDone(v));
    return () => {
      cancelled = true;
    };
  }, []);
  return done;
}
