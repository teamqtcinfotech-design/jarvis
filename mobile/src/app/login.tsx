import { Redirect } from 'expo-router';
import React, { useState } from 'react';
import { KeyboardAvoidingView, Platform, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { Button, Icon, Txt } from '@/components/ui';
import { ApiError } from '@/lib/api';
import { useSession } from '@/lib/session';
import { colors, fonts, radius } from '@/lib/theme';

export default function Login() {
  const { user, signIn, server: savedServer } = useSession();
  const [server, setServer] = useState(savedServer || '');
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
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.ink }}>
      <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
        <ScrollView contentContainerStyle={s.wrap} keyboardShouldPersistTaps="handled">
          <View style={s.crest}>
            <Icon name="shield-checkmark-outline" size={34} color={colors.gold} />
          </View>
          <Txt style={s.brand}>JARVIS</Txt>
          <Txt variant="small" style={{ color: '#C9CFD8', marginTop: 6 }}>Private executive assistant</Txt>

          <View style={s.card}>
            <Txt variant="label" style={s.label}>Server address</Txt>
            <TextInput
              value={server}
              onChangeText={setServer}
              placeholder="e.g. 192.168.1.20:8000"
              placeholderTextColor={colors.faint}
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="url"
              style={s.input}
            />
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
          <Txt variant="caption" style={{ color: '#8C96A5', marginTop: 18, textAlign: 'center' }}>
            Only enrolled MD and PA accounts can sign in.
          </Txt>
        </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  wrap: { flexGrow: 1, alignItems: 'center', justifyContent: 'center', padding: 24 },
  crest: { width: 76, height: 76, borderRadius: 38, borderWidth: 1, borderColor: colors.gold, alignItems: 'center', justifyContent: 'center' },
  brand: { fontFamily: fonts.serif, fontSize: 32, letterSpacing: 8, color: colors.white, marginTop: 16 },
  card: { alignSelf: 'stretch', backgroundColor: colors.bg, borderRadius: radius.lg, padding: 20, marginTop: 30 },
  label: { marginTop: 12, color: colors.inkSoft },
  input: {
    backgroundColor: colors.surface,
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
