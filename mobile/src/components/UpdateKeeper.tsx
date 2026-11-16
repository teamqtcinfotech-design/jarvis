import { useRouter } from 'expo-router';
import * as SecureStore from 'expo-secure-store';
import { useEffect, useRef } from 'react';
import { Alert, AppState } from 'react-native';

import { useAlertEngine } from '@/lib/alertEngine';
import { installApk, onUpdates, sayHello, setUpdateBusyCheck } from '@/lib/appUpdates';
import { useConversation } from '@/lib/conversation';

const K_ASKED = 'jarvis.apkAsked';
const ASK_AGAIN_MS = 24 * 3600_000;

/**
 * Signed in: tells the server which phone this is (so it can be a test phone, and a newer APK can be offered),
 * keeps "is JARVIS busy right now?" known to the updater (no switch to a new version while talking or while an
 * alert is on screen), and offers a new APK at most once a day per version.
 */
export function UpdateKeeper() {
  const router = useRouter();
  const { listening, speaking, busy } = useConversation();
  const { current } = useAlertEngine();
  const now = useRef({ listening, speaking, busy, alert: Boolean(current) });

  useEffect(() => {
    now.current = { listening, speaking, busy, alert: Boolean(current) };
  }, [listening, speaking, busy, current]);

  useEffect(() => {
    setUpdateBusyCheck(() => now.current.listening || now.current.speaking || now.current.busy !== '' || now.current.alert);
    void sayHello(true);
    const sub = AppState.addEventListener('change', (st) => st === 'active' && void sayHello());
    return () => {
      setUpdateBusyCheck(null);
      sub.remove();
    };
  }, []);

  useEffect(() => {
    let asking = false;
    return onUpdates(async (s) => {
      if (!s.apk || asking) return;
      asking = true;
      const offer = s.apk;
      try {
        const raw = await SecureStore.getItemAsync(K_ASKED).catch(() => null);
        const last = raw ? (JSON.parse(raw) as { version: string; at: number }) : null;
        if (last && last.version === offer.version && Date.now() - last.at < ASK_AGAIN_MS) return;
        await SecureStore.setItemAsync(K_ASKED, JSON.stringify({ version: offer.version, at: Date.now() })).catch(() => undefined);
        Alert.alert(
          'New version of JARVIS',
          `Version ${offer.version} is ready${offer.notes ? `: ${offer.notes}` : ''}. It downloads here; then tap Install.`,
          [
            { text: 'Later', style: 'cancel' },
            {
              text: 'Install',
              onPress: () => {
                router.push('/settings');
                void installApk();
              },
            },
          ],
        );
      } finally {
        asking = false;
      }
    });
  }, [router]);

  return null;
}
