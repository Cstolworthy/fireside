"""An admin can share an owned voice server with the household.

A server is personal when added; sharing flips it to system scope so every user
sees it, and stops touching anyone else when unshared.
"""

import json
import urllib.error

from tests.test_connections import ConnBase


class TestShare(ConnBase):
    def _share(self, b, cid, scope):
        try:
            return self.post(b, "/connections", {"id": cid, "scope": scope})
        except urllib.error.HTTPError as e:
            return json.loads(e.read())

    def test_admin_shares_then_unshares_an_owned_server(self):
        admin = self.browser(); self.signup(admin)
        cid = self.post(admin, "/connections",
                        {"provider": "fake", "label": "Mine",
                         "data": {"key": "K", "region": "eastus"}})["id"]
        u = self.browser(); self.signup(u, "cat")
        self.assertFalse(any(c["id"] == cid
                             for c in self.get(u, "/connections")["connections"]))
        r = self._share(admin, cid, "system")
        self.assertTrue(r.get("ok"))
        c = next(x for x in self.get(u, "/connections")["connections"] if x["id"] == cid)
        self.assertEqual(c["scope"], "system")
        self.assertEqual(c["label"], "Mine")           # a scope-only save keeps it
        by = {f["name"]: f for f in c["fields"]}
        self.assertTrue(by["key"]["set"])
        self.assertEqual(by["key"]["value"], "")        # the secret is still hidden
        # cat renders through the shared server without seeing the key
        bid = self.post(u, "/import", {"kind": "upload", "filename": "x.epub"})["book_id"]
        j = self.post(u, "/render", {"book": bid, "connection_id": cid, "voice": "vv"})
        self.assertEqual(self._wait(u, j["job_id"])["status"], "done")
        self.assertIn(b":K:", self._chapter(u, bid))
        # unshare
        self._share(admin, cid, "personal")
        self.assertFalse(any(x["id"] == cid
                             for x in self.get(u, "/connections")["connections"]))

    def test_a_non_admin_cannot_share(self):
        admin = self.browser(); self.signup(admin, "boss")
        cat = self.browser(); self.signup(cat, "cat")
        try:
            r = self.post(cat, "/connections",
                          {"provider": "fake", "label": "X", "scope": "system",
                           "data": {"key": "K"}})
        except urllib.error.HTTPError as e:
            r = json.loads(e.read())
        self.assertIn("error", r)


if __name__ == "__main__":
    import unittest
    unittest.main()
