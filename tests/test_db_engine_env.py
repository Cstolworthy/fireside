"""The database engine's environment knobs, and the MySQL adapter.

No MySQL server: a fake `pymysql` module is installed in sys.modules, so the
adapter's URL parsing, boot step, statement translation, transaction flag and
locking all run for real against a recording double.
"""

import os
import sys
import types
import unittest

from host import db


class FakeCursor:
    def __init__(self, log):
        self.log = log
        self.rows = []

    def execute(self, sql, params=()):
        self.log.append((sql, params))

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


class FakeConn:
    def __init__(self, log):
        self.log = log
        self.closed = 0
        self.commits = 0
        self.rollbacks = 0
        self.pinged = 0
        self.fail_ping = False

    def cursor(self):
        return FakeCursor(self.log)

    def ping(self, reconnect=False):
        self.pinged += 1
        if self.fail_ping:
            raise RuntimeError("gone")

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed += 1


def install_fake_pymysql(log, fail_ping=False):
    mod = types.ModuleType("pymysql")
    mod.cursors = types.SimpleNamespace(DictCursor=object)
    made = []

    def connect(**kw):
        c = FakeConn(log)
        c.fail_ping = fail_ping
        c.kw = kw
        made.append(c)
        return c

    mod.connect = connect
    mod._made = made
    sys.modules["pymysql"] = mod
    return mod


def clear_env():
    for k in ("FIRESIDE_DB_TYPE", "FIRESIDE_DB", "FIRESIDE_DB_CONN",
              "FIRESIDE_DB_SSL", "FIRESIDE_DATA"):
        os.environ.pop(k, None)


class TestEngineKnobs(unittest.TestCase):
    def setUp(self):
        clear_env()

    def tearDown(self):
        clear_env()

    def test_db_type_from_explicit_name(self):
        self.assertEqual(db.db_type("mysql"), "mysql")
        self.assertEqual(db.db_type("MariaDB"), "mysql")
        self.assertEqual(db.db_type("sqlite"), "sqlite")

    def test_db_type_from_env(self):
        os.environ["FIRESIDE_DB_TYPE"] = "mysql"
        self.assertEqual(db.db_type(), "mysql")
        os.environ["FIRESIDE_DB_TYPE"] = "sqlite3"
        self.assertEqual(db.db_type(), "sqlite")
        del os.environ["FIRESIDE_DB_TYPE"]
        os.environ["FIRESIDE_DB"] = "mysql://u@h/db"
        self.assertEqual(db.db_type(), "mysql")
        os.environ["FIRESIDE_DB"] = "/tmp/x.db"
        self.assertEqual(db.db_type(), "sqlite")

    def test_db_conn_and_url(self):
        self.assertEqual(db.db_conn("x"), "x")
        os.environ["FIRESIDE_DB"] = "legacy.db"
        self.assertEqual(db.db_conn(), "legacy.db")
        os.environ["FIRESIDE_DB_CONN"] = "conn.db"
        self.assertEqual(db.db_conn(), "conn.db")

    def test_db_url_for_mysql_and_sqlite(self):
        os.environ["FIRESIDE_DB_TYPE"] = "mysql"
        self.assertEqual(db.db_url(), "")
        os.environ["FIRESIDE_DB_CONN"] = "u:p@h/db"
        self.assertTrue(db.db_url().startswith("mysql://"))
        os.environ["FIRESIDE_DB_CONN"] = "mysql://u:p@h/db"
        self.assertTrue(db.db_url().startswith("mysql://"))
        os.environ["FIRESIDE_DB_TYPE"] = "sqlite"
        os.environ["FIRESIDE_DB_CONN"] = "file.db"
        self.assertEqual(db.db_url(), "sqlite://file.db")
        os.environ["FIRESIDE_DB_CONN"] = "sqlite://already.db"
        self.assertEqual(db.db_url(), "sqlite://already.db")
        del os.environ["FIRESIDE_DB_CONN"]
        self.assertEqual(db.db_url(), "")

    def test_ssl_kwargs(self):
        self.assertEqual(db.mysql_ssl_kwargs(), {})
        os.environ["FIRESIDE_DB_SSL"] = "1"
        ctx = db.mysql_ssl_kwargs()["ssl"]
        self.assertTrue(ctx.check_hostname)
        os.environ["FIRESIDE_DB_SSL"] = "noverify"
        ctx = db.mysql_ssl_kwargs()["ssl"]
        self.assertFalse(ctx.check_hostname)

    def test_backend_name_and_sqlite_path(self):
        self.assertEqual(db.backend_name("mysql://x"), "mysql")
        self.assertEqual(db.backend_name("x.db"), "sqlite")
        os.environ["FIRESIDE_DATA"] = "/tmp/fs-test-data"
        self.assertTrue(db.sqlite_path().endswith("fireside.db"))
        self.assertEqual(db.sqlite_path(":memory:"), ":memory:")

    def test_columns_and_migrate_use_the_engine(self):
        class Conn:
            def __init__(self, rows):
                self.rows = rows
                self.executed = []

            def execute(self, sql, params=()):
                self.executed.append(sql)
                return self

            def fetchall(self):
                return self.rows

            def commit(self):
                pass

        os.environ["FIRESIDE_DB_TYPE"] = "mysql"
        c = Conn([{"Field": "id"}])
        self.assertEqual(db._columns(c, "books"), {"id"})
        self.assertIn("SHOW COLUMNS", c.executed[0])
        os.environ["FIRESIDE_DB_TYPE"] = "sqlite"
        c = Conn([{"name": "id"}])
        self.assertEqual(db._columns(c, "books"), {"id"})
        self.assertIn("PRAGMA", c.executed[0])

        c = Conn([{"name": "id"}])
        db.migrate(c)
        self.assertTrue(any("ALTER TABLE" in s for s in c.executed))


class TestTranslate(unittest.TestCase):
    def test_translate(self):
        self.assertEqual(db.translate("INSERT OR IGNORE INTO t VALUES(?)"),
                         "INSERT IGNORE INTO t VALUES(%s)")
        self.assertTrue(db.translate("INSERT OR REPLACE INTO t VALUES(?)")
                        .startswith("REPLACE INTO t"))
        self.assertEqual(db.translate("BEGIN IMMEDIATE"), "START TRANSACTION")
        self.assertIn("ON DUPLICATE KEY UPDATE",
                      db.translate("INSERT INTO t(a,b) VALUES(?,?) ON CONFLICT(a) DO UPDATE SET b=excluded.b"))


class TestMySQLConnection(unittest.TestCase):
    def setUp(self):
        clear_env()
        self.log = []
        self.mod = install_fake_pymysql(self.log)

    def tearDown(self):
        clear_env()
        sys.modules.pop("pymysql", None)

    def _conn(self, url="mysql://user:p%40ss@dbhost:3307/fireside_db?ssl=1"):
        return db.MySQLConnection(url)

    def test_parses_url_creates_database_and_reconnects(self):
        c = self._conn()
        boot, live = self.mod._made
        self.assertEqual(live.kw["database"], "fireside_db")
        self.assertEqual(live.kw["user"], "user")
        self.assertEqual(live.kw["password"], "p@ss")
        self.assertEqual(live.kw["port"], 3307)
        self.assertEqual(boot.closed, 1)
        self.assertIn("CREATE DATABASE IF NOT EXISTS `fireside_db`", self.log[0][0])
        self.assertEqual(c._raw, live)

    def test_missing_parts_take_defaults(self):
        c = self._conn("mysql:///")
        self.assertEqual(self.mod._made[1].kw["database"], "fireside")
        self.assertEqual(self.mod._made[1].kw["host"], "127.0.0.1")
        self.assertEqual(self.mod._made[1].kw["port"], 3306)

    def test_execute_translates_and_tracks_transactions(self):
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        self.assertTrue(c.in_transaction)
        c.execute("INSERT INTO t VALUES(?)", ("a",))
        self.assertEqual(self.log[-1][0], "INSERT INTO t VALUES(%s)")
        self.assertEqual(self.log[-1][1], ("a",))
        c.execute("COMMIT")
        self.assertFalse(c.in_transaction)
        c.execute("ROLLBACK")
        self.assertFalse(c.in_transaction)

    def test_executescript_splits_statements(self):
        c = self._conn()
        c.executescript("CREATE TABLE a(i INT); CREATE TABLE b(i INT);  ")
        names = [s for s, _ in self.log if "CREATE TABLE" in s]
        self.assertEqual(len(names), 2)
        self.assertEqual(self.mod._made[1].commits, 1)

    def test_commit_rollback_close(self):
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        c.commit()
        self.assertFalse(c.in_transaction)
        c.execute("BEGIN IMMEDIATE")
        c.rollback()
        self.assertFalse(c.in_transaction)
        c.close()
        self.assertEqual(self.mod._made[1].rollbacks, 1)
        self.assertEqual(self.mod._made[1].closed, 1)

    def test_ping_failure_is_swallowed(self):
        c = self._conn()
        c._raw.fail_ping = True
        c.execute("SELECT 1")          # still runs; the ping error is ignored
        self.assertTrue(self.log)

    def test_connect_dispatches_to_mysql(self):
        os.environ["FIRESIDE_DB_TYPE"] = "mysql"
        os.environ["FIRESIDE_DB_CONN"] = "mysql://u@h/db"
        c = db.connect()
        self.assertIsInstance(c, db.MySQLConnection)
        self.assertEqual(db.backend_name(), "mysql")


if __name__ == "__main__":
    unittest.main()
