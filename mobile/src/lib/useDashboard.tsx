import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { AppState } from 'react-native';

import { ApiError, api } from './api';
import { dataChanged } from './bus';
import type { Dashboard } from './types';
import { onPush, wakeSupported } from './wake';

// With live push on this build, a change reaches the phone by itself; the timer is only a safety net.
const REFRESH_MS = wakeSupported ? 120_000 : 45_000;

interface DashboardValue {
  data: Dashboard | null;
  loading: boolean;
  error: string;
  updatedAt: number | null;
  reload: (refreshStaff?: boolean) => Promise<void>;
}

const DashboardContext = createContext<DashboardValue | null>(null);

/** One shared copy of the task lists for the whole app (main screen, menu counts, list screens). */
export function DashboardProvider({ children }: { children: React.ReactNode }) {
  const [data, setData] = useState<Dashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const inFlight = useRef(false);

  const load = useCallback(async (refreshStaff = false) => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const res = await api<Dashboard>(`/today/${refreshStaff ? '?refresh=1' : ''}`);
      setData(res.data);
      setError('');
      setUpdatedAt(Date.now());
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not load. Pull down to retry.');
    } finally {
      inFlight.current = false;
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const kick = setTimeout(() => load(true), 0);
    const timer = setInterval(() => load(false), REFRESH_MS);
    const unsub = dataChanged.subscribe(() => load(false));
    const offPush = onPush(() => load(false)); // something changed on the server: refresh now, not in two minutes
    const sub = AppState.addEventListener('change', (s) => s === 'active' && load(true));
    return () => {
      clearTimeout(kick);
      clearInterval(timer);
      unsub();
      offPush();
      sub.remove();
    };
  }, [load]);

  return <DashboardContext.Provider value={{ data, loading, error, updatedAt, reload: load }}>{children}</DashboardContext.Provider>;
}

export function useDashboard() {
  const ctx = useContext(DashboardContext);
  if (!ctx) throw new Error('useDashboard must be inside DashboardProvider');
  return ctx;
}
