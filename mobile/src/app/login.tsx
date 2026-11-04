import { Redirect } from 'expo-router';
import React, { useState } from 'react';
import { KeyboardAvoidingView, Platform, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { Button, Chip, Icon, Txt } from '@/components/ui';
import { ApiError, normalizeServer } from '@/lib/api';
import { DEFAULT_SERVER, LOCAL_SERVER } from '@/lib/config';
import { useSession } from '@/lib/session';
import { colors, fonts, radius, shadow } from '@/lib/theme';

export default function Login() {
  const { user, signIn, server: savedServer } = useSession();
  // The last address used on this phone is kept (live server or laptop); a fresh install gets the live server.
  const [server, setServer] = useState(savedServer || DEFAULT_SERVER);
  const current = normalizeServer(server);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  if (user) return <Redirect href="/" />;

  const submit = async () => {
    setBusy(true);
    setError('');
    try {
      await signIn(server, username, password);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not sign in.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }}>
      <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
        <ScrollView contentContainerStyle={s.wrap} keyboardShouldPersistTaps="handled">
          <View style={s.crest}>
            <Icon name="shield-checkmark-outline" size={34} color={colors.gold} />
          </View>
          <Txt style={s.brand}>JARVIS</Txt>
          <Txt variant="small" style={{ color: colors.muted, marginTop: 2 }}>Private executive assistant</Txt>

          <View style={s.card}>
            <Txt variant="label" style={s.label}>Server address</Txt>
            <TextInput
              value={server}
              onChangeText={setServer}
              placeholder={DEFAULT_SERVER.replace(/^https?:\/\//, '')}
              placeholderTextColor={colors.faint}
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="url"
              style={s.input}
            />
            <View style={s.servers}>
              <Chip label="Live server" icon="cloud-outline" selected={current === DEFAULT_SERVER} onPress={() => setServer(DEFAULT_SERVER)} />
              <Chip label="Laptop over USB" icon="laptop-outline" selected={current === LOCAL_SERVER} onPress={() => setServer(LOCAL_SERVER)} />
            </View>
            <Txt variant="label" style={s.label}>Username</Txt>
            <TextInput value={username} onChangeText={setUsername} autoCapitalize="none" autoCorrect={false} style={s.input} />
            <Txt variant="label" style={s.label}>Password</Txt>
            <TextInput value={password} onChangeText={setPassword} secureTextEntry style={s.input} onSubmitEditing={submit} />
            {error ? <Txt variant="small" style={{ color: colors.danger, marginTop: 12 }}>{error}</Txt> : null}
            <Button
              title="Sign in"
              icon="log-in-outline"
              onPress={submit}
              loading={busy}
              disabled={!server.trim() || !username.trim() || !password}
              style={{ marginTop: 20 }}
            />
          </View>
          <Txt variant="caption" style={{ color: colors.faint, marginTop: 18, textAlign: 'center' }}>
            Only enrolled MD and PA accounts can sign in.
          </Txt>
        </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  wrap: { flexGrow: 1, alignItems: 'center', justifyContent: 'center', padding: 24 },
  crest: { width: 76, height: 76, borderRadius: 38, borderWidth: 1, borderColor: colors.gold, backgroundColor: colors.goldSoft, alignItems: 'center', justifyContent: 'center' },
  // lineHeight must fit the 32px serif (the body default of 21 clipped the "J"); paddingLeft balances the
  // trailing letter-spacing so the word is centred and the J's hook has room.
  brand: { fontFamily: fonts.serif, fontSize: 32, lineHeight: 46, letterSpacing: 8, paddingLeft: 10, paddingRight: 2, textAlign: 'center', color: colors.ink, marginTop: 16 },
  card: { alignSelf: 'stretch', backgroundColor: colors.surface, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line, padding: 20, marginTop: 30, ...shadow },
  label: { marginTop: 12, color: colors.inkSoft },
  servers: { flexDirection: 'row', flexWrap: 'wrap', marginTop: 8 },
  input: {
    backgroundColor: colors.bg,
    borderWidth: 1,
    borderColor: colors.line,
    borderRadius: radius.md,
    paddingHorizontal: 14,
    paddingVertical: 11,
    fontSize: 16,
    color: colors.text,
    marginTop: 6,
  },
});
