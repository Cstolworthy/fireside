"""The render job pipeline: book text -> per-chapter audio behind refs.

A render is per (chapter text, provider, voice, model, speed). The cache is
global on the server's storage layer, so the same chapter text spoken by the
same voice is one render no matter which user or which book asked for it. The
job row carries progress; the client knows nothing about the pipeline.
"""

import hashlib
import uuid

from host import library
from host.db import now


def render_key(text_ref, provider, voice, model, speed):
    raw = "|".join([text_ref or "", provider or "", voice or "",
                    model or "", "%.3f" % float(speed if speed is not None else 1.0)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _chapter_row(conn, book_id, idx):
    r = conn.execute("SELECT * FROM chapters WHERE book_id=? AND idx=?",
                     (book_id, idx)).fetchone()
    if not r:
        raise KeyError("no chapter %s/%s" % (book_id, idx))
    return r


def render_chapter(conn, storage, speech, book_id, idx, voice="", model="", speed=1.0,
                   cred=None, opts=None, voice_label=""):
    """Render one chapter. Returns "cached" or "rendered"."""
    row = _chapter_row(conn, book_id, idx)
    key = render_key(row["text_ref"], speech.id, voice, model, speed)
    cached = conn.execute("SELECT * FROM render_cache WHERE key=?", (key,)).fetchone()
    if cached:
        _write_chapter_audio(conn, book_id, idx, cached, speech.id, voice, model, speed,
                             voice_label=voice_label)
        conn.commit()
        return "cached"
    text = storage.get(row["text_ref"]).decode("utf-8")
    o = dict(opts or {})
    o.setdefault("speed", speed)
    o.setdefault("model", model)
    audio = speech.synth(text, voice, o, cred)
    ref = storage.put(audio.data)
    conn.execute(
        "INSERT OR REPLACE INTO render_cache"
        "(key,audio_ref,audio_bytes,duration_s,provider,voice,model,created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (key, ref, len(audio.data), None, speech.id, voice, model, now()))
    entry = {"audio_ref": ref, "audio_bytes": len(audio.data), "duration_s": None}
    _write_chapter_audio(conn, book_id, idx, entry, speech.id, voice, model, speed,
                         voice_label=voice_label)
    conn.commit()
    return "rendered"


def _write_chapter_audio(conn, book_id, idx, entry, provider, voice, model, speed,
                         voice_label=""):
    conn.execute(
        "UPDATE chapters SET audio_ref=?,audio_bytes=?,duration_s=?,"
        "provider=?,voice=?,model=?,speed=? WHERE book_id=? AND idx=?",
        (entry["audio_ref"], entry["audio_bytes"], entry["duration_s"],
         provider, voice, model, float(speed or 1.0), book_id, idx))
    # keep every voice, not just the newest: the chapter row points at the
    # default, the renditions table is what lets the player offer a choice
    library.add_rendition(conn, book_id, idx, provider, voice, model, speed,
                          entry["audio_ref"], entry["audio_bytes"], entry["duration_s"],
                          voice_label=voice_label)


def start_job(conn, user_id, book_id, provider, voice="", model="", total=0, kind="render"):
    job_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO jobs(id,user_id,book_id,kind,provider,voice,model,status,"
        "total,current,error,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, user_id, book_id, kind, provider, voice, model, "running",
         total, 0, "", now(), now()))
    conn.commit()
    return job_id


def render_book(conn, storage, speech, user_id, book_id, voice="", model="",
                speed=1.0, cred=None, opts=None, only=None, job_id=None,
                voice_label=""):
    """Render all (or `only`) chapters of a book. Synchronous; returns the job id.

    Progress and per-chapter outcome are written to the job row so a client can
    poll; the loop stops on the first real error and records it.
    """
    rows = conn.execute("SELECT idx FROM chapters WHERE book_id=? ORDER BY idx",
                        (book_id,)).fetchall()
    idxs = [r["idx"] for r in rows]
    if only is not None:
        idxs = [i for i in idxs if i in set(only)]
    if job_id is None:
        job_id = start_job(conn, user_id, book_id, speech.id, voice, model, len(idxs))
    done = 0
    try:
        for i in idxs:
            render_chapter(conn, storage, speech, book_id, i, voice, model, speed,
                           cred=cred, opts=opts, voice_label=voice_label)
            done += 1
            conn.execute("UPDATE jobs SET current=?,updated_at=? WHERE id=?",
                         (done, now(), job_id))
            conn.commit()
        conn.execute("UPDATE jobs SET status='done',current=?,updated_at=? WHERE id=?",
                     (done, now(), job_id))
    except Exception as e:                      # a failed run must say so, not lie
        conn.execute("UPDATE jobs SET status='error',error=?,updated_at=? WHERE id=?",
                     ("%s: %s" % (type(e).__name__, e), now(), job_id))
    conn.commit()
    return job_id


def job(conn, job_id):
    r = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(r) if r else None
