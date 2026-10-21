import json
import os
import unittest

from host import mailer


class Mailer(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("FIRESIDE_RESEND_API_KEY", "FIRESIDE_MAIL_FROM")}
        os.environ.pop("FIRESIDE_RESEND_API_KEY", None)
        os.environ.pop("FIRESIDE_MAIL_FROM", None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_unconfigured_sends_nothing(self):
        self.assertFalse(mailer.configured())
        self.assertFalse(mailer.send("a@b.com", "s", "t"))

    def test_configured_needs_both_key_and_from(self):
        os.environ["FIRESIDE_RESEND_API_KEY"] = "re_x"
        self.assertFalse(mailer.configured())
        os.environ["FIRESIDE_MAIL_FROM"] = "fireside@readfireside.com"
        self.assertTrue(mailer.configured())

    def test_send_posts_the_message(self):
        os.environ["FIRESIDE_RESEND_API_KEY"] = "re_x"
        os.environ["FIRESIDE_MAIL_FROM"] = "fireside@readfireside.com"
        seen = {}

        def fake_fetch(req):
            seen["url"] = req.full_url
            seen["auth"] = req.get_header("Authorization")
            seen["body"] = json.loads(req.data.decode())
            return b'{"id":"ok"}'

        self.assertTrue(mailer.send("cat@example.com", "hi", "body",
                                    html="<p>body</p>", fetch=fake_fetch))
        self.assertEqual(seen["url"], "https://api.resend.com/emails")
        self.assertEqual(seen["auth"], "Bearer re_x")
        self.assertEqual(seen["body"]["to"], ["cat@example.com"])
        self.assertEqual(seen["body"]["from"], "fireside@readfireside.com")
        self.assertEqual(seen["body"]["html"], "<p>body</p>")

    def test_a_failed_send_returns_false_and_does_not_raise(self):
        os.environ["FIRESIDE_RESEND_API_KEY"] = "re_x"
        os.environ["FIRESIDE_MAIL_FROM"] = "fireside@readfireside.com"

        def boom(req):
            raise RuntimeError("network down")

        self.assertFalse(mailer.send("a@b.com", "s", "t", fetch=boom))
