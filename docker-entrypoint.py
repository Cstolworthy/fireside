#!/usr/bin/env python3
"""Container entrypoint: make the data directory usable, then drop privileges.

The app itself runs unprivileged (uid 10001). A host bind mount is often owned
by root though — Unraid creates /mnt/user/appdata as root, and its Community
Applications template maps that to /data. The unprivileged user then cannot
create the SQLite file and the container dies at startup with:

    sqlite3.OperationalError: unable to open database file

That is not an SQLite bug; it is a directory the app user cannot write to. The
fix should not be "SSH into the box and chown it" — a container that has to be
manually rescued on first run is a broken install. So the entrypoint runs as
root, makes the data directory writable by the app user, and then drops to that
user for real before handing off to the server.

PUID/PGID (the Unraid convention) override the owner when set, so a user can
own the files as themselves instead of 10001.

If the container is started already unprivileged (docker run --user ...), there
is nothing to fix and nothing to drop: it just execs the command.
"""

import os
import pwd
import sys

APP_UID = 10001
APP_GID = 10001


def _int_env(name, default):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _chown_tree(root, uid, gid):
    """Best effort: give the app user ownership of everything under `root`."""
    for dirpath, dirnames, filenames in os.walk(root):
        for name in (dirpath,) + tuple(dirnames) + tuple(filenames):
            try:
                os.chown(name, uid, gid)
            except OSError:
                pass


def _home_for(uid):
    try:
        home = pwd.getpwuid(uid).pw_dir
    except KeyError:
        return "/tmp"
    return home if home and os.path.isdir(home) else "/tmp"


def main(argv):
    uid = _int_env("PUID", APP_UID)
    gid = _int_env("PGID", APP_GID)
    data = os.environ.get("FIRESIDE_DATA") or "/data"

    try:
        os.makedirs(data, exist_ok=True)
    except OSError as exc:
        print("fireside: cannot create data dir %s: %s" % (data, exc),
              file=sys.stderr)

    if os.getuid() == 0:
        try:
            st = os.stat(data)
            if st.st_uid != uid or st.st_gid != gid:
                _chown_tree(data, uid, gid)
        except OSError as exc:
            print("fireside: cannot fix ownership of %s: %s" % (data, exc),
                  file=sys.stderr)
        if uid != 0:
            os.environ.setdefault("HOME", _home_for(uid))
            try:
                os.setgroups([gid])
            except OSError:
                pass
            os.setgid(gid)
            os.setuid(uid)

    os.execvp(argv[0], argv)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: docker-entrypoint.py <command> [args...]", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1:])
