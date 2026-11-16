#!/usr/bin/env bash
# Does this push send the app's code to the phones over the air? Used by GitHub Actions (deploy.yml).
# Prints "publish=true|false" (for $GITHUB_OUTPUT); explanations go to stderr.
#   - the push does not touch mobile/              -> nothing to send
#   - native parts changed, version NOT raised      -> refuse (exit 1): phones on that version could not run it
#   - native parts changed, version raised          -> published for the NEW version only (needs the new APK)
set -euo pipefail
BEFORE="${1:-}"
AFTER="${2:?usage: app-update-plan.sh <before-sha> <after-sha>}"

# Anything that ends up in the APK itself rather than in the app's code. (The public certificates in mobile/certs
# are built into the APK too, but they never change what code a phone can run, so they do not block an update.)
NATIVE=(mobile/package.json mobile/package-lock.json mobile/.npmrc mobile/app.json mobile/app.config.js mobile/plugins
        mobile/google-services.json 'mobile/modules/*/android' 'mobile/modules/*/expo-module.config.json')

if [ -z "$BEFORE" ] || [ "$BEFORE" = "0000000000000000000000000000000000000000" ] || ! git cat-file -e "${BEFORE}^{commit}" 2>/dev/null; then
  echo "No earlier commit to compare with (a manual run): publishing; the server skips it if the app did not change." >&2
  echo "publish=true"
  exit 0
fi

if git diff --quiet "$BEFORE" "$AFTER" -- mobile/; then
  echo "This push does not change the app: nothing is sent to the phones." >&2
  echo "publish=false"
  exit 0
fi

# The first "version" in app.json is expo.version (the app's version = the native build's runtime version).
version_at() { git show "$1:mobile/app.json" 2>/dev/null | grep -m1 -oE '"version"[[:space:]]*:[[:space:]]*"[^"]+"' | sed -E 's/.*"([^"]+)"$/\1/' || true; }

native="$(git diff --name-only "$BEFORE" "$AFTER" -- "${NATIVE[@]}" | tr '\n' ' ')"
if [ -n "$native" ]; then
  old="$(version_at "$BEFORE")"
  new="$(version_at "$AFTER")"
  if [ "$old" = "$new" ]; then
    echo "::error::This push changes native parts of the app ($native) but keeps version $new. Phones on $new cannot run that code, so NO update was sent. Raise the version in mobile/app.json and build a new APK." >&2
    exit 1
  fi
  echo "::notice::Native parts changed and the version went $old -> $new: this update is for $new only; phones need the $new APK first (manage.py app_update apk ...)." >&2
fi
echo "publish=true"
