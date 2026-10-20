"""The recurring platform tier: plans, checkout, confirm, and the stored row.

No network. The Stripe provider is driven by an injected transport, and the host
is driven by a fake provider, so both the provider's request shape and the host's
route-to-row path are checked without touching Stripe.
"""

import os
import unittest

from host import billing, payments
from host.contracts import Report  # noqa: F401  (kept for parity with test_billing)
from host.db import connect
from host.payments import Checkout, Plan, Receipt, Subscription

import payments_stripe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakePost:
    """Both doors: callable for POST, `.get` for GET. Records every call."""

    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def __call__(self, url, data=None, auth=None, timeout=None):
        self.calls.append({"url": url, "method": "POST", "data": data})
        return self.resp

    def get(self, url, params=None, auth=None, timeout=None):
        self.calls.append({"url": url, "method": "GET", "params": params})
        return self.resp


class TestBillingState(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")

    def test_no_row_is_inactive(self):
        self.assertIsNone(billing.get(self.conn, "u1"))
        self.assertFalse(billing.active(self.conn, "u1"))

    def test_upsert_records_and_reports_active(self):
        row = billing.upsert(self.conn, "u1", "platform", "active",
                             provider="stripe", reference="cs_1",
                             current_period_end=2000000000)
        self.assertEqual(row["plan"], "platform")
        self.assertTrue(billing.active(self.conn, "u1"))

    def test_upsert_merges_the_single_row(self):
        billing.upsert(self.conn, "u1", "platform", "active", reference="cs_1")
        billing.upsert(self.conn, "u1", "platform", "canceled", reference="cs_1")
        rows = self.conn.execute("SELECT COUNT(*) c FROM subscriptions").fetchone()
        self.assertEqual(rows["c"], 1)
        self.assertFalse(billing.active(self.conn, "u1"))


class TestStripeTier(unittest.TestCase):
    def _p(self, resp):
        fp = FakePost(resp)
        return payments_stripe.StripeProvider("sk_test_x", post=fp), fp

    def test_platform_plan_is_three_dollars_monthly(self):
        p, _ = self._p(_Resp(200, {}))
        pl = p.plans()
        self.assertEqual(pl[0].id, "platform")
        self.assertEqual(pl[0].amount_cents, 300)
        self.assertEqual(pl[0].interval, "month")

    def test_checkout_builds_a_subscription_session(self):
        p, fp = self._p(_Resp(200, {"id": "cs_9", "url": "https://pay.stripe/cs_9"}))
        co = p.checkout("platform", {"user": "u1", "return_url": "https://x/"})
        self.assertTrue(co.ok)
        self.assertEqual(co.url, "https://pay.stripe/cs_9")
        self.assertEqual(co.reference, "cs_9")
        d = fp.calls[0]["data"]
        self.assertEqual(d["mode"], "subscription")
        self.assertEqual(d["line_items[0][price_data][unit_amount]"], "300")
        self.assertEqual(d["line_items[0][price_data][recurring][interval]"], "month")
        self.assertEqual(d["metadata[user]"], "u1")
        self.assertTrue(d["success_url"].startswith("https://x/?checkout="))

    def test_checkout_unknown_plan_is_refused(self):
        p, _ = self._p(_Resp(200, {}))
        self.assertFalse(p.checkout("nope").ok)

    def test_confirm_complete_and_paid(self):
        p, _ = self._p(_Resp(200, {
            "id": "cs_9", "status": "complete", "payment_status": "paid",
            "metadata": {"user": "u1", "plan": "platform"},
            "subscription": {"status": "active", "current_period_end": 2000000000}}))
        s = p.confirm("cs_9")
        self.assertTrue(s.ok)
        self.assertEqual(s.plan, "platform")
        self.assertEqual(s.user, "u1")
        self.assertEqual(s.status, "active")
        self.assertEqual(s.current_period_end, 2000000000)

    def test_confirm_open_session_is_not_paid(self):
        p, _ = self._p(_Resp(200, {"id": "cs_9", "status": "open",
                                   "payment_status": "unpaid", "metadata": {}}))
        s = p.confirm("cs_9")
        self.assertFalse(s.ok)
        self.assertIn("not complete", s.detail)

    def test_confirm_without_reference_is_refused(self):
        p, _ = self._p(_Resp(200, {}))
        self.assertFalse(p.confirm("").ok)


class FakeProvider:
    id = "fake"

    def charge(self, usd, meta=None):
        return Receipt(True, "pi_1", "ok")

    def plans(self):
        return [Plan("platform", "fireside platform", 300, "usd", "month", "x")]

    def checkout(self, plan_id, meta=None):
        if plan_id != "platform":
            return Checkout(False, detail="unknown plan")
        return Checkout(True, "https://pay.test/x", "cs_1")

    def confirm(self, ref):
        if ref == "cs_1":
            return Subscription(True, "u1", "platform", "active", 2000000000,
                                ref, "ok")
        return Subscription(False, detail="not found")


class TestBillingRoutes(unittest.TestCase):
    """The host path: JSON in, subscription row recorded, no provider details."""

    def setUp(self):
        payments.REGISTRY.clear()
        payments.register(FakeProvider())
        import host.app as appmod
        self.appmod = appmod
        self.app = appmod.App(db_path=":memory:")

    def tearDown(self):
        payments.REGISTRY.clear()

    def test_get_lists_plans_and_no_subscription(self):
        code, out = self.app.billing_get({"user": "u1"}, {})
        self.assertEqual(code, 200)
        self.assertEqual(out["plans"][0]["id"], "platform")
        self.assertEqual(out["plans"][0]["amount_cents"], 300)
        self.assertIsNone(out["subscription"])

    def test_checkout_then_confirm_records_the_row(self):
        code, out = self.app.billing_checkout({"user": "u1"}, {"plan": "platform"})
        self.assertEqual(code, 200)
        self.assertEqual(out["url"], "https://pay.test/x")
        ref = out["reference"]
        code, out = self.app.billing_confirm({"user": "u1"}, {"reference": ref})
        self.assertEqual(code, 200)
        self.assertTrue(out["ok"])
        self.assertEqual(out["subscription"]["plan"], "platform")
        self.assertTrue(billing.active(self.app.conn, "u1"))

    def test_checkout_requires_a_user(self):
        code, out = self.app.billing_checkout({}, {"plan": "platform"})
        self.assertEqual(code, 401)

    def test_confirm_refuses_an_unknown_reference(self):
        code, out = self.app.billing_confirm({"user": "u1"}, {"reference": "nope"})
        self.assertEqual(code, 402)


if __name__ == "__main__":
    unittest.main()
