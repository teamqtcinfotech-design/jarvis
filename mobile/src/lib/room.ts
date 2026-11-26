/**
 * The room before JARVIS speaks (his ask, 26 Nov 2026): the phone listens for a moment; several voices or steady
 * noise means he is busy, so everything but the urgent waits and nothing is said. The measurement is the always-on
 * part's (modules/jarvis-wake: the wake-word microphone, the live line, or a short recording of its own); without
 * it (an older build, or the switch off) the room counts as quiet, as it always did.
 */
import { wakeRoomCheck, wakeRoomCheckSupported } from './wake';

const CACHE_MS = 8_000; // one measurement serves everything that wants to speak within this long
let enabled = true;
let last = { at: 0, busy: false };

export function setRoomCheckEnabled(on: boolean) {
  enabled = on;
}

export function roomCheckSupported(): boolean {
  return wakeRoomCheckSupported();
}

/** True when the room is busy right now (never throws; false when it cannot be measured). */
export async function roomBusy(): Promise<boolean> {
  if (!enabled || !wakeRoomCheckSupported()) return false;
  const now = Date.now();
  if (now - last.at < CACHE_MS) return last.busy;
  const r = await wakeRoomCheck(1500).catch(() => null);
  last = { at: Date.now(), busy: !!r?.busy };
  return last.busy;
}
