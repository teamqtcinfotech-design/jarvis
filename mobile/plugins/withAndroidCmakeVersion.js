// Config plugin: build the app's native code with CMake 3.31.6.
// Its bundled ninja (1.12.1) supports Windows long paths; the default 3.22.1 (ninja 1.10.2) fails with
// "Filename longer than 260 characters" on the React Native codegen objects.
// Requires Windows long paths to be enabled once (LongPathsEnabled = 1).
const { withAppBuildGradle } = require('expo/config-plugins');

const MARKER = '// jarvis: pinned CMake version';

module.exports = function withAndroidCmakeVersion(config, props = {}) {
  const version = props.version || '3.31.6';
  return withAppBuildGradle(config, (cfg) => {
    if (!cfg.modResults.contents.includes(MARKER)) {
      cfg.modResults.contents = cfg.modResults.contents.replace(
        /\nandroid\s*\{/,
        (match) => `${match}\n    ${MARKER} (long-path-aware ninja)\n    externalNativeBuild { cmake { version "${version}" } }`,
      );
    }
    return cfg;
  });
};
