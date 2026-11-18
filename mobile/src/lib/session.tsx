import React, { createContext, useCallback, useContext, useEffect, useState } from 'react';
import { AppState } from 'react-native';

import * as apiClient from './api';
import { DEFAULT_SERVER } from './config';
import { setReplyLang } from './lang';
import type { SessionUser } from './types';
import { pushForget, pushToken, wakeStop } from './wake';


interface SessionValue {
  ready: boolean;
  user: SessionUser | null;
  server: string;
  signIn: (server: string, username: string, password: string) => Promise<void>;
  /** Sign in by phone number: Firebase's proof from lib/wake otpStart / otpConfirm. */
  signInWithPhone: (server: string, idToken: string) => Promise<void>;
  signOut: () => Promise<void>;
}

/** This phone as the server lists it: its model and its own random id (the one app updates use). */
export async function thisDevice(): Promise<apiClient.DeviceInfo> {
  const Device = await import('expo-device');
  const { ensureDeviceId } = await import('./appUpdates');
  const name = `${Device.manufacturer ?? ''} ${Device.modelName ?? ''}`.trim().slice(0, 80) || 'Phone';
  let id = '';
  try {
    id = await ensureDeviceId();
  } catch {
    id = '';
  }
  return { device_name: name, device_id: id };
}

const SessionContext = createContext<SessionValue | null>(null);

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [ready, setReady] = useState(false);
  const [user, setUser] = useState<SessionUser | null>(null);
  const [server, setServer] = useState('');

  useEffect(() => {
    (async () => {
      // No fingerprint or face lock anywhere: the app opens straight to JARVIS.
      const stored = await apiClient.loadStoredSession();
      if (stored.user && apiClient.normalizeServer(stored.server) !== apiClient.normalizeServer(DEFAULT_SERVER)) {
        // A login made on another server (a test build was installed before this one, or the other way round):
        // this build talks only to its own server, so the old login is dropped and he signs in once more.
        await wakeStop().catch(() => undefined);
        await apiClient.logout();
        setServer(DEFAULT_SERVER);
        setUser(null);
        setReady(true);
        return;
      }
      setServer(stored.server);
      setUser(stored.user);
      setReplyLang(stored.user?.language);
      setReady(true);
    })();
  }, []);

  useEffect(() => {
    apiClient.onAuthLost(() => setUser(null));
    return () => apiClient.onAuthLost(null);
  }, []);

  // The profile can change on the server (a new name, "call me Ma'am"): re-read it at start and whenever the app
  // comes back to the front, so no sign-out is ever needed for a name to follow.
  const signedIn = Boolean(user);
  useEffect(() => {
    if (!signedIn) return;
    let cancelled = false;
    const refresh = () => {
      apiClient.api<SessionUser>('/me/')
        .then((r) => {
          if (cancelled || !r.data?.username) return;
          const next: SessionUser = { username: r.data.username, role: r.data.role, name: r.data.name, address: r.data.address,
            address_hi: r.data.address_hi, language: r.data.language, walkie_url: r.data.walkie_url ?? '',
            phone: r.data.phone ?? '', session_id: r.data.session_id ?? null };
          setReplyLang(next.language); // the language JARVIS is speaking to this person, as the server sees it
          setUser((prev) => (prev && JSON.stringify(prev) === JSON.stringify(next) ? prev : next));
          apiClient.storeUser(next).catch(() => undefined);
        })
        .catch(() => undefined);
    };
    const kick = setTimeout(refresh, 500);
    const sub = AppState.addEventListener('change', (s) => s === 'active' && refresh());
    return () => {
      cancelled = true;
      clearTimeout(kick);
      sub.remove();
    };
  }, [signedIn]);

  const signIn = useCallback(async (srv: string, username: string, password: string) => {
    const u = await apiClient.login(srv, username, password, await thisDevice());
    setServer(apiClient.currentServer());
    setUser(u);
  }, []);

  const signInWithPhone = useCallback(async (srv: string, idToken: string) => {
    const u = await apiClient.loginWithPhone(srv, idToken, await thisDevice());
    setServer(apiClient.currentServer());
    setUser(u);
  }, []);

  const signOut = useCallback(async () => {
    // This phone must hear nothing for this login any more: forget its push token on the server and locally.
    try {
      const token = await pushToken();
      if (token) await apiClient.api('/me/push-token/', { method: 'DELETE', body: { token } });
    } catch {
      // best effort; the native side still drops the token below
    }
    await pushForget();
    await wakeStop(); // the background watcher's login goes with the session
    await apiClient.logout();
    setUser(null);
  }, []);

  return (
    <SessionContext.Provider value={{ ready, user, server, signIn, signInWithPhone, signOut }}>
      {children}
    </SessionContext.Provider>
  );
}

export function useSession() {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error('useSession must be inside SessionProvider');
  return ctx;
}
