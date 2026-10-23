#!/usr/bin/env python3
"""Idempotently add the shell's native dependencies to the generated app build.

Capacitor regenerates android/ on `cap add`, so anything the shell needs beyond
Capacitor has to be re-applied on every build. Right now that is one thing:
androidx.browser, for the Custom Tab that carries a social sign-in.
"""
import sys

BLOCK = '''
dependencies {
    implementation "androidx.browser:browser:1.8.0"
}
'''


def main(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if "androidx.browser" in text:
        print("gradle deps already current")
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(BLOCK)
    print("gradle deps patched")


if __name__ == "__main__":
    main(sys.argv[1])
