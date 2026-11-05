/** One server-built data view (timeline / chart / table), kept fresh like the dashboard. */
import { useCallback, useEffect, useState } from 'react';

import { api } from './api';
import { dataChanged } from './bus';
import type { DataView } from './types';

export function useDataView(kind: string | null) {
  const [view, setView] = useState<DataView | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    if (!kind) return;
    setLoading(true);
    try {
      const res = await api<DataView>(`/views/${kind}/`);
      setView(res.data);
      setError('');
    } catch {
      setError('Could not load.');
    } finally {
      setLoading(false);
    }
  }, [kind]);

  useEffect(() => {
    if (!kind) {
      const clear = setTimeout(() => setView(null), 0);
      return () => clearTimeout(clear);
    }
    const kick = setTimeout(load, 0);
    const timer = setInterval(load, 60_000);
    const unsub = dataChanged.subscribe(() => load());
    return () => {
      clearTimeout(kick);
      clearInterval(timer);
      unsub();
    };
  }, [kind, load]);

  return { view, loading, error, reload: load };
}
