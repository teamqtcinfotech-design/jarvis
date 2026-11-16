// The app's config: app.json, plus where app updates come from and the certificate that checks them.
//
// The live build takes its updates from the VPS and trusts only the VPS's certificate. A laptop test build
// (EXPO_PUBLIC_JARVIS_SERVER=http://localhost:8000) takes them from the laptop and trusts only the laptop's
// certificate, and carries a different runtime version ("…-laptop"), so neither can ever run the other's code.
// The private keys stay on those servers (manage.py app_update keygen); only the public certificates are here.
//
// Prebuild writes these into the Android project, so run `npx expo prebuild --platform android --no-install`
// with the same EXPO_PUBLIC_JARVIS_SERVER before building an APK for the other server.
const LIVE_SERVER = 'http://201.18.193.128:8088'; // same as DEFAULT_SERVER in src/lib/config.ts

module.exports = ({ config }) => {
  const server = (process.env.EXPO_PUBLIC_JARVIS_SERVER || LIVE_SERVER).replace(/\/+$/, '');
  const live = server === LIVE_SERVER;
  return {
    ...config,
    runtimeVersion: live ? config.version : `${config.version}-laptop`,
    updates: {
      url: `${server}/api/v1/updates/manifest`,
      enabled: true,
      // The library itself also checks at every cold start: even an app whose own code went wrong still gets the fix.
      checkAutomatically: 'ON_LOAD',
      fallbackToCacheTimeout: 0, // never wait at start-up: a new version is used from the next restart
      codeSigningCertificate: live ? './certs/updates-live.pem' : './certs/updates-laptop.pem',
      codeSigningMetadata: { keyid: 'main', alg: 'rsa-v1_5-sha256' },
    },
  };
};
