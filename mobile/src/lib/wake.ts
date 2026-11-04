/**
 * "Hey JARVIS" background listening — thin, safe wrapper around the native module in modules/jarvis-wake.
 * Every call is a no-op when the native part is missing, so the app never crashes because of it.
 */
import { Platform } from 'react-native';

export interface WakeConfig {
  serverUrl: string;
  accessToken: string;
  refreshToken: string;
  /** What to say when woken, e.g. "Ji Sir?" (spoken by the app once it is in front). */
  address: string;
  speakWhenLocked: boolean;
}

export interface WakeStatus {
  supported: boolean;
  running: boolean;
  overlayAllowed: boolean;
  micAllowed: boolean;
  batteryUnrestricted: boolean;
  manufacturer: string;
}

type Native = {
  getStatus: () => Promise<WakeStatus>;
  start: (config: WakeConfig) => Promise<boolean>;
  stop: () => Promise<void>;
  pause: () => void;
  resume: () => void;
  consumePendingWake: () => boolean;
  openOverlaySettings: () => void;
  openBatterySettings: () => void;
  openAutostartSettings: () => boolean;
  addListener: (event: 'wake', listener: () => void) => { remove: () => void };
};

let native: Native | null = null;
if (Platform.OS === 'android') {
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    native = require('../../modules/jarvis-wake').default as Native;
  } catch {
    native = null;
  }
}

export const wakeSupported = native !== null;

export async function wakeStatus(): Promise<WakeStatus> {
  if (!native) {
    return { supported: false, running: false, overlayAllowed: false, micAllowed: false, batteryUnrestricted: false, manufacturer: '' };
  }
  try {
    return await native.getStatus();
  } catch {
    return { supported: false, running: false, overlayAllowed: false, micAllowed: false, batteryUnrestricted: false, manufacturer: '' };
  }
}

export async function wakeStart(config: WakeConfig): Promise<boolean> {
  if (!native) return false;
  try {
    return await native.start(config);
  } catch {
    return false;
  }
}

export async function wakeStop() {
  if (!native) return;
  try {
    await native.stop();
  } catch {
    // ignore
  }
}

/** Pause detection while JARVIS itself is listening or speaking (one microphone user at a time). */
export function wakePause() {
  try {
    native?.pause();
  } catch {
    // ignore
  }
}

export function wakeResume() {
  try {
    native?.resume();
  } catch {
    // ignore
  }
}

/** True once if the app was brought to the front by "Hey JARVIS" (the greeting then happens in JS). */
export function wakeConsumePending(): boolean {
  try {
    return native?.consumePendingWake() ?? false;
  } catch {
    return false;
  }
}

export function onWake(listener: () => void): () => void {
  if (!native) return () => undefined;
  try {
    const sub = native.addListener('wake', listener);
    return () => sub.remove();
  } catch {
    return () => undefined;
  }
}

export const wakeSettings = {
  overlay: () => native?.openOverlaySettings(),
  battery: () => native?.openBatterySettings(),
  /** Xiaomi / Redmi "Autostart" page. Returns false when this phone has no such page. */
  autostart: (): boolean => {
    try {
      return native?.openAutostartSettings() ?? false;
    } catch {
      return false;
    }
  },
};
