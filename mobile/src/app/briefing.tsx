import { useRouter } from 'expo-router';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { ActivityIndicator, ScrollView, StyleSheet, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { DataCard } from '@/components/DataCard';
import { Button, Chip, IconButton, Tag, Txt } from '@/components/ui';
import { ApiError, api } from '@/lib/api';
import { useLayout } from '@/lib/layout';
import { say, stopSpeaking } from '@/lib/speech';
import { colors, radius } from '@/lib/theme';
import type { DataView } from '@/lib/types';

type Kind = 'morning' | 'now' | 'evening';

export default function Briefing() {
  const router = useRouter();
  const { isTablet } = useLayout();
  const [kind, setKind] = useState<Kind>(() => (new Date().getHours() >= 17 ? 'evening' : new Date().getHours() < 11 ? 'morning' : 'now'));
  const [text, setText] = useState('');
  const [engine, setEngine] = useState('');
  const [view, setView] = useState<DataView | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async (k: Kind, speak = true) => {
    setLoading(true);
    setError('');
    try {
      const res = await api<{ text: string; engine: string; view?: DataView | null }>(`/assistant/briefing/?kind=${k}`, { timeoutMs: 45000 });
      setText(res.data.text);
      setEngine(res.data.engine);
      setView(res.data.view ?? null);
      if (speak) say(res.data.text, { interrupt: true });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not load the briefing.');
    } finally {
      setLoading(false);
    }
  }, []);

  const initialKind = useRef(kind);
  useEffect(() => {
    const kick = setTimeout(() => load(initialKind.current), 0);
    return () => {
      clearTimeout(kick);
      stopSpeaking();
    };
  }, [load]);

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }}>
      <View style={s.top}>
        <IconButton icon="chevron-back" label="Back" onPress={() => { stopSpeaking(); router.back(); }} />
        <Txt variant="title" style={{ flex: 1, textAlign: 'center', marginRight: 40 }}>Briefing</Txt>
      </View>
      <ScrollView contentContainerStyle={[{ padding: 20, paddingBottom: 60 }, isTablet && { maxWidth: 820, width: '100%', alignSelf: 'center' }]}>
        <View style={{ flexDirection: 'row', flexWrap: 'wrap' }}>
          {(['morning', 'now', 'evening'] as Kind[]).map((k) => (
            <Chip
              key={k}
              label={k === 'morning' ? 'Morning' : k === 'evening' ? 'Evening review' : 'Right now'}
              selected={kind === k}
              onPress={() => { setKind(k); load(k); }}
            />
          ))}
        </View>
        <View style={s.paper}>
          {loading ? (
            <View style={{ alignItems: 'center', paddingVertical: 40 }}>
              <ActivityIndicator color={colors.gold} />
              <Txt variant="small" style={{ marginTop: 10 }}>Preparing your briefing…</Txt>
            </View>
          ) : error ? (
            <Txt variant="body" style={{ color: colors.danger }}>{error}</Txt>
          ) : (
            <>
              <Txt style={s.text}>{text}</Txt>
              {engine ? <Tag label={engine === 'ai' ? 'Written by JARVIS AI from your live tasks' : 'Summary (AI offline)'} color={colors.inkSoft} /> : null}
            </>
          )}
        </View>
        <View style={{ flexDirection: 'row', marginTop: 16 }}>
          <Button title="Stop" kind="secondary" icon="stop" onPress={stopSpeaking} style={{ flex: 1, marginRight: 8 }} />
          <Button title="Read aloud" icon="volume-high-outline" onPress={() => say(text, { interrupt: true })} disabled={!text} style={{ flex: 1.4 }} />
        </View>
        {view && !loading ? (
          <View style={{ marginTop: 18 }}>
            <Txt variant="label" style={{ marginBottom: 8 }}>The day, as data</Txt>
            <DataCard view={view} extraText={text} />
          </View>
        ) : null}
      </ScrollView>
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  top: { flexDirection: 'row', alignItems: 'center', paddingHorizontal: 12, paddingTop: 6 },
  paper: {
    marginTop: 14,
    backgroundColor: colors.surface,
    borderRadius: radius.lg,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.line,
    borderTopWidth: 3,
    borderTopColor: colors.gold,
    padding: 20,
  },
  text: { fontSize: 17, lineHeight: 27, color: colors.text, marginBottom: 14 },
});
