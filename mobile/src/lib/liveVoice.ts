/**
 * The live conversation: one open line from this phone to Grok's realtime voice model, which is JARVIS's ears and
 * mouth and fills the form; the JARVIS server stays the brain (see backend/core/livevoice.py).
 *
 * The session (persona, facts, form tools) and a short-lived token come from the server; the native part
 * (modules/jarvis-wake LiveVoice.kt) holds the line, the microphone and the speaker. This module is the app's side:
 * open and close the line, hand the conversation the events (heard, said, a form to run), and be JARVIS's mouth
 * while the line is open (every say() goes to the line).
 */
import { api } from './api';
import { setLiveSink, type SpeakLang } from './speech';
import { type LiveEvent, liveInterrupt, liveSay, liveStart, liveStop, liveSupported, liveToolResult, onLive } from './wake';

export interface LiveSession {
  url: string;
  token: string;
  expires_at?: number;
  model: string;
  rate: number;
  idle_seconds: number;
  max_seconds: number;
  session: Record<string, unknown>;
  greeting: string;
}

export type LineState = 'off' | 'connecting' | 'open';

export interface LineHooks {
  /** What the person said (the line's own transcription); final once the model has taken the turn. */
  onHeard: (text: string, final: boolean) => void;
  /** What JARVIS said (the line's transcript of its own voice). */
  onSaid: (text: string) => void;
  /** The model filled a form: run it on the server and resolve to the tool output (JSON text with "say"). */
  onTool: (callId: string, name: string, args: Record<string, unknown>) => Promise<string>;
  onState: (state: LineState, reason: string) => void;
  onSpeaking: (on: boolean) => void;
  onListening: (on: boolean) => void;
  onLevel: (level: number) => void;
  onError: (message: string) => void;
}

let state: LineState = 'off';
let hooks: LineHooks | null = null;
let unsubscribe: (() => void) | null = null;
let openedAt = 0;
/** While a form runs on the server, the lines the app would speak are collected into the tool's answer instead. */
let collector: string[] | null = null;
/** Something to do once the line has finished speaking (closing after "bas"). */
let afterSpeech: (() => void) | null = null;
let speakingNow = false;

export const liveAvailable = liveSupported;

export function lineState(): LineState {
  return state;
}

export function lineOpen(): boolean {
  return state === 'open';
}

export function lineSpeaking(): boolean {
  return speakingNow;
}

function parse(data: string): Record<string, any> {
  try {
    return JSON.parse(data || '{}');
  } catch {
    return {};
  }
}

function handle(e: LiveEvent) {
  const d = parse(e.data);
  const h = hooks;
  if (!h) return;
  switch (e.event) {
    case 'state': {
      const s = String(d.state || '');
      if (s === 'open') {
        state = 'open';
        h.onState('open', '');
      } else if (s === 'closed') {
        const wasOpen = state !== 'off';
        state = 'off';
        setLiveSink(null);
        speakingNow = false;
        h.onSpeaking(false);
        h.onState('off', String(d.reason || ''));
        if (wasOpen) {
          const seconds = Number(d.seconds || 0) || Math.round((Date.now() - openedAt) / 1000);
          api('/assistant/live/usage/', { method: 'POST', body: { seconds }, timeoutMs: 8000 }).catch(() => undefined);
        }
        afterSpeech = null;
      } else if (s === 'connecting') {
        state = 'connecting';
        h.onState('connecting', '');
      }
      break;
    }
    case 'heard':
      h.onHeard(String(d.text || ''), !!d.final);
      break;
    case 'said':
      h.onSaid(String(d.text || ''));
      break;
    case 'tool': {
      const callId = String(d.callId || '');
      const name = String(d.name || '');
      const args = parse(String(d.arguments || '{}'));
      collector = [];
      h.onTool(callId, name, args)
        .then((output) => liveToolResult(callId, output))
        .catch((err) => liveToolResult(callId, JSON.stringify({ error: String(err?.message ?? err ?? 'failed').slice(0, 200) })))
        .finally(() => {
          collector = null;
        });
      break;
    }
    case 'speaking': {
      speakingNow = !!d.on;
      h.onSpeaking(speakingNow);
      if (!speakingNow && afterSpeech) {
        const fn = afterSpeech;
        afterSpeech = null;
        fn();
      }
      break;
    }
    case 'listening':
      h.onListening(!!d.on);
      break;
    case 'level':
      h.onLevel(Number(d.level || 0));
      break;
    case 'error':
      h.onError(String(d.message || 'The live line failed.'));
      break;
    default:
      break;
  }
}

/** Open the line: the session from the server, then the native line. Resolves false when it could not open
 *  (no live voice on this server or this build, no token, no microphone): the caller uses the usual way. */
export async function openLine(h: LineHooks, opts: { greet: boolean; bargeIn: boolean; privateOk?: boolean; greeting?: string; idleSeconds?: number }): Promise<boolean> {
  if (!liveSupported) return false;
  if (state !== 'off') closeLine();
  hooks = h;
  state = 'connecting';
  h.onState('connecting', '');
  let session: LiveSession;
  try {
    const res = await api<LiveSession>(`/assistant/live/session/${opts.privateOk === false ? '?private_ok=0' : ''}`, { timeoutMs: 15000 });
    session = res.data;
  } catch {
    state = 'off';
    h.onState('off', 'no_session');
    return false;
  }
  if (!unsubscribe) unsubscribe = onLive(handle);
  openedAt = Date.now();
  // The line is JARVIS's mouth from here: a line spoken while a form runs is the form's answer (the model reads it);
  // any other line is said by the line itself.
  setLiveSink({
    say: (text: string, _lang: SpeakLang) => {
      if (collector) collector.push(text);
      else liveSay(text);
    },
    stop: () => liveInterrupt(),
  });
  const ok = await liveStart({
    url: session.url,
    token: session.token,
    sessionJson: JSON.stringify(session.session),
    rate: session.rate,
    bargeIn: opts.bargeIn,
    bargeLevel: 1200,
    // Voice mode (the line kept open while the app is up) waits longer before closing a quiet line.
    idleSeconds: opts.idleSeconds ?? session.idle_seconds,
    maxSeconds: session.max_seconds,
    // The app's own opening line when it has one (the greeting with what has come, when JARVIS opens); else the
    // server's "Ji Sir?" after "Hey JARVIS"; nothing when he is already talking.
    greeting: opts.greeting ?? (opts.greet ? session.greeting : ''),
  });
  if (!ok) {
    state = 'off';
    setLiveSink(null);
    h.onState('off', 'failed');
    return false;
  }
  return true;
}

export function closeLine() {
  if (state === 'off') return;
  liveStop(); // the native side reports "closed", which settles the state and the usage
}

/** The text collected for the form that is running right now (what handleDraft had JARVIS say), then cleared. */
export function takeCollected(): string {
  if (!collector) return '';
  const text = collector.join(' ').trim();
  collector.length = 0;
  return text;
}

export function collecting(): boolean {
  return collector !== null;
}

export function hasCollected(): boolean {
  return !!collector && collector.length > 0;
}

/** Run `fn` once the line has finished saying what it is saying now (or at once, when it is quiet). */
export function whenQuiet(fn: () => void) {
  if (speakingNow) afterSpeech = fn;
  else fn();
}

export function sayOnLine(text: string) {
  if (state === 'open') liveSay(text);
}
