"""Voice-server connections, kept on the server.

A connection is one configured way to reach a speech provider: an Azure key, the
local Kokoro, the tower. They live here, not in a browser, so a user never holds
a key and an admin sets one up once for the whole household.

Two scopes:

  system    set by an admin. every signed-in user may use it, may override its
            non-secret fields, and may switch it off for themselves. only an
            admin changes the stored values.
  personal  owned by one user, and visible only to them.

A secret is never returned by a listing: the caller sees the field name and
whether it is set. The one place a secret is materialised is `resolve`, which
runs in-process and hands the merged settings straight to the provider.
"""

import json
import uuid

from host.db import now

SYSTEM = "system"
PERSONAL = "personal"


def _loads(s):
    try:
        return json.loads(s or "{}")
    except ValueError:
        return {}


def get(conn, conn_id):
    r = conn.execute("SELECT * FROM connections WHERE id=?", (conn_id,)).fetchone()
    if not r:
        return None
    return {"id": r["id"], "owner_id": r["owner_id"], "scope": r["scope"],
            "provider": r["provider"], "label": r["label"], "data": _loads(r["data"])}


def list_for(conn, uid):
    """Every connection a user may see: all system ones, plus their own."""
    rows = conn.execute(
        "SELECT * FROM connections WHERE scope=? OR owner_id=? "
        "ORDER BY scope, provider, label, id", (SYSTEM, uid)).fetchall()
    out = []
    for r in rows:
        c = {"id": r["id"], "owner_id": r["owner_id"], "scope": r["scope"],
             "provider": r["provider"], "label": r["label"], "data": _loads(r["data"]),
             "mine": r["owner_id"] == uid, "system": r["scope"] == SYSTEM}
        ov = conn.execute("SELECT enabled,data FROM connection_overrides "
                          "WHERE user_id=? AND connection_id=?", (uid, r["id"])).fetchone()
        c["override"] = _loads(ov["data"]) if ov else {}
        c["enabled"] = True if not ov or ov["enabled"] is None else bool(ov["enabled"])
        out.append(c)
    return out


def may_use(c, uid, is_admin=False):
    if c is None:
        return False
    if c["scope"] == SYSTEM:
        return True
    return c["owner_id"] == uid or bool(is_admin)


def may_edit(c, uid, is_admin=False):
    if c is None:
        return False
    if c["scope"] == SYSTEM:
        return bool(is_admin)
    return c["owner_id"] == uid


def upsert(conn, owner_id, provider, label, data, scope=PERSONAL, conn_id=None):
    """Create a connection, or update one in place.

    An update merges: a field sent empty (or absent) keeps its stored value, so
    saving a form that does not re-type a secret never blanks it.
    """
    if conn_id:
        cur = get(conn, conn_id)
        if cur is None:
            raise KeyError("no connection %s" % conn_id)
        merged = dict(cur["data"])
        for k, v in (data or {}).items():
            if v not in (None, ""):
                merged[k] = v
        conn.execute("UPDATE connections SET provider=?,label=?,data=?,updated_at=? "
                     "WHERE id=?",
                     (provider or cur["provider"],
                      cur["label"] if label is None else label,
                      json.dumps(merged), now(), conn_id))
        conn.commit()
        return conn_id
    cid = uuid.uuid4().hex
    conn.execute("INSERT INTO connections"
                 "(id,owner_id,scope,provider,label,data,created_at,updated_at)"
                 " VALUES(?,?,?,?,?,?,?,?)",
                 (cid, owner_id, scope, provider, label or "", json.dumps(data or {}),
                  now(), now()))
    conn.commit()
    return cid


def delete(conn, conn_id):
    conn.execute("DELETE FROM connections WHERE id=?", (conn_id,))
    conn.execute("DELETE FROM connection_overrides WHERE connection_id=?", (conn_id,))
    conn.commit()


def set_override(conn, uid, conn_id, enabled=None, data=None):
    row = conn.execute("SELECT enabled,data FROM connection_overrides "
                       "WHERE user_id=? AND connection_id=?", (uid, conn_id)).fetchone()
    cur_enabled = True if not row or row["enabled"] is None else bool(row["enabled"])
    cur_data = _loads(row["data"]) if row else {}
    if enabled is not None:
        cur_enabled = bool(enabled)
    for k, v in (data or {}).items():
        if v not in (None, ""):
            cur_data[k] = v
    conn.execute(
        "INSERT OR REPLACE INTO connection_overrides"
        "(user_id,connection_id,enabled,data,updated_at) VALUES(?,?,?,?,?)",
        (uid, conn_id, 1 if cur_enabled else 0, json.dumps(cur_data), now()))
    conn.commit()


def resolve(conn, uid, conn_id):
    """The effective settings for one render. Secrets materialise here only."""
    c = get(conn, conn_id)
    if c is None:
        return None
    row = conn.execute("SELECT enabled,data FROM connection_overrides "
                       "WHERE user_id=? AND connection_id=?", (uid, conn_id)).fetchone()
    if row and row["enabled"] is not None and not bool(row["enabled"]):
        return None
    merged = dict(c["data"])
    if row:
        merged.update(_loads(row["data"]))
    return merged


def get_prefs(conn, uid):
    r = conn.execute("SELECT default_connection_id,default_voice FROM user_prefs "
                     "WHERE user_id=?", (uid,)).fetchone()
    return {"default_connection_id": (r["default_connection_id"] if r else "") or "",
            "default_voice": (r["default_voice"] if r else "") or ""}


def set_prefs(conn, uid, connection_id=None, voice=None):
    """None leaves a field alone; "" clears it."""
    cur = get_prefs(conn, uid)
    cid = cur["default_connection_id"] if connection_id is None else connection_id
    v = cur["default_voice"] if voice is None else voice
    conn.execute(
        "INSERT OR REPLACE INTO user_prefs"
        "(user_id,default_connection_id,default_voice,updated_at) VALUES(?,?,?,?)",
        (uid, cid or "", v or "", now()))
    conn.commit()
