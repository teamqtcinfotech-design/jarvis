/**
 * The live JARVIS server (the VPS). Every sign-in uses it; the login screen has no server box. To point a build at
 * another server, set EXPO_PUBLIC_JARVIS_SERVER before building.
 */
export const DEFAULT_SERVER = process.env.EXPO_PUBLIC_JARVIS_SERVER || 'http://201.18.193.128:8088';

/** The developer's laptop, reached over USB after `adb reverse tcp:8000 tcp:8000`. Not used by the client build: a
 *  laptop test build is made with EXPO_PUBLIC_JARVIS_SERVER=http://localhost:8000. */
export const LOCAL_SERVER = 'http://localhost:8000';
