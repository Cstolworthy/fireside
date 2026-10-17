"""Server-side credential store (hosted mode).

A hosted deployment holds the provider key so the user pastes nothing. The value
never leaves the server: it is resolved in-process and handed to the plugin as a
plain credential, exactly like the client-pasted path. A credential stored here
is never returned by any endpoint.

At-rest protection is a deployment choice; this module stores JSON and documents
that the caller owns encryption (a disk/OS key or a DB-level secret).
"""

import json

from host.db import now


def set_credential(conn, user_id, provider, data):
    conn.execute(
        "INSERT INTO credentials(user_id,provider,data,updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id,provider) DO UPDATE SET data=excluded.data,"
        "updated_at=excluded.updated_at",
        (user_id, provider, json.dumps(data or {}), now()))
    conn.commit()


def get_credential(conn, user_id, provider):
    """Resolve a stored credential for a plugin. Server-internal; never served."""
    r = conn.execute("SELECT data FROM credentials WHERE user_id=? AND provider=?",
                     (user_id, provider)).fetchone()
    if not r:
        return {}
    try:
        return json.loads(r["data"] or "{}")
    except ValueError:
        return {}


def delete_credential(conn, user_id, provider):
    conn.execute("DELETE FROM credentials WHERE user_id=? AND provider=?",
                 (user_id, provider))
    conn.commit()


def providers(conn, user_id):
    """The names of providers this user has a stored credential for (no values)."""
    rows = conn.execute("SELECT provider FROM credentials WHERE user_id=?", (user_id,))
    return sorted(r["provider"] for r in rows)
