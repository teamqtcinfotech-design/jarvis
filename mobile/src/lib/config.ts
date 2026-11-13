/**
 * The live JARVIS server (the VPS). Every sign-in uses it; the login screen has no server box. To point a build at
 * another server, set EXPO_PUBLIC_JARVIS_SERVER before building.
 */
export const DEFAULT_SERVER = process.env.EXPO_PUBLIC_JARVIS_SERVER || 'http://201.18.193.128:8088';

/** The developer's laptop, reached over USB after `adb reverse tcp:8000 tcp:8000`. For testing only: hold the shield
 *  on the login screen for two seconds to switch to it (and back). */
export const LOCAL_SERVER = 'http://localhost:8000';
