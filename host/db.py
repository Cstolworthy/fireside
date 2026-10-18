"""Host state: the mutable records (users, library, jobs, ledger, positions).

Blobs (source bytes, rendered audio) live in the storage backend and are named
by content hash. Everything mutable and queryable lives here, in the data dir.

One shape, two engines. The host speaks one connection dialect - `?`
placeholders, rows addressed by column name, execute/executescript/commit/close -
and this module presents it over SQLite (the default) or a MySQL-compatible
server when configured. No other module knows which engine is behind the
connection.

Two variables, engine and connection, set independently:

  FIRESIDE_DB_TYPE=sqlite|mysql      the engine (default: sqlite)
  FIRESIDE_DB_CONN=<connection>      sqlite: a file path, or sqlite:///path ('' =
                                     the default under FIRESIDE_DATA)
                                     mysql:  user:pass@host:3306/dbname, or a
                                     full mysql://... DSN

The legacy single-variable form still works, so an existing deployment need not
move: FIRESIDE_DB=mysql://user:pass@host:3306/fireside (or sqlite:///path/to.db)
sets both the engine and the connection when the two variables above are unset.

The schema uses only types both engines accept: VARCHAR keys (MySQL needs a
key length; SQLite does not care), TEXT for prose, DOUBLE and BIGINT for numbers.
Reserved-word columns are quoted with backticks, which both engines understand.
"""

import os
import re
import sqlite3
import threading
import time
import urllib.parse

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id VARCHAR(128) PRIMARY KEY, name VARCHAR(255) DEFAULT '', created_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS books (
    id VARCHAR(128) PRIMARY KEY, user_id VARCHAR(128), title TEXT,
    source_json TEXT, created_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS chapters (
    book_id VARCHAR(128), idx INT, title TEXT, chars INT,
    text_ref VARCHAR(255), audio_ref VARCHAR(255), audio_bytes INT, duration_s DOUBLE,
    provider VARCHAR(64), voice VARCHAR(128), model VARCHAR(128), speed DOUBLE,
    PRIMARY KEY (book_id, idx));
CREATE TABLE IF NOT EXISTS render_cache (
    `key` VARCHAR(191) PRIMARY KEY, audio_ref VARCHAR(255), audio_bytes INT,
    duration_s DOUBLE, provider VARCHAR(64), voice VARCHAR(128),
    model VARCHAR(128), created_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS renditions (
    rk VARCHAR(191) PRIMARY KEY, book_id VARCHAR(128), idx INT,
    provider VARCHAR(64), voice VARCHAR(128), voice_label VARCHAR(255),
    model VARCHAR(128), speed DOUBLE, audio_ref VARCHAR(255), audio_bytes INT,
    duration_s DOUBLE, created_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS jobs (
    id VARCHAR(128) PRIMARY KEY, user_id VARCHAR(128), book_id VARCHAR(128), kind VARCHAR(32),
    provider VARCHAR(64), voice VARCHAR(128), model VARCHAR(128), status VARCHAR(32),
    total INT, current INT, error TEXT,
    created_at VARCHAR(32), updated_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS ledger (
    id VARCHAR(128) PRIMARY KEY, user_id VARCHAR(128), kind VARCHAR(32), delta DOUBLE,
    currency VARCHAR(16), book_id VARCHAR(128), job_id VARCHAR(128), note TEXT,
    created_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS balances (
    user_id VARCHAR(128) PRIMARY KEY, currency VARCHAR(16), amount DOUBLE);
CREATE TABLE IF NOT EXISTS reservations (
    id VARCHAR(128) PRIMARY KEY, user_id VARCHAR(128), amount DOUBLE, currency VARCHAR(16),
    state VARCHAR(32), job_id VARCHAR(128), created_at VARCHAR(32), updated_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS positions (
    user_id VARCHAR(128), book_id VARCHAR(128), chapter_index INT, offset_seconds DOUBLE,
    updated_at VARCHAR(32), owner_device_id VARCHAR(128),
    PRIMARY KEY (user_id, book_id));
CREATE TABLE IF NOT EXISTS devices (
    user_id VARCHAR(128), device_id VARCHAR(128), book_id VARCHAR(128), last_seen VARCHAR(32),
    stop INT DEFAULT 0, PRIMARY KEY (user_id, device_id, book_id));
CREATE TABLE IF NOT EXISTS credentials (
    user_id VARCHAR(128), provider VARCHAR(64), data TEXT, updated_at VARCHAR(32),
    PRIMARY KEY (user_id, provider));
CREATE TABLE IF NOT EXISTS sessions (
    token VARCHAR(191) PRIMARY KEY, user_id VARCHAR(128), provider VARCHAR(32),
    created_at BIGINT, expires_at BIGINT);
CREATE TABLE IF NOT EXISTS local_accounts (
    username VARCHAR(191) PRIMARY KEY, user_id VARCHAR(128), digest TEXT, salt TEXT,
    created_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS admins (
    user_id VARCHAR(128) PRIMARY KEY, created_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS settings (
    `key` VARCHAR(191) PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS connections (
    id VARCHAR(128) PRIMARY KEY, owner_id VARCHAR(128), scope VARCHAR(16),
    provider VARCHAR(64), label VARCHAR(255), data TEXT,
    created_at VARCHAR(32), updated_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS connection_overrides (
    user_id VARCHAR(128), connection_id VARCHAR(128), enabled INT DEFAULT 1,
    data TEXT, updated_at VARCHAR(32),
    PRIMARY KEY (user_id, connection_id));
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id VARCHAR(128) PRIMARY KEY, default_connection_id VARCHAR(128),
    default_voice VARCHAR(255), updated_at VARCHAR(32));
"""


def default_data_dir():
    return os.environ.get(
        "FIRESIDE_DATA",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"),
    )


def _env(name):
    return (os.environ.get(name) or "").strip()


def db_type(explicit=None):
    """The configured engine: 'sqlite' (default) or 'mysql'.

    FIRESIDE_DB_TYPE names it directly. When that is unset the engine is read
    from the legacy FIRESIDE_DB URL, so a single-variable deployment is unchanged.
    """
    if explicit:
        return "mysql" if str(explicit).strip().lower() in ("mysql", "mariadb") else "sqlite"
    t = _env("FIRESIDE_DB_TYPE").lower()
    if t in ("mysql", "mariadb"):
        return "mysql"
    if t in ("sqlite", "sqlite3"):
        return "sqlite"
    return "mysql" if _env("FIRESIDE_DB").startswith("mysql") else "sqlite"


def db_conn(explicit=None):
    """The connection string for the configured engine.

    Unset (or empty) means the SQLite default under FIRESIDE_DATA. Falls back to
    the legacy FIRESIDE_DB when FIRESIDE_DB_CONN is unset.
    """
    if explicit is not None:
        return explicit
    return _env("FIRESIDE_DB_CONN") or _env("FIRESIDE_DB")


def db_url():
    """One connection string for the configured backend, engine included."""
    kind = db_type()
    conn = db_conn()
    if kind == "mysql":
        if not conn:
            return ""
        return conn if conn.startswith("mysql") else "mysql://" + conn
    if not conn or conn.startswith("sqlite://"):
        return conn
    return "sqlite://" + conn


def backend_name(url=None):
    if url is None:
        return db_type()
    return "mysql" if url.startswith("mysql") else "sqlite"


def sqlite_path(path=None):
    if path is None:
        d = default_data_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "fireside.db")
    elif path != ":memory:":
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
    return path


def connect(path=None):
    """Open the configured backend. `path` is the SQLite file (default dir)."""
    if db_type() == "mysql":
        conn = MySQLConnection(db_url())
        conn.executescript(SCHEMA)
        return conn
    url = db_conn()
    if path is None and url:
        if url.startswith("sqlite://"):
            path = url[len("sqlite://"):] or None
        else:
            path = url
    conn = sqlite3.connect(sqlite_path(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---- MySQL shim -------------------------------------------------------------
# SQLite-isms in the query layer, translated to what MySQL accepts. The list is
# deliberately short and explicit: everything else the host writes is plain
# SQL-92 and runs unchanged on both engines.

_INSERT_OR_IGNORE = re.compile(r"\bINSERT\s+OR\s+IGNORE\b", re.I)
_INSERT_OR_REPLACE = re.compile(r"\bINSERT\s+OR\s+REPLACE\b", re.I)


def translate(sql):
    """Rewrite a SQLite-flavoured statement for MySQL. Pure, so it is testable."""
    sql = _INSERT_OR_IGNORE.sub("INSERT IGNORE", sql)
    sql = _INSERT_OR_REPLACE.sub("REPLACE", sql)
    if sql.strip().upper().startswith("BEGIN IMMEDIATE"):
        sql = "START TRANSACTION"
    return sql.replace("?", "%s")


class MySQLConnection:
    """A DB-API connection shaped like the sqlite one the host already speaks.

    All access is serialised behind one lock: a pymysql connection is not
    thread-safe and the host serves requests from a thread pool. For a home
    server that is exactly the right amount of concurrency control.
    """

    def __init__(self, url, cursor_dict=True):
        import pymysql  # imported here so SQLite deployments need not install it

        u = urllib.parse.urlparse(url.replace("mysql+pymysql", "mysql", 1))
        self._pymysql = pymysql
        self._lock = threading.RLock()
        self._in_txn = False
        dbname = (u.path or "").lstrip("/") or "fireside"
        safe = re.sub(r"[^A-Za-z0-9_]", "", dbname) or "fireside"
        base = dict(
            host=u.hostname or "127.0.0.1",
            port=u.port or 3306,
            user=urllib.parse.unquote(u.username or ""),
            password=urllib.parse.unquote(u.password or ""),
            charset="utf8mb4",
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
        )
        boot = pymysql.connect(**base)          # the database may not exist yet
        try:
            with boot.cursor() as c:
                c.execute("CREATE DATABASE IF NOT EXISTS `%s` "
                          "CHARACTER SET utf8mb4" % safe)
        finally:
            boot.close()
        self._raw = pymysql.connect(database=safe, **base)

    def _ping(self):
        try:
            self._raw.ping(reconnect=True)
        except Exception:
            pass

    def execute(self, sql, params=()):
        with self._lock:
            self._ping()
            cur = self._raw.cursor()
            cur.execute(translate(sql), tuple(params) if params else ())
            head = sql.strip().upper()
            if head.startswith("START TRANSACTION") or head.startswith("BEGIN IMMEDIATE"):
                self._in_txn = True
            elif head.startswith("COMMIT") or head.startswith("ROLLBACK"):
                self._in_txn = False
            return cur

    def executescript(self, script):
        with self._lock:
            cur = self._raw.cursor()
            for stmt in script.split(";"):
                s = stmt.strip()
                if s:
                    cur.execute(s)
            self._raw.commit()
            self._in_txn = False

    def commit(self):
        with self._lock:
            self._raw.commit()
            self._in_txn = False

    def rollback(self):
        with self._lock:
            self._raw.rollback()
            self._in_txn = False

    @property
    def in_transaction(self):
        return self._in_txn

    def close(self):
        with self._lock:
            self._raw.close()

