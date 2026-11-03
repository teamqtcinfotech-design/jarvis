import { useCallback, useEffect, useRef, useState } from 'react';
import { AppState } from 'react-native';

import { ApiError, api } from './api';
import { dataChanged } from './bus';
import type { Dashboard } from './types';

export function useDashboard() {
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
    const timer = setInterval(() => load(false), 45_000);
    const unsub = dataChanged.subscribe(() => load(false));
    const sub = AppState.addEventListener('change', (s) => s === 'active' && load(true));
    return () => {
      clearTimeout(kick);
      clearInterval(timer);
      unsub();
      sub.remove();
    };
  }, [load]);

  return { data, loading, error, updatedAt, reload: load };
}
