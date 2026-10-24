"""Resale metering: a run pays only for the characters it actually synthesises.

The sale connection is the one place a render is metered. These tests prove the
mechanic that makes a re-voice honest: a chapter already voiced in this voice is
a cache hit and costs the buyer nothing, so a fully cached book is $0 and a
partial re-render pays only for the fresh chapters. The environment switch that
designates the sale connection is tested here too.
"""

import os
import tempfile
import time
import unittest

from host import accounting as acc
from host.app import App, _sale_connection_from_env
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


class TestSaleSwitchFromEnv(unittest.TestCase):
    def test_absent_is_none(self):
        os.environ.pop("FIRESIDE_SALE_CONNECTION", None)
        self.assertIsNone(_sale_connection_from_env())

    def test_json_spec(self):
        os.environ["FIRESIDE_SALE_CONNECTION"] = (
            '{"connection_id": "c1", "provider": "azure", "voice": "en-US-AndrewNeural"}')
        try:
            spec = _sale_connection_from_env()
            self.assertEqual(spec["connection_id"], "c1")
            self.assertEqual(spec["provider"], "azure")
            self.assertEqual(spec["voice"], "en-US-AndrewNeural")
        finally:
            os.environ.pop("FIRESIDE_SALE_CONNECTION", None)

    def test_shorthand_spec(self):
        os.environ["FIRESIDE_SALE_CONNECTION"] = "azure:en-US-AndrewNeural"
        try:
            spec = _sale_connection_from_env()
            self.assertEqual(spec, {"provider": "azure", "voice": "en-US-AndrewNeural"})
        finally:
            os.environ.pop("FIRESIDE_SALE_CONNECTION", None)


class TestResaleMetering(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "fake", order=10, body=READER_BODY)
        write_plugin(root, "speech", "sale", body=SPEECH_BODY)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"),
                       storage_id="mem",
                       sale_connection={"provider": "sale", "voice": "v"})
        acc.topup(self.app.conn, "u1", 100.0, currency="chars")

    def _import(self):
        s, imp = self.app.import_book({}, {"user": "u1", "kind": "upload",
                                           "filename": "x.epub"})
        self.assertEqual(s, 200)
        return imp["book_id"]

    def _render(self, bid):
        s, out = self.app.render({}, {"user": "u1", "book": bid,
                                      "provider": "sale", "voice": "v"})
        self.assertEqual(s, 200)
        self._wait(out["job_id"])
        return out

    def _wait(self, jid, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            s, job = self.app.job({"id": jid}, {})
            if s == 200 and job.get("status") in ("done", "error", "cancelled"):
                return job
            time.sleep(0.05)
        self.fail("job %s did not finish" % jid)

    def test_first_run_pays_for_the_text(self):
        bid = self._import()
        self._render(bid)
        chars = sum(c["chars"] for c in self.app.conn.execute(
            "SELECT chars FROM chapters WHERE book_id=?", (bid,)).fetchall())
        self.assertAlmostEqual(acc.balance(self.app.conn, "u1"), 100.0 - chars)

    def test_rerender_in_the_same_voice_is_free(self):
        bid = self._import()
        self._render(bid)
        after_first = acc.balance(self.app.conn, "u1")
        self._render(bid)
        # nothing fresh to synthesise: no hold, no debit
        self.assertAlmostEqual(acc.balance(self.app.conn, "u1"), after_first)
        self.assertEqual(acc.reserved_total(self.app.conn, "u1"), 0.0)

    def test_partial_rerender_pays_only_for_the_fresh_chapter(self):
        bid = self._import()
        self._render(bid)
        chars = {c["idx"]: c["chars"] for c in self.app.conn.execute(
            "SELECT idx,chars FROM chapters WHERE book_id=?", (bid,)).fetchall()}
        # drop chapter 0's rendition AND its cached audio: only chapter 1 is now
        # cached, so a re-render has to synthesise chapter 0 again
        self.app.conn.execute(
            "DELETE FROM renditions WHERE book_id=? AND idx=0 AND voice='v'", (bid,))
        self.app.conn.execute("DELETE FROM render_cache")
        self.app.conn.commit()
        before = acc.balance(self.app.conn, "u1")
        self._render(bid)
        self.assertAlmostEqual(acc.balance(self.app.conn, "u1"),
                               before - chars[0])


class TestRefusalCarriesNumbers(unittest.TestCase):
    """A metered run that cannot be paid for must say how far short it is.

    The screen can only print what the server sends, so "insufficient balance"
    on its own leaves the reader with two words and no route out. The refusal
    carries `needed` and `available`, and they have to be the real ones: this is
    a hold decision, and a wrong number here is a wrong number about money.
    """

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

    def _render(self, bid):
        return self.app.render({}, {"user": "u1", "book": bid,
                                    "provider": "sale", "voice": "v"})

    def test_empty_balance_reports_zero_available(self):
        bid = self._import()
        s, out = self._render(bid)
        self.assertEqual(s, 402)
        self.assertEqual(out["error"], "insufficient balance")
        self.assertGreater(out["needed"], 0)
        self.assertEqual(out["available"], 0.0)
        self.assertIn("detail", out)

    def test_partial_balance_reports_what_is_actually_there(self):
        acc.topup(self.app.conn, "u1", 10.0, currency="chars")
        bid = self._import()
        s, out = self._render(bid)
        self.assertEqual(s, 402)
        self.assertEqual(out["available"], 10.0)
        self.assertGreater(out["needed"], out["available"])
        # a refusal holds nothing: the balance is untouched
        self.assertEqual(acc.balance(self.app.conn, "u1"), 10.0)
        self.assertEqual(acc.reserved_total(self.app.conn, "u1"), 0.0)


if __name__ == "__main__":
    unittest.main()
