import { requireNativeModule } from 'expo-modules-core';

/** Native "Hey JARVIS" listener (Android only). See src/lib/wake.ts for the safe JS wrapper. */
const JarvisWake = requireNativeModule('JarvisWake');

export default JarvisWake;
