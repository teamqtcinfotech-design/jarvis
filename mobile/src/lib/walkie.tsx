/**
 * The walkie-talkie on the app side: what the native engine reports (who is around, who holds the floor, who is
 * talking to me, my own voice level) and the few things the screens ask of it (hold to talk, reply, quiet).
 *
 * The engine itself lives in the always-on module (WalkieEngine.kt): the line stays open and incoming talks play
 * with the app closed; this provider only mirrors its state while the app is open.
 */
import { router } from 'expo-router';
import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { AppState } from 'react-native';

import { api } from './api';
import { usePrefs } from './prefs';
import { useSession } from './session';
import { onWalkie, walkieConsumePending, walkieEnd, walkieQuiet, walkieRefresh, walkieStart, walkieSupported, walkieTalk } from './wake';

export type WalkieStatus = 'available' | 'quiet' | 'off' | 'reach';

export interface WalkiePerson {
  id: number;
  name: string;
  title: string;
  role: string;
  status: WalkieStatus;
  quiet_until: string;
  talking: boolean;
}

export type TalkState = 'idle' | 'connecting' | 'ready' | 'talking' | 'listening' | 'busy';

interface WalkieValue {
  supported: boolean;
  enabled: boolean;
  connected: boolean;
  people: WalkiePerson[];
  you: number;
  /** The talk I am in (speaking, listening, or between turns), or ''. */
  talk: string;
  /** What the big button should say. */
  state: TalkState;
  /** Who is talking to me right now (or spoke last in this talk). */
  incomingFrom: string;
  /** Everyone in the current talk (ids), so a reply reaches all of them. */
  members: number[];
  /** My own voice level while talking, 0..1. */
  level: number;
  /** A short line for the screen ("Mansi is quiet till 3:30", "Nikki is busy"). */
  note: string;
  /** The people I talked to last (for the home button's long-press). */
  lastTo: number[];
  talkStart: (to: number[]) => void;
  reply: () => void;
  talkEnd: () => void;
  quiet: (minutes: number) => void;
  refresh: () => void;
}

const WalkieContext = createContext<WalkieValue | null>(null);

const parse = (s: string): any => {
  try {
    return JSON.parse(s || '{}');
  } catch {
    return {};
  }
};

export function WalkieProvider({ children }: { children: React.ReactNode }) {
  const { user } = useSession();
  const { prefs } = usePrefs();
  const [connected, setConnected] = useState(false);
  const [people, setPeople] = useState<WalkiePerson[]>([]);
  const [you, setYou] = useState(0);
  const [talk, setTalk] = useState('');
  const [state, setState] = useState<TalkState>('idle');
  const [incomingFrom, setIncomingFrom] = useState('');
  const [members, setMembers] = useState<number[]>([]);
  const [level, setLevel] = useState(0);
  const [note, setNote] = useState('');
  const [lastTo, setLastTo] = useState<number[]>([]);
  const holding = useRef(false);
  const noteTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const say = useCallback((text: string, ms = 4000) => {
    setNote(text);
    if (noteTimer.current) clearTimeout(noteTimer.current);
    noteTimer.current = setTimeout(() => setNote(''), ms);
  }, []);

  // The line: open while the switch is on (the engine keeps it open with the app closed too).
  useEffect(() => {
    if (!user || !walkieSupported) return;
    let cancelled = false;
    (async () => {
      let url = user.walkie_url ?? '';
      if (!url && prefs.walkieEnabled) {
        try {
          url = (await api<{ walkie_url?: string }>('/me/')).data.walkie_url ?? '';
        } catch {
          url = '';
        }
      }
      if (cancelled) return;
      await walkieStart(url, prefs.walkieEnabled && !!url);
      if (prefs.walkieEnabled) walkieRefresh();
    })();
    return () => {
      cancelled = true;
    };
  }, [user, prefs.walkieEnabled]);

  // What the engine reports.
  useEffect(() => {
    if (!walkieSupported) return;
    const off = onWalkie((e) => {
      const d = parse(e.data);
      switch (e.event) {
        case 'state':
          setConnected(!!d.connected);
          if (!d.connected) setState((st) => (st === 'talking' || st === 'listening' ? 'idle' : st));
          break;
        case 'people':
          setPeople(Array.isArray(d.people) ? d.people : []);
          if (d.you) setYou(d.you);
          break;
        case 'floor':
          setTalk(String(d.talk || ''));
          setMembers(Array.isArray(d.to) ? d.to : []);
          setState('talking');
          if (Array.isArray(d.waking) && d.waking.length) say(`Waking ${d.waking.join(', ')}…`, 6000);
          if (Array.isArray(d.quiet) && d.quiet.length) say(`${d.quiet.join(', ')} ${d.quiet.length === 1 ? 'is' : 'are'} quiet; they'll be told you tried.`, 6000);
          break;
        case 'busy':
          holding.current = false;
          setState('busy');
          say(`${d.who || 'Someone'} is talking… wait.`);
          setTimeout(() => setState((st) => (st === 'busy' ? 'ready' : st)), 1500);
          break;
        case 'refused':
          holding.current = false;
          setState('ready');
          say(String(d.why || "Couldn't talk."));
          break;
        case 'cut':
          holding.current = false;
          setState('ready');
          say('A talk is at most a minute; press again to go on.');
          break;
        case 'incoming':
          setTalk(String(d.talk || ''));
          setMembers(Array.isArray(d.to) ? d.to : []);
          setIncomingFrom(String(d.from?.name || ''));
          setState('listening');
          break;
        case 'done':
          setState((st) => (st === 'listening' ? 'ready' : st));
          break;
        case 'ended':
          setTalk('');
          setMembers([]);
          setIncomingFrom('');
          setState((st) => (st === 'talking' ? st : 'idle'));
          break;
        case 'level':
          setLevel(Number(d.level) || 0);
          break;
        case 'error':
          say(d.where === 'mic' ? 'The microphone could not open.' : 'The speaker could not open.');
          break;
        default:
          break;
      }
    });
    return off;
  }, [say]);

  // Coming back to the front: fresh people, and the incoming card that may have opened the app.
  useEffect(() => {
    if (!walkieSupported) return;
    const check = () => {
      walkieRefresh();
      const pending = walkieConsumePending();
      if (pending) {
        setTalk(pending);
        try {
          router.push('/walkie');
        } catch {
          // not mounted yet
        }
      }
    };
    const kick = setTimeout(check, 500);
    const sub = AppState.addEventListener('change', (s) => s === 'active' && check());
    return () => {
      clearTimeout(kick);
      sub.remove();
    };
  }, []);

  const talkStart = useCallback((to: number[]) => {
    if (!to.length || holding.current) return;
    holding.current = true;
    setLastTo(to);
    setState(connected ? 'talking' : 'connecting');
    walkieTalk(to, null);
  }, [connected]);

  const reply = useCallback(() => {
    if (!talk || holding.current) return;
    holding.current = true;
    setState('talking');
    walkieTalk([], talk);
  }, [talk]);

  const talkEnd = useCallback(() => {
    if (!holding.current) return;
    holding.current = false;
    walkieEnd();
    setLevel(0);
    setState((st) => (st === 'talking' || st === 'connecting' ? 'ready' : st));
  }, []);

  const quiet = useCallback((minutes: number) => walkieQuiet(minutes), []);
  const refresh = useCallback(() => walkieRefresh(), []);

  const value = useMemo<WalkieValue>(() => ({
    supported: walkieSupported, enabled: prefs.walkieEnabled, connected, people, you, talk, state, incomingFrom, members, level, note, lastTo,
    talkStart, reply, talkEnd, quiet, refresh,
  }), [prefs.walkieEnabled, connected, people, you, talk, state, incomingFrom, members, level, note, lastTo, talkStart, reply, talkEnd, quiet, refresh]);

  return <WalkieContext.Provider value={value}>{children}</WalkieContext.Provider>;
}

export function useWalkie() {
  const ctx = useContext(WalkieContext);
  if (!ctx) throw new Error('useWalkie must be inside WalkieProvider');
  return ctx;
}
