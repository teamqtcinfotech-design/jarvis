import * as Crypto from 'expo-crypto';
import * as SecureStore from 'expo-secure-store';

import type { SessionUser } from './types';

const K_SERVER = 'jarvis.server';
const K_ACCESS = 'jarvis.access';
const K_REFRESH = 'jarvis.refresh';
const K_USER = 'jarvis.user';

let serverUrl = '';
let accessToken = '';
let refreshTokenValue = '';
let authLostHandler: (() => void) | null = null;
let refreshInFlight: Promise<boolean> | null = null;

export class ApiError extends Error {
  status: number;
  code: string;
  retryable: boolean;
  body: any;
  constructor(status: number, code: string, message: string, body: any = null) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryable = status === 0 || status >= 500 || status === 429;
    this.body = body;
  }
}

export const newIdempotencyKey = () => Crypto.randomUUID();

export function normalizeServer(input: string): string {
  let url = input.trim().replace(/\/+$/, '');
  if (url && !/^https?:\/\//i.test(url)) url = `http://${url}`;
  return url;
}

export function onAuthLost(handler: (() => void) | null) {
  authLostHandler = handler;
}

export function currentServer() {
  return serverUrl;
}

/** Asks the server for a separate login for the background "Hey JARVIS" service, so its refreshes never clash with the app's. */
export async function deviceTokens(): Promise<{ access: string; refresh: string }> {
  const res = await api<{ access: string; refresh: string }>('/auth/device-token/', { method: 'POST', body: {} });
  return res.data;
}

export async function loadStoredSession(): Promise<{ server: string; user: SessionUser | null }> {
  serverUrl = (await SecureStore.getItemAsync(K_SERVER)) ?? '';
  accessToken = (await SecureStore.getItemAsync(K_ACCESS)) ?? '';
  refreshTokenValue = (await SecureStore.getItemAsync(K_REFRESH)) ?? '';
  const rawUser = await SecureStore.getItemAsync(K_USER);
  const user = rawUser && refreshTokenValue ? (JSON.parse(rawUser) as SessionUser) : null;
  return { server: serverUrl, user };
}

/** Keep the profile the server sent (name, how this person is addressed) with the session, so it survives a restart. */
export async function storeUser(user: SessionUser) {
  await SecureStore.setItemAsync(K_USER, JSON.stringify(user));
}

async function storeTokens(access: string, refresh?: string) {
  accessToken = access;
  await SecureStore.setItemAsync(K_ACCESS, access);
  if (refresh) {
    refreshTokenValue = refresh;
    await SecureStore.setItemAsync(K_REFRESH, refresh);
  }
}

async function rawFetch(path: string, init: RequestInit & { timeoutMs?: number }) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), init.timeoutMs ?? 20000);
  try {
    return await fetch(`${serverUrl}/api/v1${path}`, { ...init, signal: controller.signal });
  } catch (e: any) {
    const aborted = e?.name === 'AbortError';
    diagNote?.('warn', `api ${init.method ?? 'GET'} ${path.split('?')[0]}`, aborted ? 'timeout' : `network: ${e?.message ?? e}`);
    throw new ApiError(0, aborted ? 'timeout' : 'network', aborted ? 'The server took too long to respond.' : 'Cannot reach the JARVIS server. Check Wi-Fi.');
  } finally {
    clearTimeout(timer);
  }
}

/** This phone, as the server lists it under "where am I signed in": its model and its own random id. */
export interface DeviceInfo {
  device_name: string;
  device_id: string;
}

async function finishLogin(res: Response): Promise<SessionUser> {
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = res.status === 401 ? (body?.message ?? 'Sign-in was refused.') : body?.message ?? 'Sign-in failed.';
    throw new ApiError(res.status, body?.code ?? 'login_failed', msg, body);
  }
  await SecureStore.setItemAsync(K_SERVER, serverUrl);
  await storeTokens(body.access, body.refresh);
  const user = { ...body.user, session_id: body.session_id } as SessionUser;
  await SecureStore.setItemAsync(K_USER, JSON.stringify(user));
  return user;
}

/** Sign in by username and password (the older way; the server may have it switched off). */
export async function login(server: string, username: string, password: string, device?: DeviceInfo): Promise<SessionUser> {
  serverUrl = normalizeServer(server);
  const res = await rawFetch('/auth/login/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ username: username.trim(), password, ...(device ?? {}) }),
  });
  return finishLogin(res);
}

/** Sign in by phone number: Firebase's proof that this phone holds the number (lib/wake otpStart / otpConfirm). */
export async function loginWithPhone(server: string, idToken: string, device: DeviceInfo): Promise<SessionUser> {
  serverUrl = normalizeServer(server);
  const res = await rawFetch('/auth/phone/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ id_token: idToken, ...device }),
  });
  return finishLogin(res);
}

export interface SessionRow {
  id: string;
  name: string;
  how: 'phone' | 'password';
  signed_in_at: string;
  last_seen_at: string;
  this_phone: boolean;
  active: boolean;
  revoked_at: string | null;
  revoked_why: string;
}

/** Where this account is signed in (this phone first). */
export async function sessions(): Promise<SessionRow[]> {
  return (await api<{ results: SessionRow[] }>('/auth/sessions/')).data.results;
}

/** Sign one phone out, now. */
export async function revokeSession(id: string) {
  await api(`/auth/sessions/${id}/revoke/`, { method: 'POST', body: {} });
}

/** Sign out every phone but this one. */
export async function revokeOtherSessions(): Promise<number> {
  return (await api<{ signed_out: number }>('/auth/sessions/', { method: 'POST', body: {} })).data.signed_out;
}

export async function logout() {
  try {
    if (refreshTokenValue) {
      await rawFetch('/auth/logout/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ refresh: refreshTokenValue }),
        timeoutMs: 5000,
      });
    }
  } catch {
    // best effort
  }
  accessToken = '';
  refreshTokenValue = '';
  await SecureStore.deleteItemAsync(K_ACCESS);
  await SecureStore.deleteItemAsync(K_REFRESH);
  await SecureStore.deleteItemAsync(K_USER);
}

async function refreshAccess(): Promise<boolean> {
  if (!refreshTokenValue) return false;
  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      try {
        const res = await rawFetch('/auth/refresh/', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh: refreshTokenValue }),
          timeoutMs: 10000,
        });
        if (!res.ok) return false;
        const body = await res.json();
        await storeTokens(body.access, body.refresh);
        return true;
      } catch {
        return false;
      } finally {
        setTimeout(() => (refreshInFlight = null), 0);
      }
    })();
  }
  return refreshInFlight;
}

export interface ApiOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE';
  body?: unknown;
  idempotencyKey?: string;
  timeoutMs?: number;
}

export async function api<T = any>(path: string, opts: ApiOptions = {}): Promise<{ status: number; data: T }> {
  const doFetch = () => {
    const headers: Record<string, string> = { Accept: 'application/json', Authorization: `Bearer ${accessToken}` };
    if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
    if (opts.idempotencyKey) headers['Idempotency-Key'] = opts.idempotencyKey;
    return rawFetch(path, {
      method: opts.method ?? 'GET',
      headers,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      timeoutMs: opts.timeoutMs,
    });
  };
  let res = await doFetch();
  if (res.status === 401) {
    const ok = await refreshAccess();
    if (!ok) {
      authLostHandler?.();
      throw new ApiError(401, 'auth_lost', 'Your session has expired. Please sign in again.');
    }
    res = await doFetch();
  }
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    if (res.status >= 500) diagNote?.('error', `api ${opts.method ?? 'GET'} ${path.split('?')[0]}`, `server ${res.status} ${data?.code ?? ''}`);
    throw new ApiError(res.status, data?.code ?? 'error', data?.message ?? `Request failed (${res.status}).`, data);
  }
  return { status: res.status, data: data as T };
}

/** The bearer header for a request made outside api() (an <Image> of a file, a download). */
export function authHeaders(): Record<string, string> {
  return { Authorization: `Bearer ${accessToken}` };
}

/** Where a file on a task is fetched from (with authHeaders()). */
export function fileUrl(fileId: string): string {
  return `${serverUrl}/api/v1/files/${fileId}/`;
}

/** A file picked on the phone (a photo, a document), as fetch's FormData takes it. */
export interface LocalFile {
  uri: string;
  name: string;
  type: string;
}

/** Upload one file to a task (multipart "file"); the usual sign-in refresh applies. */
export async function apiUpload<T = any>(path: string, file: LocalFile, fields: Record<string, string> = {}): Promise<{ status: number; data: T }> {
  const doFetch = () => {
    const form = new FormData();
    form.append('file', { uri: file.uri, name: file.name, type: file.type } as unknown as Blob);
    for (const [k, v] of Object.entries(fields)) form.append(k, v);
    return rawFetch(path, {
      method: 'POST',
      headers: { Accept: 'application/json', Authorization: `Bearer ${accessToken}` },
      body: form,
      timeoutMs: 90000,
    });
  };
  let res = await doFetch();
  if (res.status === 401) {
    const ok = await refreshAccess();
    if (!ok) {
      authLostHandler?.();
      throw new ApiError(401, 'auth_lost', 'Your session has expired. Please sign in again.');
    }
    res = await doFetch();
  }
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) throw new ApiError(res.status, data?.code ?? 'error', data?.message ?? `Upload failed (${res.status}).`, data);
  return { status: res.status, data: data as T };
}

/** Diagnostics hook (set by lib/diag, which imports this file: no import cycle). Short lines, never task titles. */
let diagNote: ((level: 'error' | 'warn' | 'info', where: string, message: string) => void) | null = null;
export function setDiagNote(fn: typeof diagNote) {
  diagNote = fn;
}
