import Constants from 'expo-constants';
import * as Crypto from 'expo-crypto';
import * as Device from 'expo-device';
import { File, Paths } from 'expo-file-system';
import { startActivityAsync } from 'expo-intent-launcher';
import * as SecureStore from 'expo-secure-store';
import * as Updates from 'expo-updates';
import { AppState } from 'react-native';

import { api } from './api';
import * as diag from './diag';
import { isSpeaking } from './speech';

/**
 * App updates over the air (served and signed by the JARVIS server; see backend/core/appupdates.py).
 *
 * - The update library itself checks at every cold start. This adds a check whenever JARVIS comes back to the
 *   front (at most every 10 minutes), downloads a new version quietly, and switches to it the next time the app
 *   goes to the background while nothing is being said or rung, so nobody is ever cut off mid-sentence.
 * - The phone has a random id (sent with every check) so the server can make it a test phone.
 * - A new APK (only for native changes) is offered too: one tap, Android's own installer.
 */

export type UpdateState = 'off' | 'idle' | 'checking' | 'downloading' | 'ready' | 'current' | 'error';

export interface ApkOffer {
  version: string;
  size: number;
  notes: string;
  url: string;
}

export interface UpdatesSnapshot {
  state: UpdateState;
  /** A downloaded version waits for the next quiet moment (or "Restart now"). */
  ready: boolean;
  tester: boolean;
  deviceId: string;
  apk: ApkOffer | null;
  /** 0..1 while a new APK downloads, null otherwise. */
  apkProgress: number | null;
  apkError: string;
}

const K_DEVICE = 'jarvis.device';
const CHECK_EVERY_MS = 10 * 60_000;
const HELLO_EVERY_MS = 10 * 60_000;

let snap: UpdatesSnapshot = {
  state: Updates.isEnabled ? 'idle' : 'off', ready: false, tester: false, deviceId: '', apk: null, apkProgress: null, apkError: '',
};
const listeners = new Set<(s: UpdatesSnapshot) => void>();
let lastCheck = 0;
let lastHello = 0;
let started = false;
let busy: (() => boolean) | null = null;

function set(patch: Partial<UpdatesSnapshot>) {
  snap = { ...snap, ...patch };
  listeners.forEach((fn) => fn(snap));
}

export function updatesSnapshot() {
  return snap;
}

export function onUpdates(fn: (s: UpdatesSnapshot) => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

/** The app says when it is in the middle of something (talking, an alert on screen): no switch then. */
export function setUpdateBusyCheck(fn: (() => boolean) | null) {
  busy = fn;
}

/** When the code running now was published (null = the APK's own code). */
export function runningUpdateDate(): Date | null {
  return Updates.isEnabled && !Updates.isEmbeddedLaunch ? Updates.createdAt : null;
}

/** Short code of the update running now (what `manage.py app_update devices` shows), or '' for the APK's own code. */
export function runningUpdateCode(): string {
  return Updates.isEnabled && !Updates.isEmbeddedLaunch && Updates.updateId ? Updates.updateId.slice(0, 8) : '';
}

async function ensureDeviceId(): Promise<string> {
  let id = await SecureStore.getItemAsync(K_DEVICE).catch(() => null);
  if (!id) {
    id = Crypto.randomUUID().replace(/-/g, '');
    await SecureStore.setItemAsync(K_DEVICE, id).catch(() => undefined);
  }
  return id;
}

/** Once, when the app starts. */
export async function startUpdates() {
  if (started) return;
  started = true;
  const id = await ensureDeviceId();
  set({ deviceId: id });
  if (!Updates.isEnabled) return;
  if (Updates.isEmergencyLaunch) diag.note('warn', 'update', `emergency launch: ${Updates.emergencyLaunchReason ?? ''}`.slice(0, 200));
  await Updates.setExtraParamAsync('device', id).catch(() => undefined);
  setTimeout(() => void checkForUpdate(), 20_000); // after the library's own check at start-up
  AppState.addEventListener('change', (st) => {
    if (st === 'active') void checkForUpdate();
    else if (st === 'background') applyWhenQuiet();
  });
}

/** Ask the server; download a new version if there is one. `force`: the "Check now" button. */
export async function checkForUpdate(force = false): Promise<void> {
  if (!Updates.isEnabled) return;
  if (snap.state === 'checking' || snap.state === 'downloading') return;
  if (!force && Date.now() - lastCheck < CHECK_EVERY_MS) return;
  lastCheck = Date.now();
  set({ state: 'checking' });
  try {
    const found = await Updates.checkForUpdateAsync();
    if (!found.isAvailable && !found.isRollBackToEmbedded) {
      set({ state: snap.ready ? 'ready' : 'current' });
      return;
    }
    set({ state: 'downloading' });
    const got = await Updates.fetchUpdateAsync();
    const ready = snap.ready || got.isNew || got.isRollBackToEmbedded;
    set({ state: ready ? 'ready' : 'current', ready });
    if (got.isNew || got.isRollBackToEmbedded) diag.note('info', 'update', got.isRollBackToEmbedded ? 'roll back downloaded' : 'new version downloaded');
  } catch (e: any) {
    set({ state: snap.ready ? 'ready' : 'error' });
    diag.note('warn', 'update check', String(e?.message ?? e).slice(0, 200));
  }
}

/** Switch to the downloaded version now that JARVIS is in the background, unless it is saying or ringing something. */
function applyWhenQuiet() {
  if (!snap.ready) return;
  if (isSpeaking() || busy?.()) return;
  diag.note('info', 'update', 'switching to the new version in the background');
  Updates.reloadAsync().catch((e: any) => diag.note('warn', 'update reload', String(e?.message ?? e).slice(0, 200)));
}

/** The "Restart now" button. */
export async function restartIntoUpdate() {
  await Updates.reloadAsync();
}

/** Signed in: tell the server which phone this is and what it runs. It answers: test phone? newer APK? */
export async function sayHello(force = false): Promise<void> {
  if (!force && Date.now() - lastHello < HELLO_EVERY_MS) return;
  lastHello = Date.now();
  try {
    const device = snap.deviceId || (await ensureDeviceId());
    const res = await api<{ tester: boolean; apk: ApkOffer | null }>('/updates/hello/', {
      method: 'POST',
      body: {
        device,
        runtime: Updates.runtimeVersion ?? '',
        update_id: Updates.isEnabled && !Updates.isEmbeddedLaunch ? (Updates.updateId ?? '') : '',
        model: `${Device.manufacturer ?? ''} ${Device.modelName ?? ''}`.trim().slice(0, 80),
        app_version: Constants.expoConfig?.version ?? '',
      },
      timeoutMs: 10000,
    });
    set({ deviceId: device, tester: !!res.data.tester, apk: res.data.apk ?? null });
  } catch {
    // offline or an older server: try again later
  }
}

/** Download the offered APK and open Android's installer (he taps Install). Returns '' or what went wrong. */
export async function installApk(): Promise<string> {
  const offer = snap.apk;
  if (!offer || snap.apkProgress !== null) return '';
  set({ apkProgress: 0, apkError: '' });
  try {
    const dest = new File(Paths.cache, `JARVIS-${offer.version}.apk`);
    if (dest.exists) dest.delete();
    const task = File.createDownloadTask(offer.url, dest, {
      onProgress: ({ bytesWritten, totalBytes }) => set({ apkProgress: totalBytes ? Math.min(1, bytesWritten / totalBytes) : 0 }),
    });
    const file = await task.downloadAsync();
    if (!file || (offer.size && file.size !== offer.size)) throw new Error('incomplete download');
    set({ apkProgress: null });
    await startActivityAsync('android.intent.action.VIEW', {
      data: file.contentUri,
      flags: 1, // FLAG_GRANT_READ_URI_PERMISSION: the installer may read the file
      type: 'application/vnd.android.package-archive',
    });
    return '';
  } catch (e: any) {
    diag.note('warn', 'apk install', String(e?.message ?? e).slice(0, 200));
    const msg = 'Could not download the new version. Check the connection and try again.';
    set({ apkProgress: null, apkError: msg });
    return msg;
  }
}
