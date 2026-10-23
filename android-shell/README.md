# Fireside Android shell

Capacitor wrapper that turns Fireside into a phone app. It is a thin remote: on
first run it asks for the address of a Fireside server (yours, or one shared with
you), saves it, and loads that in the WebView. Everything else is the same web
app served over HTTP, so fixes to the web UI do not need a new APK.

## Why it is not just a browser tab

- **Background audio.** `MediaPlaybackService` keeps the process in the foreground
  while audio plays, so a locked screen does not silence a chapter.
- **No address bar.** `MainActivity` exposes `window.NativeMedia` to the bundled
  page (connect / reset / start / stop). The same page is a no-op in a browser.

## Build

```sh
./build_apk.sh
```

Needs the bench toolchain (`/srv/bench/android_env.sh`: JDK 21, Android SDK) and
node 22. Produces `android/app/build/outputs/apk/debug/app-debug.apk`, which is
debug-signed and sideloadable.

`android/` is generated: `build_apk.sh` runs `npx cap add android` when it is
missing, then re-copies `android_src/*.java` and patches the manifest from
`android_src/patch_manifest.py`. Never hand-edit the generated project; edit
`android_src/` instead or the next clean build drops it.

## Not done yet (Play gate)

Release `.aab` + Play App Signing, D-U-N-S on the org account, one 512px icon,
Data safety + content rating, in-app account deletion. Buying stays on the web:
no purchase button in the app.