"""Reserve before start, settle on the plugin report, never overdraw."""

import tempfile
import unittest

from host import accounting as acc
from host.contracts import Quote, Report
from host.db import connect


class TestAccounting(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        acc.topup(self.conn, "u1", 10.0)

    def test_topup_and_balance(self):
        self.assertEqual(acc.balance(self.conn, "u1"), 10.0)
        self.assertEqual(acc.available(self.conn, "u1"), 10.0)

    def test_reserve_holds_funds(self):
        acc.reserve(self.conn, "u1", 3.0)
        self.assertEqual(acc.balance(self.conn, "u1"), 10.0)     # not spent yet
        self.assertEqual(acc.reserved_total(self.conn, "u1"), 3.0)
        self.assertEqual(acc.available(self.conn, "u1"), 7.0)

    def test_reserve_cannot_overdraw(self):
        acc.reserve(self.conn, "u1", 8.0)
        with self.assertRaises(acc.Insufficient):
            acc.reserve(self.conn, "u1", 3.0)
        self.assertEqual(acc.available(self.conn, "u1"), 2.0)

    def test_settle_charged_debits_and_releases_remainder(self):
        rid = acc.reserve(self.conn, "u1", 3.0)
        st = acc.settle(self.conn, rid, Report(charge="charged", cost=1.25, currency="USD"))
        self.assertEqual(st, "settled")
        self.assertAlmostEqual(acc.balance(self.conn, "u1"), 8.75)
        self.assertEqual(acc.reserved_total(self.conn, "u1"), 0.0)

    def test_unknown_stays_reserved_and_pending(self):
        rid = acc.reserve(self.conn, "u1", 3.0)
        st = acc.settle(self.conn, rid, Report(charge="unknown"))
        self.assertEqual(st, "pending")
        self.assertEqual(acc.reservation(self.conn, rid)["state"], "pending")
        self.assertEqual(acc.reserved_total(self.conn, "u1"), 3.0)   # still held
        self.assertAlmostEqual(acc.balance(self.conn, "u1"), 10.0)   # nothing debited

    def test_reconcile_later_applies_without_double_debit(self):
        rid = acc.reserve(self.conn, "u1", 3.0)
        acc.settle(self.conn, rid, Report(charge="unknown"))
        acc.reconcile(self.conn, rid, Report(charge="charged", cost=2.0, currency="USD"))
        self.assertAlmostEqual(acc.balance(self.conn, "u1"), 8.0)
        acc.reconcile(self.conn, rid, Report(charge="charged", cost=2.0, currency="USD"))
        self.assertAlmostEqual(acc.balance(self.conn, "u1"), 8.0)     # idempotent

    def test_release_returns_hold(self):
        rid = acc.reserve(self.conn, "u1", 4.0)
        acc.release(self.conn, rid)
        self.assertEqual(acc.available(self.conn, "u1"), 10.0)
        self.assertEqual(acc.reservation(self.conn, rid)["state"], "released")

    def test_ledger_records_every_move(self):
        rid = acc.reserve(self.conn, "u1", 3.0)
        acc.settle(self.conn, rid, Report(charge="charged", cost=1.0, currency="USD"))
        kinds = [e["kind"] for e in acc.ledger_entries(self.conn, "u1")]
        self.assertEqual(kinds, ["topup", "charge"])

    def test_only_one_sale_connection(self):
        spec = {"provider": "azure", "voice": "en-US-Ava", "model": ""}
        self.assertTrue(acc.sale_connection_matches(spec, "azure", "en-US-Ava", "x"))
        self.assertFalse(acc.sale_connection_matches(spec, "minimax", "en-US-Ava", ""))
        self.assertFalse(acc.sale_connection_matches(spec, "azure", "other", ""))
        self.assertFalse(acc.sale_connection_matches(None, "azure", "en-US-Ava", ""))
        pinned = {"provider": "openai", "voice": "af_heart", "model": "kokoro"}
        self.assertFalse(acc.sale_connection_matches(pinned, "openai", "af_heart", "other"))


if __name__ == "__main__":
    unittest.main()
