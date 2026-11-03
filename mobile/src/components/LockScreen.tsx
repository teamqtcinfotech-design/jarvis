import React, { useEffect } from 'react';
import { StyleSheet, View } from 'react-native';

import { useSession } from '@/lib/session';
import { colors, fonts } from '@/lib/theme';

import { Button, Icon, Txt } from './ui';

export function LockScreen() {
  const { unlock, signOut, user } = useSession();

  useEffect(() => {
    unlock();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <View style={s.wrap}>
      <View style={s.crest}>
        <Icon name="shield-checkmark-outline" size={38} color={colors.gold} />
      </View>
      <Txt style={s.brand}>JARVIS</Txt>
      <Txt variant="small" style={{ color: '#C9CFD8', marginTop: 6 }}>Locked · {user?.role === 'MD' ? 'Managing Director' : user?.name}</Txt>
      <Button title="Unlock" icon="finger-print" kind="gold" onPress={unlock} style={{ marginTop: 36, width: 220 }} />
      <Button title="Sign out" kind="ghost" onPress={signOut} style={{ marginTop: 8 }} />
    </View>
  );
}

const s = StyleSheet.create({
  wrap: { position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, backgroundColor: colors.ink, alignItems: 'center', justifyContent: 'center', zIndex: 100 },
  crest: { width: 84, height: 84, borderRadius: 42, borderWidth: 1, borderColor: colors.gold, alignItems: 'center', justifyContent: 'center' },
  brand: { fontFamily: fonts.serif, fontSize: 32, letterSpacing: 8, color: colors.white, marginTop: 18 },
});
