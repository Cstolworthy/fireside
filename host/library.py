"""The library: books and their chapters, per user, with text behind refs.

The extracted chapter TEXT is stored content-addressed, so the same chapter text
(from an epub or the same fic fetched as html) resolves to one blob and one
render cache entry. A book row names its source in `meta`; a chapter row names
its text blob and, once rendered, its audio blob.
"""

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
                    "created_at": r["created_at"], "chapters": n, "rendered": done})
    return out


def get_book(conn, book_id):
    r = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not r:
        return None
    return {"id": r["id"], "user_id": r["user_id"], "title": r["title"],
            "meta": json.loads(r["source_json"] or "{}"), "created_at": r["created_at"]}


def chapters(conn, book_id):
    rows = conn.execute("SELECT * FROM chapters WHERE book_id=? ORDER BY idx", (book_id,))
    return [dict(r) for r in rows]


def chapter_text(conn, storage, book_id, idx):
    r = conn.execute("SELECT text_ref FROM chapters WHERE book_id=? AND idx=?",
                     (book_id, idx)).fetchone()
    if not r:
        raise KeyError("no chapter %s/%s" % (book_id, idx))
    return storage.get(r["text_ref"]).decode("utf-8")
