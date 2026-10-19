"""Per-user listening stats: characters voiced, books listened, time heard.

Characters sent to speech come from finished render jobs (done_chars), grouped
by the provider that voiced them: a job is a real synth run, and its done_chars
is what the reader actually spent. Books listened and listen time come from the
append-only `listens` log, one row per play heartbeat, which starts counting the
day it ships. Nothing here is inferred; a missing row is zero, never a guess.
"""

from host.db import now


def add_listen(conn, user_id, book_id, seconds):
    """Append one listen event. A non-positive or anonymous tick is dropped, so
    the log stays a real record of time the audio was actually running."""
    try:
        seconds = float(seconds or 0)
    except (TypeError, ValueError):
        return
    if seconds <= 0 or not user_id or not book_id:
        return
    conn.execute(
        "INSERT INTO listens(user_id,book_id,seconds,created_at) VALUES(?,?,?,?)",
        (user_id, book_id, seconds, now()))
    conn.commit()


def user_stats(conn, user_id):
    """The reader's own numbers: characters voiced (total and per provider),
    books listened, and total listen time in seconds."""
    chars_total = 0
    by_provider = {}
    rows = conn.execute(
        "SELECT provider, COALESCE(SUM(done_chars),0) s FROM jobs "
        "WHERE user_id=? AND status='done' GROUP BY provider", (user_id,)).fetchall()
    for r in rows:
        n = int(r["s"] or 0)
        by_provider[r["provider"] or ""] = n
        chars_total += n
    t = conn.execute(
        "SELECT COALESCE(SUM(seconds),0) s, COUNT(DISTINCT book_id) b "
        "FROM listens WHERE user_id=?", (user_id,)).fetchone()
    return {"chars_total": chars_total,
            "chars_by_provider": by_provider,
            "books_listened": int(t["b"] or 0),
            "listen_seconds": float(t["s"] or 0.0)}
