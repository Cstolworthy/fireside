"""Admin grants: lift one limit for one user.

A grant changes only whether a limit applies, never the accounting behind it. An
admin flips it from the settings screen (or, for a stuck account, from the
server). Two switches, independent:

  storage_unlimited   a write never refuses for want of room
  credits_unlimited   a paid render never refuses for want of character credit

Usage is still measured and shown; we just stop refusing. Nothing here touches
the ledger, so a lifted check cannot hide what was spent.
"""

from host.db import now


def get(conn, user_id):
    r = conn.execute(
        "SELECT storage_unlimited,credits_unlimited FROM user_grants WHERE user_id=?",
        (user_id or "",)).fetchone()
    if not r:
        return {"user_id": user_id or "", "storage_unlimited": False,
                "credits_unlimited": False}
    return {"user_id": user_id or "",
            "storage_unlimited": bool(r["storage_unlimited"]),
            "credits_unlimited": bool(r["credits_unlimited"])}


def set_grants(conn, user_id, storage=None, credits=None):
    """Flip either switch. None leaves that one as it was."""
    cur = get(conn, user_id)
    s = cur["storage_unlimited"] if storage is None else bool(storage)
    c = cur["credits_unlimited"] if credits is None else bool(credits)
    row = conn.execute("SELECT user_id FROM user_grants WHERE user_id=?",
                       (user_id,)).fetchone()
    if row:
        conn.execute(
            "UPDATE user_grants SET storage_unlimited=?,credits_unlimited=?,updated_at=?"
            " WHERE user_id=?", (1 if s else 0, 1 if c else 0, now(), user_id))
    else:
        conn.execute(
            "INSERT INTO user_grants(user_id,storage_unlimited,credits_unlimited,"
            "updated_at) VALUES(?,?,?,?)",
            (user_id, 1 if s else 0, 1 if c else 0, now()))
    conn.commit()
    return get(conn, user_id)


def storage_unlimited(conn, user_id):
    return get(conn, user_id)["storage_unlimited"]


def credits_unlimited(conn, user_id):
    return get(conn, user_id)["credits_unlimited"]


def list_all(conn):
    return {r["user_id"]: {"storage_unlimited": bool(r["storage_unlimited"]),
                           "credits_unlimited": bool(r["credits_unlimited"])}
            for r in conn.execute("SELECT * FROM user_grants")}
