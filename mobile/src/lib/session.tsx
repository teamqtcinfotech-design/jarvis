import * as LocalAuthentication from 'expo-local-authentication';
import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { AppState } from 'react-native';

import * as apiClient from './api';
import type { SessionUser } from './types';

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
      setLocked(Boolean(stored.user) && bio);
      setReady(true);
    })();
  }, []);

  useEffect(() => {
    apiClient.onAuthLost(() => setUser(null));
    return () => apiClient.onAuthLost(null);
  }, []);

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
