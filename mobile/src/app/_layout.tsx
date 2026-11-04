import { Stack } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import React from 'react';
import { View } from 'react-native';
import { SafeAreaProvider } from 'react-native-safe-area-context';

import { AlertBanner } from '@/components/AlertBanner';
import { HeldSheet } from '@/components/HeldSheet';
import { LockScreen } from '@/components/LockScreen';
import { AlertEngineProvider } from '@/lib/alertEngine';
import { ConversationProvider } from '@/lib/conversation';
import { PrefsProvider } from '@/lib/prefs';
import { SessionProvider, useSession } from '@/lib/session';
import { colors } from '@/lib/theme';
import { DashboardProvider } from '@/lib/useDashboard';

function Shell() {
  const { user, locked } = useSession();
  const stack = (
    <Stack screenOptions={{ headerShown: false, contentStyle: { backgroundColor: colors.bg }, animation: 'fade' }} />
  );
  if (!user) return stack;
  if (locked) return <LockScreen />;
  return (
    <AlertEngineProvider>
      <DashboardProvider>
        <ConversationProvider>
          <View style={{ flex: 1 }}>
            {stack}
            <AlertBanner />
            <HeldSheet />
          </View>
        </ConversationProvider>
      </DashboardProvider>
    </AlertEngineProvider>
  );
}

export default function RootLayout() {
  return (
    <SafeAreaProvider>
      <SessionProvider>
        <PrefsProvider>
          <StatusBar style="dark" />
          <Shell />
        </PrefsProvider>
      </SessionProvider>
    </SafeAreaProvider>
  );
}
