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
  /** Also listen for "Hey JARVIS" (microphone). The reminder / message watcher runs either way. */
  wakeWord: boolean;
}

export interface WakeStatus {
  supported: boolean;
  /** The background service runs (reminders and messages reach a closed app). */
  running: boolean;
  /** The microphone part ("Hey JARVIS") runs too. */
  listening: boolean;
  overlayAllowed: boolean;
  micAllowed: boolean;
  notificationsAllowed: boolean;
  batteryUnrestricted: boolean;
  manufacturer: string;
}

const NO_STATUS: WakeStatus = {
  supported: false, running: false, listening: false, overlayAllowed: false, micAllowed: false,
  notificationsAllowed: false, batteryUnrestricted: false, manufacturer: '',
};

export interface TtsEvent {
  id: string;
  event: 'done' | 'error' | 'stopped';
}

type Native = {
  getStatus: () => Promise<WakeStatus>;
  start: (config: WakeConfig) => Promise<boolean>;
  setListening: (on: boolean) => Promise<boolean>;
  stop: () => Promise<void>;
  getPushToken: () => Promise<string | null>;
  deletePushToken: () => Promise<void>;
  pause: () => void;
  resume: () => void;
  consumePendingWake: () => boolean;
  openOverlaySettings: () => void;
  openBatterySettings: () => void;
  openAutostartSettings: () => boolean;
  openMiuiPermissions: () => boolean;
  speak: (id: string, text: string, lang: string, fallback: string | null, rate: number) => void;
  stopSpeaking: () => void;
  addListener: ((event: 'wake' | 'push', listener: () => void) => { remove: () => void }) &
    ((event: 'tts', listener: (e: TtsEvent) => void) => { remove: () => void });
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
  if (!native) return NO_STATUS;
  try {
    return { ...NO_STATUS, ...(await native.getStatus()) };
  } catch {
    return NO_STATUS;
  }
}

/** Start or re-apply the background service. Resolves to whether it now listens for "Hey JARVIS". */
export async function wakeStart(config: WakeConfig): Promise<boolean> {
  if (!native) return false;
  try {
    return await native.start(config);
  } catch {
    return false;
  }
}

/** Switch only the "Hey JARVIS" microphone part; reminders and messages keep arriving. */
export async function wakeSetListening(on: boolean): Promise<boolean> {
  if (!native) return false;
  try {
    return await native.setListening(on);
  } catch {
    return false;
  }
}

/** Sign-out only: the service stops for good. */
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

// ---------------------------------------------------------------- live push (Firebase Cloud Messaging)
/** This phone's push token, or null when push is not available on this build / phone. */
export async function pushToken(): Promise<string | null> {
  if (!native || typeof native.getPushToken !== 'function') return null;
  try {
    return await native.getPushToken();
  } catch {
    return null;
  }
}

/** Sign-out: the phone stops receiving pushes for this login. */
export async function pushForget() {
  if (!native || typeof native.deletePushToken !== 'function') return;
  try {
    await native.deletePushToken();
  } catch {
    // ignore
  }
}

/** A push arrived while the app is open: refresh alerts at once. */
export function onPush(listener: () => void): () => void {
  if (!native) return () => undefined;
  try {
    const sub = native.addListener('push', listener);
    return () => sub.remove();
  } catch {
    return () => undefined;
  }
}

// ---------------------------------------------------------------- the voice (self-healing TTS)
export const ttsAvailable = native !== null && typeof native.speak === 'function';

export function ttsSpeak(id: string, text: string, lang: string, fallback: string | null, rate: number): boolean {
  if (!native || !ttsAvailable) return false;
  try {
    native.speak(id, text, lang, fallback, rate);
    return true;
  } catch {
    return false;
  }
}

export function ttsStop() {
  try {
    native?.stopSpeaking();
  } catch {
    // ignore
  }
}

export function onTts(listener: (e: TtsEvent) => void): () => void {
  if (!native || !ttsAvailable) return () => undefined;
  try {
    const sub = native.addListener('tts', listener);
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
  /** Xiaomi app-permission page ("Display pop-up windows while running in the background"). */
  miuiPermissions: (): boolean => {
    try {
      return native?.openMiuiPermissions() ?? false;
    } catch {
      return false;
    }
  },
};
