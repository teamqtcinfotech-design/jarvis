/**
 * The live JARVIS server (the VPS). It is pre-filled on the login screen and can still be changed there.
 * To point a build at another server, set EXPO_PUBLIC_JARVIS_SERVER before building.
 */
export const DEFAULT_SERVER = process.env.EXPO_PUBLIC_JARVIS_SERVER || 'http://201.18.193.128:8088';

/** Addresses that only worked while testing over USB from the laptop. */
export function isDevServer(url: string): boolean {
  return /^(https?:\/\/)?(localhost|127\.0\.0\.1|10\.0\.2\.2)(:|\/|$)/i.test(url.trim());
}
