"""The library: books and their chapters, per user, with text behind refs.

The extracted chapter TEXT is stored content-addressed, so the same chapter text
(from an epub or the same fic fetched as html) resolves to one blob and one
render cache entry. A book row names its source in `meta`; a chapter row names
its text blob and, once rendered, its audio blob.
"""

import hashlib
import json
import uuid

from host.db import now


def create_user(conn, user_id, name=""):
    conn.execute("INSERT OR IGNORE INTO users(id,name,created_at) VALUES(?,?,?)",
                 (user_id, name, now()))
    conn.commit()
    return user_id


def add_book(conn, storage, user_id, book):
    """Store book + chapters. `book` is a host.contracts.Book. Returns the id."""
    book_id = book.id or uuid.uuid4().hex
    meta = dict(book.meta or {})
    conn.execute(
        "INSERT INTO books(id,user_id,title,source_json,created_at) VALUES(?,?,?,?,?)",
        (book_id, user_id, book.title, json.dumps(meta), now()))
    for i, ch in enumerate(book.chapters):
        text = ch.text if isinstance(ch.text, str) else str(ch.text)
        ref = storage.put(text.encode("utf-8"))
        conn.execute(
            "INSERT OR REPLACE INTO chapters"
            "(book_id,idx,title,chars,text_ref) VALUES(?,?,?,?,?)",
            (book_id, i, ch.title, len(text), ref))
    conn.commit()
    return book_id


def list_books(conn, user_id):
    rows = conn.execute(
        "SELECT * FROM books WHERE user_id=? ORDER BY created_at DESC", (user_id,))
    out = []
    for r in rows:
        n = conn.execute("SELECT COUNT(*) c FROM chapters WHERE book_id=?",
                         (r["id"],)).fetchone()["c"]
        done = conn.execute(
            "SELECT COUNT(*) c FROM chapters WHERE book_id=? AND audio_ref IS NOT NULL",
            (r["id"],)).fetchone()["c"]
        out.append({"id": r["id"], "title": r["title"],
                    "created_at": r["created_at"], "chapters": n, "rendered": done,
                    "cover": r["cover_ref"] or ""})
    return out


def get_book(conn, book_id):
    r = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not r:
        return None
    meta = json.loads(r["source_json"] or "{}")
    return {"id": r["id"], "user_id": r["user_id"], "title": r["title"],
            "meta": meta, "created_at": r["created_at"],
            "cover": r["cover_ref"] or "", "cover_mime": r["cover_mime"] or "",
            "isbn": (meta or {}).get("isbn", "")}


# ---- cover art ------------------------------------------------------------
# A cover is one more blob behind a ref, named on the book row. The bytes are
# the book's own (pulled from the epub) or fetched by ISBN; the host never
# invents one. `cover_ref` doubles as the cache-busting version in the UI.

def set_cover(conn, storage, book_id, data, mime="image/jpeg"):
    ref = storage.put(bytes(data))
    conn.execute("UPDATE books SET cover_ref=?,cover_mime=? WHERE id=?",
                 (ref, mime or "image/jpeg", book_id))
    conn.commit()
    return ref


def clear_cover(conn, book_id):
    conn.execute("UPDATE books SET cover_ref='',cover_mime='' WHERE id=?", (book_id,))
    conn.commit()


def cover(conn, storage, book_id):
    """(bytes, mime) for the book's cover, or None."""
    r = conn.execute("SELECT cover_ref,cover_mime FROM books WHERE id=?",
                     (book_id,)).fetchone()
    if not r or not r["cover_ref"]:
        return None
    return storage.get(r["cover_ref"]), (r["cover_mime"] or "image/jpeg")


def chapters(conn, book_id):
    rows = conn.execute("SELECT * FROM chapters WHERE book_id=? ORDER BY idx", (book_id,))
    return [dict(r) for r in rows]


def chapter_text(conn, storage, book_id, idx):
    r = conn.execute("SELECT text_ref FROM chapters WHERE book_id=? AND idx=?",
                     (book_id, idx)).fetchone()
    if not r:
        raise KeyError("no chapter %s/%s" % (book_id, idx))
    return storage.get(r["text_ref"]).decode("utf-8")


# ---- renditions: one voiced copy of one chapter ---------------------------
# A chapter can be voiced more than once - the same chapter read by a male and
# a female voice is two renditions, both kept. `chapters` points at the newest
# (the default when no voice is named); this table is the full set the player
# offers. The key is deterministic, so a re-render of the same voice updates in
# place and a migration can re-seed without duplicating.

def rendition_key(book_id, idx, provider, voice, model, speed):
    raw = "|".join([book_id or "", str(idx), provider or "", voice or "",
                    model or "", "%.3f" % float(speed if speed is not None else 1.0)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def add_rendition(conn, book_id, idx, provider, voice, model, speed,
                  audio_ref, audio_bytes, duration_s=None, voice_label=""):
    rk = rendition_key(book_id, idx, provider, voice, model, speed)
    conn.execute(
        "INSERT OR REPLACE INTO renditions"
        "(rk,book_id,idx,provider,voice,voice_label,model,speed,"
        " audio_ref,audio_bytes,duration_s,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (rk, book_id, idx, provider or "", voice or "", voice_label or "",
         model or "", float(speed or 1.0), audio_ref, audio_bytes, duration_s, now()))


def renditions(conn, book_id):
    rows = conn.execute(
        "SELECT * FROM renditions WHERE book_id=? ORDER BY idx, created_at",
        (book_id,))
    return [dict(r) for r in rows]


def rendition(conn, book_id, idx, provider="", voice="", model=""):
    """The rendition of one chapter for a named voice, or None. An empty voice
    matches any, so the newest wins (the ORDER BY picks it)."""
    for r in renditions(conn, book_id):
        if r["idx"] != idx:
            continue
        if voice and r["voice"] != voice:
            continue
        if provider and r["provider"] != provider:
            continue
        if model and r["model"] != model:
            continue
        if r["audio_ref"]:
            return r
    return None


def backfill_renditions(conn):
    """Seed the renditions table from chapter rows of a book voiced before this
    table existed, so it still lists its one voice. Idempotent: the key is
    deterministic and INSERT OR IGNORE skips what is already there."""
    rows = conn.execute(
        "SELECT book_id,idx,provider,voice,model,speed,audio_ref,audio_bytes,duration_s "
        "FROM chapters WHERE audio_ref IS NOT NULL").fetchall()
    for r in rows:
        rk = rendition_key(r["book_id"], r["idx"], r["provider"], r["voice"],
                           r["model"], r["speed"])
        conn.execute(
            "INSERT OR IGNORE INTO renditions"
            "(rk,book_id,idx,provider,voice,voice_label,model,speed,"
            " audio_ref,audio_bytes,duration_s,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (rk, r["book_id"], r["idx"], r["provider"] or "", r["voice"] or "",
             "", r["model"] or "", float(r["speed"] or 1.0), r["audio_ref"],
             r["audio_bytes"], r["duration_s"], now()))
    conn.commit()
