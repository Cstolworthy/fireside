"""Host state: the mutable records (users, library, jobs, ledger, positions).

Blobs (source bytes, rendered audio) live in the storage backend and are named
by content hash. Everything mutable and queryable lives here in SQLite, in the
data dir. This split is the contract: storage is dumb and content-addressed,
the DB is where pointers and policy live.
"""

import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, name TEXT DEFAULT '', created_at TEXT);
CREATE TABLE IF NOT EXISTS books (
    id TEXT PRIMARY KEY, user_id TEXT, title TEXT,
    source_json TEXT DEFAULT '', created_at TEXT);
CREATE TABLE IF NOT EXISTS chapters (
    book_id TEXT, idx INTEGER, title TEXT, chars INTEGER,
    text_ref TEXT, audio_ref TEXT, audio_bytes INTEGER, duration_s REAL,
    provider TEXT, voice TEXT, model TEXT, speed REAL,
    PRIMARY KEY (book_id, idx));
CREATE TABLE IF NOT EXISTS render_cache (
    key TEXT PRIMARY KEY, audio_ref TEXT, audio_bytes INTEGER,
    duration_s REAL, provider TEXT, voice TEXT, model TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, user_id TEXT, book_id TEXT, kind TEXT,
    provider TEXT, voice TEXT, model TEXT, status TEXT,
    total INTEGER, current INTEGER, error TEXT,
    created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS ledger (
    id TEXT PRIMARY KEY, user_id TEXT, kind TEXT, delta REAL,
    currency TEXT, book_id TEXT, job_id TEXT, note TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS balances (
    user_id TEXT PRIMARY KEY, currency TEXT, amount REAL);
CREATE TABLE IF NOT EXISTS reservations (
    id TEXT PRIMARY KEY, user_id TEXT, amount REAL, currency TEXT,
    state TEXT, job_id TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS positions (
    user_id TEXT, book_id TEXT, chapter_index INTEGER, offset_seconds REAL,
    updated_at TEXT, owner_device_id TEXT, PRIMARY KEY (user_id, book_id));
CREATE TABLE IF NOT EXISTS devices (
    user_id TEXT, device_id TEXT, book_id TEXT, last_seen TEXT, stop INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, device_id, book_id));
"""


def default_data_dir():
    return os.environ.get(
        "FIRESIDE_DATA",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"),
    )


def connect(path=None):
    if path is None:
        d = default_data_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "fireside.db")
    elif path != ":memory:":
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
