import * as LocalAuthentication from 'expo-local-authentication';
import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { AppState } from 'react-native';

import * as apiClient from './api';
import { setReplyLang } from './lang';
import type { SessionUser } from './types';
import { pushForget, pushToken, wakeStop } from './wake';

const LOCK_AFTER_MS = 60_000; // re-lock after 1 minute in the background

interface SessionValue {
  ready: boolean;
  user: SessionUser | null;
  server: string;
  locked: boolean;
  biometricAvailable: boolean;
  signIn: (server: string, username: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
  unlock: () => Promise<boolean>;
}

const SessionContext = createContext<SessionValue | null>(null);

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [ready, setReady] = useState(false);
  const [user, setUser] = useState<SessionUser | null>(null);
  const [server, setServer] = useState('');
  const [locked, setLocked] = useState(false);
  const [biometricAvailable, setBiometricAvailable] = useState(false);
  const backgroundedAt = useRef<number | null>(null);

  useEffect(() => {
    (async () => {
      const [stored, hasHw, enrolled] = await Promise.all([
        apiClient.loadStoredSession(),
        LocalAuthentication.hasHardwareAsync(),
        LocalAuthentication.isEnrolledAsync(),
      ]);
      const bio = hasHw && enrolled;
      setBiometricAvailable(bio);
      setServer(stored.server);
      setUser(stored.user);
      setReplyLang(stored.user?.language);
      setLocked(Boolean(stored.user) && bio);
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
            address_hi: r.data.address_hi, language: r.data.language };
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

  useEffect(() => {
    const sub = AppState.addEventListener('change', (state) => {
      if (state === 'background') backgroundedAt.current = Date.now();
      if (state === 'active' && backgroundedAt.current && user && biometricAvailable) {
        if (Date.now() - backgroundedAt.current > LOCK_AFTER_MS) setLocked(true);
        backgroundedAt.current = null;
      }
    });
    return () => sub.remove();
  }, [user, biometricAvailable]);

  const signIn = useCallback(async (srv: string, username: string, password: string) => {
    const u = await apiClient.login(srv, username, password);
    setServer(apiClient.currentServer());
    setUser(u);
    setLocked(false);
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
    setLocked(false);
  }, []);

  const unlock = useCallback(async () => {
    const result = await LocalAuthentication.authenticateAsync({
      promptMessage: 'Unlock JARVIS',
      cancelLabel: 'Cancel',
      disableDeviceFallback: false,
    });
    if (result.success) setLocked(false);
    return result.success;
  }, []);

  return (
    <SessionContext.Provider value={{ ready, user, server, locked, biometricAvailable, signIn, signOut, unlock }}>
      {children}
    </SessionContext.Provider>
  );
}

export function useSession() {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error('useSession must be inside SessionProvider');
  return ctx;
}
