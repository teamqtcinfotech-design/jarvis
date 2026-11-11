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

export async function login(server: string, username: string, password: string): Promise<SessionUser> {
  serverUrl = normalizeServer(server);
  const res = await rawFetch('/auth/login/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ username: username.trim(), password }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = res.status === 401 ? 'Username or password is incorrect.' : body?.message ?? 'Login failed.';
    throw new ApiError(res.status, body?.code ?? 'login_failed', msg, body);
  }
  await SecureStore.setItemAsync(K_SERVER, serverUrl);
  await storeTokens(body.access, body.refresh);
  await SecureStore.setItemAsync(K_USER, JSON.stringify(body.user));
  return body.user as SessionUser;
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

/** Diagnostics hook (set by lib/diag, which imports this file: no import cycle). Short lines, never task titles. */
let diagNote: ((level: 'error' | 'warn' | 'info', where: string, message: string) => void) | null = null;
export function setDiagNote(fn: typeof diagNote) {
  diagNote = fn;
}
