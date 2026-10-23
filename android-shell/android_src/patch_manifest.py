#!/usr/bin/env python3
"""Idempotently add the shell's native pieces to a Capacitor-generated AndroidManifest.

The generated manifest is disposable; this script is the record of what the shell
actually needs. Safe to run any number of times.
"""
import sys

PERMS = [
    "android.permission.INTERNET",
    "android.permission.FOREGROUND_SERVICE",
    "android.permission.FOREGROUND_SERVICE_MEDIA_PLAYBACK",
    "android.permission.POST_NOTIFICATIONS",
]

SERVICE = """        <service
            android:name=".MediaPlaybackService"
            android:exported="false"
            android:foregroundServiceType="mediaPlayback" />
"""


def main(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    orig = text

    for p in PERMS:
        line = '    <uses-permission android:name="%s" />\n' % p
        if p not in text:
            text = text.replace("<application", line + "\n    <application", 1)

    if "MediaPlaybackService" not in text:
        text = text.replace("    </application>", SERVICE + "    </application>", 1)

    if text != orig:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        print("manifest patched")
    else:
        print("manifest already current")


if __name__ == "__main__":
    main(sys.argv[1])