"""Playback state: one owner per book, exact position, stop-push to the loser.

The server is the source of truth for two things a client cannot be trusted
with: who currently owns playback, and where the position is. Timestamps are
server-stamped. A claim makes a device the owner and raises a stop flag for the
previous owner; the previous owner checks its flag (poll/SSE) and stops.

Full model: docs/PLAYBACK.md.
"""

from host.db import now


def get_position(conn, user_id, book_id):
    r = conn.execute("SELECT * FROM positions WHERE user_id=? AND book_id=?",
                     (user_id, book_id)).fetchone()
    if not r:
        return None
    return {"chapter_index": r["chapter_index"], "offset_seconds": r["offset_seconds"],
            "updated_at": r["updated_at"], "owner_device_id": r["owner_device_id"]}


def last_position(conn, user_id):
    """The book this user touched most recently, with where they were in it.

    One row per (user, book); the newest updated_at is the one a fresh page
    should open. The title rides along so the caller need not have loaded the
    library first.
    """
    r = conn.execute(
        "SELECT p.book_id, p.chapter_index, p.offset_seconds, p.updated_at, b.title "
        "FROM positions p LEFT JOIN books b ON b.id=p.book_id "
        "WHERE p.user_id=? ORDER BY p.updated_at DESC LIMIT 1",
        (user_id,)).fetchone()
    if not r:
        return {}
    return {"book_id": r["book_id"], "title": r["title"] or "",
            "chapter_index": r["chapter_index"], "offset_seconds": r["offset_seconds"],
            "updated_at": r["updated_at"]}


def put_position(conn, user_id, book_id, chapter_index, offset_seconds, device_id):
    """Write the exact position. On pause/stop/seek this is immediate."""
    conn.execute(
        "INSERT INTO positions(user_id,book_id,chapter_index,offset_seconds,updated_at,"
        "owner_device_id) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(user_id,book_id) DO UPDATE SET chapter_index=excluded.chapter_index,"
        "offset_seconds=excluded.offset_seconds,updated_at=excluded.updated_at",
        (user_id, book_id, int(chapter_index), float(offset_seconds), now(), device_id))
    conn.commit()
    return get_position(conn, user_id, book_id)


def owner(conn, user_id, book_id):
    r = conn.execute("SELECT owner_device_id FROM positions WHERE user_id=? AND book_id=?",
                     (user_id, book_id)).fetchone()
    return r["owner_device_id"] if r and r["owner_device_id"] else None


def claim(conn, user_id, book_id, device_id):
    """Make `device_id` the owner; flag every other device to stop."""
    prev = owner(conn, user_id, book_id)
    if prev and prev != device_id:
        conn.execute("UPDATE devices SET stop=1 WHERE user_id=? AND book_id=? AND device_id=?",
                     (user_id, book_id, prev))
    conn.execute(
        "INSERT INTO positions(user_id,book_id,chapter_index,offset_seconds,updated_at,"
        "owner_device_id) VALUES(?,?,0,0,?,?) "
        "ON CONFLICT(user_id,book_id) DO UPDATE SET owner_device_id=excluded.owner_device_id,"
        "updated_at=excluded.updated_at",
        (user_id, book_id, now(), device_id))
    conn.execute("INSERT OR IGNORE INTO devices(user_id,device_id,book_id,last_seen,stop) "
                 "VALUES(?,?,?,?,0)", (user_id, device_id, book_id, now()))
    conn.execute("UPDATE devices SET stop=0,last_seen=? WHERE user_id=? AND device_id=? AND book_id=?",
                 (now(), user_id, device_id, book_id))
    conn.commit()
    return {"owner": device_id, "stopped": prev if prev and prev != device_id else None}


def heartbeat(conn, user_id, book_id, device_id):
    conn.execute("INSERT OR IGNORE INTO devices(user_id,device_id,book_id,last_seen,stop) "
                 "VALUES(?,?,?,?,0)", (user_id, device_id, book_id, now()))
    conn.execute("UPDATE devices SET last_seen=? WHERE user_id=? AND device_id=? AND book_id=?",
                 (now(), user_id, device_id, book_id))
    conn.commit()


def stop_pending(conn, user_id, book_id, device_id):
    """True if this device has been told to stop (the loser of a handoff)."""
    r = conn.execute("SELECT stop FROM devices WHERE user_id=? AND device_id=? AND book_id=?",
                     (user_id, device_id, book_id)).fetchone()
    return bool(r and r["stop"])


def ack_stop(conn, user_id, book_id, device_id):
    conn.execute("UPDATE devices SET stop=0 WHERE user_id=? AND device_id=? AND book_id=?",
                 (user_id, device_id, book_id))
    conn.commit()


def release(conn, user_id, book_id, device_id):
    """Explicit handoff: the owner stops and clears ownership."""
    if owner(conn, user_id, book_id) == device_id:
        conn.execute("UPDATE positions SET owner_device_id='',updated_at=? "
                     "WHERE user_id=? AND book_id=?", (now(), user_id, book_id))
        conn.commit()
