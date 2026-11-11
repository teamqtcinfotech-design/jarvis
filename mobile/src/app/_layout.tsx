import { Poppins_400Regular, Poppins_500Medium, Poppins_600SemiBold, Poppins_700Bold, useFonts } from '@expo-google-fonts/poppins';
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
  // The dashboard comes first: the alert engine reads the PA's name and the MD's form of address from it.
  return (
    <DashboardProvider>
      <AlertEngineProvider>
        <ConversationProvider>
          <View style={{ flex: 1 }}>
            {stack}
            <AlertBanner />
            <HeldSheet />
          </View>
        </ConversationProvider>
      </AlertEngineProvider>
    </DashboardProvider>
  );
}

export default function RootLayout() {
  // Poppins for everything; the first screen waits the few hundred ms the fonts take, so nothing flashes.
  const [fontsLoaded, fontError] = useFonts({ Poppins_400Regular, Poppins_500Medium, Poppins_600SemiBold, Poppins_700Bold });
  if (!fontsLoaded && !fontError) return null;
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
