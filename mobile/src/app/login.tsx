/**
 * Sign in. For now with a username and password (see PHONE_SIGN_IN). The phone sign-in is built and waits: the
 * number, then the 6-digit SMS code (Firebase sends and checks it on the phone; on most phones the code is read by
 * itself); the app on this phone must carry it (2.7.0 and later).
 */
import { Redirect } from 'expo-router';
import React, { useEffect, useState } from 'react';
import { KeyboardAvoidingView, Platform, Pressable, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { Button, Icon, Txt } from '@/components/ui';
import { ApiError } from '@/lib/api';
import { DEFAULT_SERVER } from '@/lib/config';
import { useSession } from '@/lib/session';
import { colors, fonts, radius, shadow } from '@/lib/theme';
import { otpAwaitAuto, otpCancel, otpConfirm, otpStart, otpSupported } from '@/lib/wake';

const COUNTRY = '+91';

/** The phone sign-in is switched off for now: Firebase will not send the SMS until the project is on its paid plan
 *  (and India is allowed as a region). Until then every phone signs in with the username and password (the MD's
 *  test account among them). Set this true to bring the phone sign-in back; PhoneLogin below is complete. */
const PHONE_SIGN_IN = false;

/** "98765 43210" / "098765-43210" / "+91 98765 43210" -> "+919876543210"; "" when it is not a number. */
function fullNumber(raw: string): string {
  const digits = raw.replace(/\D/g, '');
  if (raw.trim().startsWith('+')) return digits.length >= 8 && digits.length <= 15 ? `+${digits}` : '';
  if (digits.length === 10) return `${COUNTRY}${digits}`;
  if (digits.length === 11 && digits.startsWith('0')) return `${COUNTRY}${digits.slice(1)}`;
  if (digits.length === 12 && digits.startsWith('91')) return `+${digits}`;
  return '';
}

export default function Login() {
  const { user } = useSession();
  if (user) return <Redirect href="/" />;
  return PHONE_SIGN_IN && otpSupported ? <PhoneLogin /> : <PasswordLogin />;
}

function PhoneLogin() {
  const { signInWithPhone } = useSession();
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [step, setStep] = useState<'phone' | 'code'>('phone');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [seconds, setSeconds] = useState(0); // until "Send again" is offered
  const [sentTo, setSentTo] = useState('');

  useEffect(() => {
    if (!seconds) return undefined;
    const t = setTimeout(() => setSeconds((n) => n - 1), 1000);
    return () => clearTimeout(t);
  }, [seconds]);
  useEffect(() => () => otpCancel(), []);

  const finish = async (idToken: string) => {
    try {
      await signInWithPhone(DEFAULT_SERVER, idToken);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not sign in.');
      setBusy(false);
    }
  };

  const sendCode = async () => {
    const full = fullNumber(phone);
    if (!full) {
      setError('Enter the 10-digit mobile number.');
      return;
    }
    setBusy(true);
    setError('');
    try {
      setSentTo(full);
      const res = await otpStart(full);
      if (res.status === 'verified' && res.id_token) {
        await finish(res.id_token); // the phone read the code itself
        return;
      }
      setStep('code');
      setSeconds(45);
      setBusy(false);
      // The phone may still read the SMS by itself while the code box is open.
      otpAwaitAuto().then((auto) => {
        if (auto.status === 'verified' && auto.id_token) {
          setBusy(true);
          finish(auto.id_token).catch(() => undefined);
        }
      }).catch(() => undefined);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'The code could not be sent.');
      setBusy(false);
    }
  };

  const confirm = async () => {
    if (code.trim().length < 6) {
      setError('Enter the 6-digit code from the SMS.');
      return;
    }
    setBusy(true);
    setError('');
    try {
      const res = await otpConfirm(code);
      if (res.status === 'verified' && res.id_token) await finish(res.id_token);
      else {
        setError('The code could not be checked.');
        setBusy(false);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'That code is wrong.');
      setBusy(false);
    }
  };

  return (
    <Shell>
      <View style={s.card}>
        {step === 'phone' ? (
          <>
            <Txt variant="label" style={s.label}>Mobile number</Txt>
            <View style={s.phoneRow}>
              <View style={s.country}><Txt variant="heading">{COUNTRY}</Txt></View>
              <TextInput
                value={phone}
                onChangeText={setPhone}
                keyboardType="phone-pad"
                autoComplete="tel"
                textContentType="telephoneNumber"
                maxLength={16}
                placeholder="98765 43210"
                placeholderTextColor={colors.faint}
                style={[s.input, { flex: 1, marginTop: 0 }]}
                onSubmitEditing={sendCode}
                editable={!busy}
              />
            </View>
            <Txt variant="caption" style={{ marginTop: 8 }}>The number the MD added for you on the Team page. A 6-digit code comes by SMS.</Txt>
            {error ? <Txt variant="small" style={{ color: colors.danger, marginTop: 12 }}>{error}</Txt> : null}
            <Button title="Send the code" icon="chatbox-ellipses-outline" onPress={sendCode} loading={busy} disabled={!fullNumber(phone)} style={{ marginTop: 20 }} />
          </>
        ) : (
          <>
            <Txt variant="label" style={s.label}>Code sent to {sentTo}</Txt>
            <TextInput
              value={code}
              onChangeText={(t) => setCode(t.replace(/\D/g, '').slice(0, 6))}
              keyboardType="number-pad"
              autoComplete="sms-otp"
              textContentType="oneTimeCode"
              maxLength={6}
              placeholder="6-digit code"
              placeholderTextColor={colors.faint}
              style={[s.input, s.codeInput]}
              onSubmitEditing={confirm}
              editable={!busy}
              autoFocus
            />
            {error ? <Txt variant="small" style={{ color: colors.danger, marginTop: 12 }}>{error}</Txt> : null}
            <Button title="Sign in" icon="log-in-outline" onPress={confirm} loading={busy} disabled={code.length < 6} style={{ marginTop: 20 }} />
            <View style={{ flexDirection: 'row', justifyContent: 'space-between', marginTop: 14 }}>
              <Pressable onPress={() => { otpCancel(); setStep('phone'); setCode(''); setError(''); }} hitSlop={8}>
                <Txt variant="small" style={{ color: colors.inkSoft }}>Change number</Txt>
              </Pressable>
              <Pressable onPress={sendCode} disabled={seconds > 0 || busy} hitSlop={8}>
                <Txt variant="small" style={{ color: seconds > 0 ? colors.faint : colors.gold }}>{seconds > 0 ? `Send again in ${seconds}s` : 'Send again'}</Txt>
              </Pressable>
            </View>
          </>
        )}
      </View>
      <Txt variant="caption" style={{ color: colors.faint, marginTop: 18, textAlign: 'center' }}>
        Only numbers on the team can sign in.
      </Txt>
    </Shell>
  );
}

/** Username and password: the way in while the phone sign-in is switched off (and for a phone without it). */
function PasswordLogin() {
  const { signIn } = useSession();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const submit = async () => {
    setBusy(true);
    setError('');
    try {
      await signIn(DEFAULT_SERVER, username, password);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not sign in.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Shell>
      <View style={s.card}>
        <Txt variant="label" style={s.label}>Username</Txt>
        <TextInput value={username} onChangeText={setUsername} autoCapitalize="none" autoCorrect={false} style={s.input} />
        <Txt variant="label" style={s.label}>Password</Txt>
        <TextInput value={password} onChangeText={setPassword} secureTextEntry style={s.input} onSubmitEditing={submit} />
        {error ? <Txt variant="small" style={{ color: colors.danger, marginTop: 12 }}>{error}</Txt> : null}
        <Button title="Sign in" icon="log-in-outline" onPress={submit} loading={busy} disabled={!username.trim() || !password} style={{ marginTop: 20 }} />
      </View>
      <Txt variant="caption" style={{ color: colors.faint, marginTop: 18, textAlign: 'center' }}>
        Only invited accounts can sign in.
      </Txt>
    </Shell>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }}>
      <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
        <ScrollView contentContainerStyle={s.wrap} keyboardShouldPersistTaps="handled">
          <View style={s.crest}>
            <Icon name="shield-checkmark-outline" size={34} color={colors.gold} />
          </View>
          <Txt style={s.brand}>JARVIS</Txt>
          <Txt variant="small" style={{ color: colors.muted, marginTop: 2 }}>Private executive assistant</Txt>
          {children}
        </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  wrap: { flexGrow: 1, alignItems: 'center', justifyContent: 'center', padding: 24 },
  crest: { width: 76, height: 76, borderRadius: 38, borderWidth: 1, borderColor: colors.gold, backgroundColor: colors.goldSoft, alignItems: 'center', justifyContent: 'center' },
  brand: { fontFamily: fonts.serif, fontSize: 28, fontWeight: '700', lineHeight: 40, letterSpacing: 3, paddingLeft: 4, textAlign: 'center', color: colors.ink, marginTop: 16 },
  card: { alignSelf: 'stretch', backgroundColor: colors.surface, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line, padding: 20, marginTop: 30, ...shadow },
  label: { marginTop: 12, color: colors.inkSoft },
  phoneRow: { flexDirection: 'row', alignItems: 'center', marginTop: 6 },
  country: { paddingHorizontal: 12, paddingVertical: 11, borderWidth: 1, borderColor: colors.line, borderRadius: radius.md, backgroundColor: colors.bg, marginRight: 8 },
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
  codeInput: { fontSize: 24, letterSpacing: 8, textAlign: 'center' },
});
