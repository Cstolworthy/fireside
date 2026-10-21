"""The payment webhook: a buyer who closes the tab still gets their plan.

Stripe posts the settled checkout to a public route, so the whole trust is the
signature - a forged body must change nothing. These run the real HTTP handler
so the raw-body path and the dispatch are both exercised, with a fake provider
standing in for Stripe (the crypto itself is covered in test_payments_provider).
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from host import billing, payments
from host.app import App, make_handler
from host.payments import Subscription
from tests.test_app import STORAGE_BODY
from tests.test_pluginhost import write_plugin


class FakeProvider:
    """A provider that authenticates with a fixed header, like Stripe does with
    a signature: `good` verifies, anything else is refused."""

    id = "stripe"

    def verify_webhook(self, raw, sig_header):
        if sig_header != "good":
            return None, "signature verification failed"
        try:
            return json.loads(raw.decode("utf-8")), ""
        except (ValueError, UnicodeDecodeError):
            return None, "bad payload"

    def webhook_reference(self, event):
        if (event or {}).get("type") != "checkout.session.completed":
            return ""
        return ((event or {}).get("data") or {}).get("object", {}).get("id", "") or ""

    def confirm(self, reference):
        return Subscription(True, user="u1", plan="platform", status="active",
                            reference=reference, current_period_end=123)


class TestPaymentWebhook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"),
                       storage_id="mem")
        payments.REGISTRY.clear()
        payments.register(FakeProvider())
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        payments.REGISTRY.clear()

    def _post(self, path, raw, sig):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path), data=raw,
            headers={"Content-Type": "application/json",
                     "Stripe-Signature": sig})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _event(self, ref="cs_test_1"):
        return json.dumps({"type": "checkout.session.completed",
                           "data": {"object": {"id": ref}}}).encode()

    def test_a_signed_completed_checkout_applies_the_plan(self):
        s, body = self._post("/webhook/stripe", self._event(), "good")
        self.assertEqual(s, 200)
        self.assertTrue(body["applied"])
        self.assertEqual(body["user"], "u1")
        sub = billing.get(self.app.conn, "u1")
        self.assertEqual(sub["plan"], "platform")
        self.assertEqual(sub["status"], "active")
        self.assertEqual(sub["reference"], "cs_test_1")

    def test_a_forged_body_changes_nothing(self):
        s, body = self._post("/webhook/stripe", self._event(), "bad")
        self.assertEqual(s, 400)
        self.assertIsNone(billing.get(self.app.conn, "u1"))

    def test_a_replayed_event_does_not_double_anything(self):
        self._post("/webhook/stripe", self._event(), "good")
        self._post("/webhook/stripe", self._event(), "good")
        rows = self.app.conn.execute(
            "SELECT COUNT(*) AS n FROM subscriptions WHERE user_id='u1'"
        ).fetchone()
        self.assertEqual(rows["n"], 1)
        self.assertEqual(billing.get(self.app.conn, "u1")["plan"], "platform")

    def test_an_unhandled_event_type_is_acknowledged_and_ignored(self):
        raw = json.dumps({"type": "customer.created",
                          "data": {"object": {"id": "cus_1"}}}).encode()
        s, body = self._post("/webhook/stripe", raw, "good")
        self.assertEqual(s, 200)
        self.assertEqual(body.get("ignored"), "customer.created")
        self.assertIsNone(billing.get(self.app.conn, "u1"))


if __name__ == "__main__":
    unittest.main()
