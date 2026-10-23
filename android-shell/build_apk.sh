#!/bin/sh
# Build the Fireside debug APK on bench01.
# Toolchain comes from /srv/bench/android_env.sh; node 22 from /srv/bench/tools.
set -e
[ -f /srv/bench/android_env.sh ] && . /srv/bench/android_env.sh
export PATH=/srv/bench/tools/node-v22.11.0-linux-x64/bin:$PATH
cd "$(dirname "$0")"

# www/ is the source of truth; computing it fresh keeps the APK honest.
npm install --no-audit --no-fund
[ -d android ] || npx cap add android
npx cap sync android

# Native pieces Capacitor does not own. Copied on every build so the generated
# project can be thrown away without losing them.
mkdir -p android/app/src/main/java/com/stolworthy/fireside
cp android_src/MainActivity.java android_src/MediaPlaybackService.java \
   android/app/src/main/java/com/stolworthy/fireside/
python3 android_src/patch_manifest.py android/app/src/main/AndroidManifest.xml

[ -f android/local.properties ] || echo "sdk.dir=$ANDROID_HOME" > android/local.properties
cd android
sh ./gradlew assembleDebug
ls -l app/build/outputs/apk/debug/app-debug.apk