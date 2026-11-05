import { Redirect } from 'expo-router';
import React from 'react';

import { PhoneHome } from '@/components/PhoneHome';
import { TabletHome } from '@/components/TabletHome';
import { useLayout } from '@/lib/layout';
import { useSession } from '@/lib/session';
import { useSetupDone } from '@/lib/setup';
import { wakeSupported } from '@/lib/wake';

/** Home: a dashboard with the floating orb on phones; JARVIS beside the data on tablets. */
export default function Home() {
  const { user, ready } = useSession();
  const setupDone = useSetupDone();
  const { isTablet } = useLayout();
  if (!ready || setupDone === null) return null;
  if (!user) return <Redirect href="/login" />;
  // First sign-in on this phone: ask for every permission once, then switch "Hey JARVIS" on.
  if (!setupDone && wakeSupported) return <Redirect href="/setup" />;
  return isTablet ? <TabletHome user={user} /> : <PhoneHome user={user} />;
}
