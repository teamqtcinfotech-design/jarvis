/**
 * "Hey JARVIS" background listening — thin, safe wrapper around the native module in modules/jarvis-wake.
 * Every call is a no-op when the native part is missing, so the app never crashes because of it.
 */
import { Platform } from 'react-native';

export interface WakeConfig {
  serverUrl: string;
  accessToken: string;
  refreshToken: string;
  /** How JARVIS addresses this person ("Sir", "Ma'am", a first name), as in "Ji Sir?". */
  address: string;
  /** The same in Devanagari, for the Hindi lines of the closed-app conversation. */
  addressHi?: string;
  /** Whether this phone's user is the MD (the closed-app loop words a few lines differently). */
  isMd?: boolean;
  /** The PA's name as JARVIS says it, so the closed-app listener expects it (left out = keep the last one). */
  paName?: string;
  /** Names the closed-app recogniser should expect (staff, contacts, the two of them), so they are not misheard. */
  hints?: string[];
  /** The phone voice for English lines ('hi-IN' Hindi accent, 'en-IN' Indian English); Hindi lines always use hi-IN. */
  voiceAccent?: string;
  /** The language the closed-app recogniser starts in ('en-IN' understands Hinglish too; 'hi-IN' for pure Hindi). */
  listenLang?: string;
  /** The language JARVIS is speaking to this person right now ('hi' / 'en'), for the few lines the phone builds itself. */
  replyLang?: string;
  /** Notification sound for reminders that arrive while the app is closed ('default' = the phone's, 'silent'). */
  reminderSound?: string;
  /** Talking over JARVIS while it speaks stops it and it listens (off if the room echoes). */
  bargeIn?: boolean;
  /** Which Grok voice JARVIS speaks with (the MD's choice; male voices). */
  voiceName?: string;
  /** The wake word chosen (src/lib/wakeWords.ts): its listener file, the phrase, and the assistant's name with it. */
  wakeModel?: string;
  wakePhrase?: string;
  assistantName?: string;
  assistantNameHi?: string;
  speakWhenLocked: boolean;
  /** Also listen for "Hey JARVIS" (microphone). The reminder / message watcher runs either way. */
  wakeWord: boolean;
  /** Walkie-talkie: keep the line to the relay open (the person's switch), and where the relay is (from /me/). */
  walkie?: boolean;
  walkieUrl?: string;
  /** The floating JARVIS button over other apps (a tap talks, like "Hey JARVIS"); needs "Display over other apps". */
  floatingButton?: boolean;
  /** With the app closed: speak only when he is here (the screen unlocked within the last few minutes, or a word to
   *  JARVIS); otherwise a notification, and the count when he is back. */
  speakOnlyWhenHere?: boolean;
  /** With the app closed: listen to the room for a moment before speaking; busy means everything but urgent waits. */
  checkRoom?: boolean;
}

/** What the room sounds like right now: busy = several voices or steady noise over the last moment. */
export interface RoomCheck {
  busy: boolean;
  /** Average sound level (0 = silence, ~3000 = speech at the phone). */
  level: number;
  /** The share of the moment with sound at a speaking level (0..1). */
  share: number;
  /** Where the sound was measured: the wake-word microphone, the live line, or a short recording of its own. */
  source: string;
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
  /** How often "Hey JARVIS" fired today, and how often nothing was said after it (a false wake, most likely). */
  wakesToday: number;
  emptyWakesToday: number;
}

const NO_STATUS: WakeStatus = {
  supported: false, running: false, listening: false, overlayAllowed: false, micAllowed: false,
  notificationsAllowed: false, batteryUnrestricted: false, manufacturer: '', wakesToday: 0, emptyWakesToday: 0,
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
  consumePendingOpen?: () => string | null;
  consumePendingShare?: () => string | null;
  noteEmptyWake?: () => void;
  openOverlaySettings: () => void;
  openBatterySettings: () => void;
  openAutostartSettings: () => boolean;
  openMiuiPermissions: () => boolean;
  speak: (id: string, text: string, lang: string, fallback: string | null, rate: number) => void;
  stopSpeaking: () => void;
  setVoiceName?: (name: string) => void;
  walkieStart?: (url: string, enabled: boolean) => Promise<boolean>;
  walkieTalk?: (to: number[], talk: string | null) => void;
  walkieEnd?: () => void;
  walkieQuiet?: (minutes: number) => void;
  walkieRefresh?: () => void;
  walkieStatus?: () => string;
  consumePendingWalkie?: () => string | null;
  // sign in by phone (2.7.0 and later)
  otpSupported?: () => boolean;
  otpStart?: (phone: string) => Promise<OtpResult>;
  otpConfirm?: (code: string) => Promise<OtpResult>;
  otpAwaitAuto?: () => Promise<OtpResult>;
  otpCancel?: () => void;
  // the room check before speaking (2.11.0 and later)
  roomCheck?: (ms: number) => Promise<RoomCheck>;
  // the live voice line (2.8.0 and later)
  liveSupported?: () => boolean;
  liveStart?: (config: LiveConfig) => Promise<boolean>;
  liveStop?: () => void;
  liveToolResult?: (callId: string, output: string) => void;
  liveSay?: (text: string) => void;
  liveInterrupt?: () => void;
  liveStatus?: () => string;
  addListener: ((event: 'wake' | 'push' | 'barge', listener: () => void) => { remove: () => void }) &
    ((event: 'tts', listener: (e: TtsEvent) => void) => { remove: () => void }) &
    ((event: 'walkie', listener: (e: WalkieEvent) => void) => { remove: () => void }) &
    ((event: 'live', listener: (e: LiveEvent) => void) => { remove: () => void });
};

/** What the native live line needs to open (from /assistant/live/session/, see src/lib/liveVoice.ts). */
export interface LiveConfig {
  url: string;
  token: string;
  /** The session.update body, as JSON text (the persona, the facts, the form tools). */
  sessionJson: string;
  rate: number;
  bargeIn: boolean;
  bargeLevel: number;
  idleSeconds: number;
  maxSeconds: number;
  /** Said by the line as soon as it opens ("Ji Sir?"), or ''. */
  greeting: string;
}

/** What the live line reports: the event name and its details (JSON text from the native side). */
export interface LiveEvent {
  event: 'state' | 'heard' | 'said' | 'tool' | 'speaking' | 'listening' | 'barge' | 'usage' | 'level' | 'error';
  data: string;
}

/** What the walkie-talkie engine reports: the event name and its details (JSON text from the native side). */
export interface WalkieEvent {
  event: 'state' | 'people' | 'floor' | 'busy' | 'refused' | 'cut' | 'incoming' | 'done' | 'ended' | 'quiet' | 'level' | 'error';
  data: string;
}

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

/** The always-on part can measure the room (a build with roomCheck). */
export function wakeRoomCheckSupported(): boolean {
  return typeof native?.roomCheck === 'function';
}

/** What the room sounds like over the last moment (at most `ms` of listening when nothing is already open). */
export async function wakeRoomCheck(ms = 1500): Promise<RoomCheck | null> {
  if (!native?.roomCheck) return null;
  try {
    return await native.roomCheck(ms);
  } catch {
    return null;
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

/** The reminder / alert key of the notification that just opened the app, once; null otherwise. */
export function wakeConsumeOpenKey(): string | null {
  try {
    return native?.consumePendingOpen?.() ?? null;
  } catch {
    return null;
  }
}

/** A photo or file shared into JARVIS from another app (the Share sheet), once; null otherwise. */
export interface SharedIn {
  uri: string;
  mime: string;
  name: string;
  text: string;
}

export function wakeConsumeShare(): SharedIn | null {
  try {
    const raw = native?.consumePendingShare?.();
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<SharedIn>;
    if (!parsed.uri) return null;
    return { uri: parsed.uri, mime: parsed.mime || 'application/octet-stream', name: parsed.name || 'shared', text: parsed.text || '' };
  } catch {
    return null;
  }
}

/** Woken by "Hey JARVIS", greeted, and nothing was said: counted, so Settings can show how often that happens. */
export function wakeNoteEmpty() {
  try {
    native?.noteEmptyWake?.();
  } catch {
    // ignore
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

/** He started talking while JARVIS was speaking: the native side has already stopped the voice; now listen. */
export function onBarge(listener: () => void): () => void {
  if (!native) return () => undefined;
  try {
    const sub = native.addListener('barge', listener);
    return () => sub.remove();
  } catch {
    return () => undefined;
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

/** JARVIS's voice changed in Settings: the native player uses it from the next sentence. */
export function wakeSetVoice(name: string) {
  try {
    native?.setVoiceName?.(name);
  } catch {
    // an older native part: the voice follows at the next start
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

// ---------------------------------------------------------------- walkie-talkie (the native engine)
export const walkieSupported = native !== null && typeof native.walkieStart === 'function';

export async function walkieStart(url: string, enabled: boolean): Promise<boolean> {
  if (!native?.walkieStart) return false;
  try {
    return await native.walkieStart(url, enabled);
  } catch {
    return false;
  }
}

/** Hold to talk: to these people (a new talk) or a reply inside `talk`. The sound streams from this moment. */
export function walkieTalk(to: number[], talk: string | null) {
  try {
    native?.walkieTalk?.(to, talk);
  } catch {
    // ignore
  }
}

export function walkieEnd() {
  try {
    native?.walkieEnd?.();
  } catch {
    // ignore
  }
}

export function walkieQuiet(minutes: number) {
  try {
    native?.walkieQuiet?.(minutes);
  } catch {
    // ignore
  }
}

export function walkieRefresh() {
  try {
    native?.walkieRefresh?.();
  } catch {
    // ignore
  }
}

export function walkieStatus(): Record<string, unknown> {
  try {
    return JSON.parse(native?.walkieStatus?.() ?? '{}');
  } catch {
    return {};
  }
}

/** The talk behind the incoming card that just opened the app, once; null otherwise. */
export function walkieConsumePending(): string | null {
  try {
    return native?.consumePendingWalkie?.() ?? null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- sign in by phone (an SMS code through Firebase)
export const otpSupported = native !== null && typeof native.otpStart === 'function';

export interface OtpResult {
  status: 'sent' | 'verified';
  id_token?: string;
  auto?: boolean;
}

/** Sends the code to +91… Resolves "sent" (type the code) or "verified" (the phone read it by itself). */
export function otpStart(phone: string): Promise<OtpResult> {
  if (!native?.otpStart) return Promise.reject(new Error('Phone sign-in needs the new app.'));
  return native.otpStart(phone);
}

/** The typed code: resolves "verified" with the proof for the server. */
export function otpConfirm(code: string): Promise<OtpResult> {
  if (!native?.otpConfirm) return Promise.reject(new Error('Phone sign-in needs the new app.'));
  return native.otpConfirm(code);
}

/** Waits for the phone to read the code by itself (resolves like otpConfirm); use alongside the typed path. */
export function otpAwaitAuto(): Promise<OtpResult> {
  if (!native?.otpAwaitAuto) return new Promise(() => undefined);
  return native.otpAwaitAuto();
}

export function otpCancel() {
  try {
    native?.otpCancel?.();
  } catch {
    // ignore
  }
}

// ---------------------------------------------------------------- the live voice line (Grok realtime)
export const liveSupported = native !== null && typeof native.liveStart === 'function';

export async function liveStart(config: LiveConfig): Promise<boolean> {
  if (!native?.liveStart) return false;
  try {
    return await native.liveStart(config);
  } catch {
    return false;
  }
}

export function liveStop() {
  try {
    native?.liveStop?.();
  } catch {
    // ignore
  }
}

/** The server's answer to a form the model filled: the line reads its "say" text back. */
export function liveToolResult(callId: string, output: string) {
  try {
    native?.liveToolResult?.(callId, output);
  } catch {
    // ignore
  }
}

/** Make the line say this now, word for word. */
export function liveSay(text: string) {
  try {
    native?.liveSay?.(text);
  } catch {
    // ignore
  }
}

export function liveInterrupt() {
  try {
    native?.liveInterrupt?.();
  } catch {
    // ignore
  }
}

export function onLive(listener: (e: LiveEvent) => void): () => void {
  if (!native || !liveSupported) return () => undefined;
  try {
    const sub = native.addListener('live', listener);
    return () => sub.remove();
  } catch {
    return () => undefined;
  }
}

export function onWalkie(listener: (e: WalkieEvent) => void): () => void {
  if (!native || !walkieSupported) return () => undefined;
  try {
    const sub = native.addListener('walkie', listener);
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
