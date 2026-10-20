"""Resold character credits: the plugin sets the rate, the host holds the count.

Covers docs/PRICING.md: the sale connection's plugin reports a rate that already
includes our fee; a payment of N dollars buys floor(N / rate * 1e6) characters,
and from then on the balance is just characters. Consumption holds the estimate
before a run, applies the plugin's reported count after, and refuses a run the
balance cannot cover.
"""

import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from host import accounting as acc, payments
from host.contracts import Report
from host.db import connect


def _azure():
    """Load the shipped azure sale plugin the way the plugin host would."""
    path = os.path.join(ROOT, "plugins", "speech", "azure", "plugin.py")
    spec = importlib.util.spec_from_file_location("az_sale_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Speech()


class TestSaleRate(unittest.TestCase):
    def setUp(self):
        self.sp = _azure()

    def test_rate_is_provider_plus_infra_times_markup(self):
        rate = self.sp.sale_rate_per_1m({"markup": "1.3", "infra_per_1m": "0.1"})
        self.assertAlmostEqual(rate, (15.0 + 0.10) * 1.3, places=9)

    def test_defaults_when_fields_missing(self):
        self.assertAlmostEqual(self.sp.sale_rate_per_1m({}), (15.0 + 0.10) * 1.3, places=9)

    def test_chars_rounded_down_never_oversell(self):
        cred = {"markup": "1.3", "infra_per_1m": "0.1"}
        rate = self.sp.sale_rate_per_1m(cred)
        chars = self.sp.chars_for_payment(5.0, cred)
        self.assertEqual(chars, int(5.0 / rate * 1_000_000))
        # the buyer never gets characters the money does not cover
        self.assertLessEqual(chars / 1_000_000 * rate, 5.0)

    def test_unpriced_amounts_buy_nothing(self):
        self.assertEqual(self.sp.chars_for_payment(0, {}), 0)
        self.assertEqual(self.sp.chars_for_payment(-3, {}), 0)

    def test_bundles_are_priced_in_chars(self):
        b = self.sp.bundles({"markup": "1.3", "infra_per_1m": "0.1"})
        self.assertTrue(b)
        for entry in b:
            self.assertEqual(entry["chars"], self.sp.chars_for_payment(entry["usd"], {}))

    def test_sale_quote_reserves_characters(self):
        q = self.sp.quote({"chars_est": 4200, "cred": {}, "sale": True})
        self.assertEqual(q.currency, "chars")
        self.assertEqual(q.price, 4200.0)
        self.assertEqual(q.chars_est, 4200)

    def test_passthrough_quote_stays_in_usd(self):
        q = self.sp.quote({"chars_est": 1_000_000, "cred": {}})
        self.assertEqual(q.currency, "USD")
        self.assertAlmostEqual(q.price, 15.0, places=6)

    def test_sale_report_charges_actual_characters(self):
        r = self.sp.report({"chars_actual": 3999, "sale": True})
        self.assertEqual(r.charge, "charged")
        self.assertEqual(r.cost, 3999.0)
        self.assertEqual(r.currency, "chars")


class TestCharLedger(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        acc.topup(self.conn, "u1", 4000, currency="chars")

    def test_balance_counts_characters(self):
        self.assertEqual(acc.balance(self.conn, "u1"), 4000.0)
        self.assertEqual(acc.available(self.conn, "u1"), 4000.0)

    def test_refuse_when_short(self):
        with self.assertRaises(acc.Insufficient):
            acc.reserve(self.conn, "u1", 5000, currency="chars")
        self.assertEqual(acc.available(self.conn, "u1"), 4000.0)

    def test_hold_estimate_then_settle_actual(self):
        rid = acc.reserve(self.conn, "u1", 4000, currency="chars")
        self.assertEqual(acc.available(self.conn, "u1"), 0.0)   # held, not spent
        acc.settle(self.conn, rid, Report(charge="charged", cost=3900, currency="chars"))
        self.assertEqual(acc.balance(self.conn, "u1"), 100.0)   # only actual spent
        self.assertEqual(acc.reserved_total(self.conn, "u1"), 0.0)


class TestPaymentGate(unittest.TestCase):
    def setUp(self):
        payments.REGISTRY.clear()

    def tearDown(self):
        payments.REGISTRY.clear()

    def test_no_provider_means_sales_are_held(self):
        self.assertFalse(payments.configured())
        self.assertIsNone(payments.get())

    def test_registered_provider_can_settle(self):
        class P:
            id = "test"

            def charge(self, usd, meta=None):
                return payments.Receipt(True, "ref-1")

        payments.register(P())
        self.assertTrue(payments.configured())
        r = payments.get("test").charge(5.0, {})
        self.assertTrue(r.ok)
        self.assertEqual(r.reference, "ref-1")


if __name__ == "__main__":
    unittest.main()
