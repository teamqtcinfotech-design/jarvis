/**
 * Said with no connection: kept on the phone and sent when the server is back (Nov 2026). JARVIS says so at once
 * ("I'll send it when we're back online"), tries again every 20 seconds and whenever the app comes back to the
 * front, and announces each one as it goes through. Only plain new instructions are kept: an answer to a question or
 * a waiting card belongs to that moment and is not replayed later.
 */
import { File, Paths } from 'expo-file-system';

export interface QueuedSaying {
  id: string;
  text: string;
  at: string;
  tries: number;
}

const MAX = 20;
const FILE = () => new File(Paths.document, 'jarvis-offline-queue.json');
let items: QueuedSaying[] | null = null;

function load(): QueuedSaying[] {
  if (items) return items;
  try {
    const f = FILE();
    const parsed = f.exists ? JSON.parse(f.textSync()) : [];
    items = Array.isArray(parsed) ? parsed.filter((q) => q && typeof q.text === 'string').slice(-MAX) : [];
  } catch {
    items = [];
  }
  return items;
}

function persist() {
  try {
    FILE().write(JSON.stringify(load()));
  } catch {
    // the list in memory still works until the app closes
  }
}

export function queued(): QueuedSaying[] {
  return [...load()];
}

export function enqueue(text: string): QueuedSaying {
  const q = { id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, text: text.trim().slice(0, 1000), at: new Date().toISOString(), tries: 0 };
  load().push(q);
  if (load().length > MAX) items = load().slice(-MAX);
  persist();
  return q;
}

export function remove(id: string) {
  items = load().filter((q) => q.id !== id);
  persist();
}

/**
 * Send what is waiting, oldest first, through `send`. Stops at the first one that still cannot reach the server
 * (the rest wait with it); one that the server refuses for its own reasons is dropped so it never blocks the line.
 * Returns the sayings that went through.
 */
export async function flush(send: (q: QueuedSaying) => Promise<void>, stillOffline: (e: unknown) => boolean): Promise<QueuedSaying[]> {
  const done: QueuedSaying[] = [];
  for (const q of queued()) {
    try {
      await send(q);
      remove(q.id);
      done.push(q);
    } catch (e) {
      if (stillOffline(e)) {
        q.tries += 1;
        persist();
        break;
      }
      remove(q.id); // refused for good: not worth a second try
    }
  }
  return done;
}
