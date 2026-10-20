"""Per-user storage accounting, by reference count over content-addressed blobs.

Storage is metered on the user's OWN bytes: a blob counts against a user's quota
only while that user is its sole referencer. The moment two distinct users
reference the same blob (the shared render cache, or the same chapter text from
the same source), it costs every one of them nothing - it is host infrastructure,
funded by the platform tier. That is the whole point of content addressing: we
store one copy, so we charge for one copy, and the second user's copy is free.

A blob that WAS shared and drops to a single referencer does not silently start
billing that user, and does not silently vanish. It enters a grace window: the
shelf surfaces "nobody else references this; it purges at T unless you adopt it."
Adopt onto your quota and it bills to you; do nothing and it is purged at T, after
the notice. A blob that was never shared is just the user's own and bills from the
start, no window.

Blobs are counted from the rows that name them (chapters.text_ref/audio_ref,
renditions.audio_ref, books.cover_ref), never from a running tally, so the count
cannot drift from the shelf. Byte size prefers the size recorded at render time
and falls back to the storage backend's own size, so a missing audio_bytes is a
read, never a zero.

`sweep` only detects and reports; it deletes only true orphans (no referencer at
all). Deleting a still-referenced blob is the explicit, separate `purge` step, so
nothing the user can still see is ever removed by a background pass.
"""

import time

from host.db import now

# ---- policy constants ------------------------------------------------------
# The platform tier's included allowance. Blocks (buy more storage, monthly) are
# not wired yet - the block size is pending; when it lands, allowance adds
# blocks * STORAGE_BLOCK_BYTES and this stays the only place that knows.
PLATFORM_ALLOWANCE_BYTES = 2 * 1000 ** 3        # 2 GB included with the tier
STORAGE_BLOCK_BYTES = 0                          # pending: size of a storage block
PLAN_ALLOWANCE = {"platform": PLATFORM_ALLOWANCE_BYTES}
GRACE_SECONDS = 30 * 24 * 3600                   # adopt-or-purge window after a drop to 1


def _sources(conn):
    """Yield (user_id, ref, recorded_bytes) for every blob a shelf names.

    recorded_bytes may be None when the row does not carry a size (chapter text,
    cover art); the caller sizes those from the storage backend.
    """
    q = ("SELECT b.user_id AS u, c.text_ref AS ref, NULL AS nbytes "
         "FROM chapters c JOIN books b ON b.id = c.book_id "
         "WHERE c.text_ref IS NOT NULL AND c.text_ref <> ''")
    for r in conn.execute(q):
        yield r["u"], r["ref"], r["nbytes"]
    q = ("SELECT b.user_id AS u, c.audio_ref AS ref, c.audio_bytes AS nbytes "
         "FROM chapters c JOIN books b ON b.id = c.book_id "
         "WHERE c.audio_ref IS NOT NULL AND c.audio_ref <> ''")
    for r in conn.execute(q):
        yield r["u"], r["ref"], r["nbytes"]
    q = ("SELECT b.user_id AS u, r.audio_ref AS ref, r.audio_bytes AS nbytes "
         "FROM renditions r JOIN books b ON b.id = r.book_id "
         "WHERE r.audio_ref IS NOT NULL AND r.audio_ref <> ''")
    for r in conn.execute(q):
        yield r["u"], r["ref"], r["nbytes"]
    q = ("SELECT user_id AS u, cover_ref AS ref, NULL AS nbytes FROM books "
         "WHERE cover_ref IS NOT NULL AND cover_ref <> ''")
    for r in conn.execute(q):
        yield r["u"], r["ref"], r["nbytes"]


def index(conn, storage):
    """Build the reference index in one pass.

    Returns (ref_users, ref_bytes): for each blob ref, the set of distinct user
    ids that reference it, and its size in bytes.
    """
    ref_users = {}
    ref_bytes = {}
    for uid, ref, nbytes in _sources(conn):
        if not ref:
            continue
        ref_users.setdefault(ref, set()).add(uid)
        if ref not in ref_bytes:
            try:
                n = int(nbytes or 0)
            except (TypeError, ValueError):
                n = 0
            if n <= 0 and storage is not None:
                n = storage.size(ref)
            ref_bytes[ref] = n
    return ref_users, ref_bytes


def shared_refs(ref_users):
    """Refs referenced by two or more distinct users - free for all of them."""
    return {ref for ref, users in ref_users.items() if len(users) >= 2}


def _holds(conn):
    """ref -> hold row, for every blob currently in a grace window."""
    out = {}
    try:
        rows = conn.execute("SELECT * FROM blob_holds").fetchall()
    except Exception:
        return out
    for r in rows:
        out[r["ref"]] = {"unshared_at": int(r["unshared_at"] or 0),
                         "adopted_at": int(r["adopted_at"] or 0),
                         "state": r["state"] or "pending"}
    return out


def usage(conn, storage, user_id):
    """This user's storage, split three ways.

    own_bytes      - blobs this user alone references and has claimed (billed)
    pending_bytes  - sole-referenced blobs in an adopt window, not yet billed;
                     they purge at their deadline unless adopted
    shared_bytes   - blobs this user references that someone else also does (0)
    total_bytes    - what the whole shelf would cost if nothing were shared
    blobs / shared_blobs / pending_blobs - counts, for display
    """
    ref_users, ref_bytes = index(conn, storage)
    shared = shared_refs(ref_users)
    holds = _holds(conn)
    own = pending = shared_here = 0
    n_own = n_shared = n_pending = 0
    for ref, users in ref_users.items():
        if user_id not in users:
            continue
        n = ref_bytes.get(ref, 0)
        if ref in shared:
            shared_here += n
            n_shared += 1
            continue
        h = holds.get(ref)
        if h and not h["adopted_at"]:
            pending += n
            n_pending += 1
        else:
            own += n
            n_own += 1
    return {"user_id": user_id, "own_bytes": own, "pending_bytes": pending,
            "shared_bytes": shared_here, "total_bytes": own + pending + shared_here,
            "blobs": n_own, "shared_blobs": n_shared, "pending_blobs": n_pending}


def allowance_bytes(plan):
    """Included bytes for a plan name; an unknown or absent plan gets none."""
    return PLAN_ALLOWANCE.get((plan or "").strip().lower(), 0)


def check_room(conn, storage, user_id, plan, incoming_bytes):
    """True when adding `incoming_bytes` of OWN storage stays within allowance.

    `incoming_bytes` is the size of bytes that would be the user's own (a new
    upload). A blob that already exists for another user is shared and costs this
    user nothing, so callers pass 0 for a cache hit. Read-only: it decides, the
    caller refuses the write.
    """
    allowed = allowance_bytes(plan)
    used = usage(conn, storage, user_id)["own_bytes"]
    return (used + int(incoming_bytes or 0)) <= allowed


# ---- the grace window: detect, adopt, purge --------------------------------

def pending_for(conn, storage, user_id, grace_seconds=GRACE_SECONDS):
    """The user's sole-referenced blobs that are in an adopt window, each with
    its purge deadline. This is what the shelf surfaces so nothing vanishes
    silently."""
    ref_users, ref_bytes = index(conn, storage)
    shared = shared_refs(ref_users)
    holds = _holds(conn)
    out = []
    for ref, users in ref_users.items():
        if user_id not in users or ref in shared:
            continue
        h = holds.get(ref)
        if h and not h["adopted_at"]:
            out.append({"ref": ref, "bytes": ref_bytes.get(ref, 0),
                        "unshared_at": h["unshared_at"],
                        "purge_at": h["unshared_at"] + grace_seconds})
    out.sort(key=lambda e: e["purge_at"])
    return out


def sweep(conn, storage, ts=None, grace_seconds=GRACE_SECONDS):
    """Recompute refcounts and maintain grace windows. Never deletes a blob a
    user still references.

    - a blob that was shared (>=2 users) and is now sole-referenced gets a hold,
      stamped now, so the shelf can say "purges at T unless you adopt it"
    - a blob that is shared again loses its hold (it is free again)
    - a blob no user references is an orphan: its blob is deleted and its rows
      cleared (this is the only deletion sweep performs)

    Returns {"new_holds": [ref], "expired": [ref], "orphans": [ref]}.
    """
    ts = int(ts if ts is not None else time.time())
    ref_users, _ = index(conn, storage)
    prev = {}
    try:
        for r in conn.execute("SELECT ref, refcount FROM blob_refs"):
            prev[r["ref"]] = int(r["refcount"] or 0)
    except Exception:
        prev = {}
    holds = _holds(conn)
    new_holds, expired, orphans = [], [], []

    for ref in set(prev) | set(ref_users):
        cur = len(ref_users.get(ref, ()))
        was = prev.get(ref, 0)
        if cur == 0:
            if storage is not None:
                try:
                    storage.delete(ref)
                except Exception:
                    pass
            conn.execute("DELETE FROM blob_refs WHERE ref=?", (ref,))
            conn.execute("DELETE FROM blob_holds WHERE ref=?", (ref,))
            if was:
                orphans.append(ref)
            continue
        conn.execute(
            "INSERT INTO blob_refs(ref,refcount,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(ref) DO UPDATE SET refcount=excluded.refcount, "
            "updated_at=excluded.updated_at", (ref, cur, now()))
        if cur >= 2:
            # shared again -> free for everyone, drop any window
            conn.execute("DELETE FROM blob_holds WHERE ref=?", (ref,))
            continue
        if was >= 2 and ref not in holds:
            conn.execute(
                "INSERT INTO blob_holds(ref,unshared_at,adopted_at,state,updated_at) "
                "VALUES(?,?,0,'pending',?)", (ref, ts, now()))
            new_holds.append(ref)
        h = holds.get(ref)
        if h and not h["adopted_at"] and ts - h["unshared_at"] >= grace_seconds:
            expired.append(ref)
    conn.commit()
    return {"new_holds": new_holds, "expired": expired, "orphans": orphans}


def adopt(conn, refs, ts=None):
    """Claim sole-referenced blobs onto the user's quota, ending their window."""
    ts = int(ts if ts is not None else time.time())
    n = 0
    for ref in refs or ():
        cur = conn.execute("SELECT 1 FROM blob_holds WHERE ref=?", (ref,)).fetchone()
        if not cur:
            continue
        conn.execute("UPDATE blob_holds SET adopted_at=?, state='adopted', updated_at=? "
                     "WHERE ref=?", (ts, now(), ref))
        n += 1
    conn.commit()
    return n


def _clear_ref(conn, ref):
    """Remove a blob ref from every shelf row that names it, keeping the rows."""
    conn.execute("UPDATE chapters SET audio_ref=NULL, audio_bytes=0 WHERE audio_ref=?", (ref,))
    conn.execute("UPDATE chapters SET text_ref=NULL WHERE text_ref=?", (ref,))
    conn.execute("DELETE FROM renditions WHERE audio_ref=?", (ref,))
    conn.execute("UPDATE books SET cover_ref='', cover_mime='' WHERE cover_ref=?", (ref,))


def purge(conn, storage, refs):
    """Explicitly delete blobs and clear their shelf rows. Only call for refs the
    caller has decided to remove - orphan sweep or expired, unadopted holds."""
    n = 0
    for ref in refs or ():
        _clear_ref(conn, ref)
        if storage is not None:
            try:
                storage.delete(ref)
            except Exception:
                pass
        conn.execute("DELETE FROM blob_refs WHERE ref=?", (ref,))
        conn.execute("DELETE FROM blob_holds WHERE ref=?", (ref,))
        n += 1
    conn.commit()
    return n
