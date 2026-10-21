"""Admin grants: lift the storage or character check for one user.

A grant changes whether a limit refuses, never the accounting. These tests read
the two switches, prove each one lifts its own check, and prove the route is
admin-only.
"""

import os
import tempfile
import unittest

from host import grants
from host.app import App
from host.db import connect
from tests.test_app import READER_BODY, STORAGE_BODY
from tests.test_pluginhost import write_plugin

SPEECH_BODY = """
from host.contracts import Audio, Quote, Report
class Plugin:
    id = "sale"
    ext = "mp3"
    mime = "audio/mpeg"
    needs_key = False
    def settings(self):
        return []
    def list_voices(self, cred):
        return [{"id": "v", "label": "V"}]
    def synth(self, text, voice, opts, cred):
        return Audio(data=("A:" + text).encode())
    def cost_per_1m_chars(self, cred):
        return 12.0
    def sale_rate_per_1m(self, cred):
        return 15.0
    def chars_for_payment(self, usd, cred):
        return int(usd / 15.0 * 1_000_000)
    def bundles(self, cred):
        return []
    def quote(self, ctx):
        n = ctx.get("chars_est", 0)
        return Quote(chars_est=n, price=float(n), currency="chars")
    def report(self, ctx):
        return Report(charge="charged", cost=float(ctx.get("chars_actual", 0)),
                      currency="chars")
"""


class TestGrantModule(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")

    def test_default_is_no_grant(self):
        g = grants.get(self.conn, "u1")
        self.assertFalse(g["storage_unlimited"])
        self.assertFalse(g["credits_unlimited"])
        self.assertFalse(grants.storage_unlimited(self.conn, "u1"))

    def test_flip_one_leaves_the_other(self):
        grants.set_grants(self.conn, "u1", credits=True)
        self.assertTrue(grants.credits_unlimited(self.conn, "u1"))
        self.assertFalse(grants.storage_unlimited(self.conn, "u1"))
        grants.set_grants(self.conn, "u1", storage=True)
        self.assertTrue(grants.storage_unlimited(self.conn, "u1"))
        self.assertTrue(grants.credits_unlimited(self.conn, "u1"))

    def test_restore(self):
        grants.set_grants(self.conn, "u1", storage=True)
        grants.set_grants(self.conn, "u1", storage=False)
        self.assertFalse(grants.storage_unlimited(self.conn, "u1"))


class TestGrantEnforcement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "fake", order=10, body=READER_BODY)
        write_plugin(root, "speech", "sale", body=SPEECH_BODY)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"),
                       storage_id="mem",
                       sale_connection={"provider": "sale", "voice": "v"})

    def _import(self):
        s, imp = self.app.import_book({}, {"user": "u1", "kind": "upload",
                                           "filename": "x.epub"})
        self.assertEqual(s, 200)
        return imp["book_id"]

    def test_storage_grant_lifts_the_write_refusal(self):
        self.assertFalse(self.app._storage_ok("u1", 5 * 1000 ** 3))    # 5GB > 2GB
        grants.set_grants(self.app.conn, "u1", storage=True)
        self.assertTrue(self.app._storage_ok("u1", 5 * 1000 ** 3))

    def test_credit_grant_lifts_the_paid_render_refusal(self):
        bid = self._import()
        s, out = self.app.render({}, {"user": "u1", "book": bid,
                                      "provider": "sale", "voice": "v"})
        self.assertEqual(s, 402)                       # no balance, no grant
        grants.set_grants(self.app.conn, "u1", credits=True)
        s, out = self.app.render({}, {"user": "u1", "book": bid,
                                      "provider": "sale", "voice": "v"})
        self.assertEqual(s, 200)
        self.assertIn("job_id", out)

    def test_grants_route_is_admin_only(self):
        s, out = self.app.grants_list({"_user": "u1"}, {})
        self.assertEqual(s, 403)
        s, out = self.app.grants_set({"_user": "u1"},
                                     {"user": "u1", "storage_unlimited": True})
        self.assertEqual(s, 403)


if __name__ == "__main__":
    unittest.main()
