/**
 * What went wrong on this phone, kept in a short ring and sent to the server on request ("Send diagnostics" in
 * Settings) or by itself after a crash. Short lines only: where, what, when. Never task titles or spoken text.
 */
import Constants from 'expo-constants';
import { File, Paths } from 'expo-file-system';

import { api, setDiagNote } from './api';

export interface DiagEvent {
  level: 'error' | 'warn' | 'info';
  where: string;
  message: string;
  at: string;
}

const MAX = 200;
let events: DiagEvent[] = [];
let loaded = false;
let dirty = false;
const FILE = () => new File(Paths.cache, 'jarvis-diag.json');

function load() {
  if (loaded) return;
  loaded = true;
  try {
    const f = FILE();
    if (f.exists) {
      const parsed = JSON.parse(f.textSync());
      if (Array.isArray(parsed)) events = parsed.slice(-MAX);
    }
  } catch {
    events = [];
  }
}

function persist() {
  if (!dirty) return;
  dirty = false;
  try {
    FILE().write(JSON.stringify(events.slice(-MAX)));
  } catch {
    // the cache may be unavailable; the ring in memory still works
  }
}

let persistTimer: ReturnType<typeof setTimeout> | null = null;

/** Note one thing that went wrong. `message` is cut short and must never carry a task title. */
export function note(level: DiagEvent['level'], where: string, message: string) {
  load();
  const line = String(message ?? '').replace(/\s+/g, ' ').slice(0, 300);
  if (!line) return;
  events.push({ level, where: where.slice(0, 80), message: line, at: new Date().toISOString() });
  if (events.length > MAX) events = events.slice(-MAX);
  dirty = true;
  if (persistTimer) clearTimeout(persistTimer);
  persistTimer = setTimeout(persist, 1500);
}

export function recent(): DiagEvent[] {
  load();
  return [...events];
}

/** Send everything kept so far to the server (the same person can read it back there), then clear the ring. */
export async function send(): Promise<number> {
  load();
  const batch = events.slice(-100);
  if (!batch.length) return 0;
  await api('/diagnostics/', { method: 'POST', body: { app_version: Constants.expoConfig?.version ?? '', events: batch } });
  events = [];
  dirty = true;
  persist();
  return batch.length;
}

/** Catch what nobody else caught: a crash is noted before the app goes down, and sent on the next start. */
export function install() {
  load();
  setDiagNote(note);
  const utils = (globalThis as any).ErrorUtils;
  if (utils?.setGlobalHandler) {
    const previous = utils.getGlobalHandler?.();
    utils.setGlobalHandler((error: unknown, fatal?: boolean) => {
      note('error', fatal ? 'crash' : 'uncaught', error instanceof Error ? `${error.name}: ${error.message}` : String(error));
      persist();
      previous?.(error, fatal);
    });
  }
  // A crash last time: send it now, quietly.
  if (events.some((e) => e.where === 'crash')) send().catch(() => undefined);
}
