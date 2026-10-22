"""The OpenAI-compatible provider must send its token on the voice list too.

A gated server (our tower) answers 401 to an unkeyed /v1/audio/voices, which the
provider silently turns into a one-voice fallback. This pins the header on that
call, the same one synth already sends.
"""

import json
import unittest
import urllib.error
from unittest import mock

from providers.openai import OpenAICompat


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._p


def _capture(payload, into):
    def fake_urlopen(req, timeout=0):
        into["url"] = req.full_url
        into["auth"] = req.get_header("Authorization")
        return _Resp(json.dumps(payload).encode())
    return fake_urlopen


class TestListVoices(unittest.TestCase):
    def test_key_goes_out_as_bearer(self):
        p = OpenAICompat()
        seen = {}
        payload = {"voices": [{"id": "af_bella"}, {"id": "am_adam"}]}
        with mock.patch("providers.openai.urllib.request.urlopen",
                        _capture(payload, seen)):
            vs = p.list_voices({"base_url": "https://tower:8443", "key": "tok"})
        self.assertEqual(seen["auth"], "Bearer tok")
        self.assertEqual([v.id for v in vs], ["af_bella", "am_adam"])
        self.assertTrue(seen["url"].endswith("/v1/audio/voices"))

    def test_no_key_sends_no_header(self):
        p = OpenAICompat()
        seen = {}
        with mock.patch("providers.openai.urllib.request.urlopen",
                        _capture({"voices": [{"id": "x"}]}, seen)):
            vs = p.list_voices({"base_url": "http://127.0.0.1:8880"})
        self.assertIsNone(seen["auth"])
        self.assertEqual([v.id for v in vs], ["x"])


if __name__ == "__main__":
    unittest.main()


class TestListRetries(unittest.TestCase):
    """A cold server (the tower's tunnel) used to cost the user every voice.

    One slow answer collapsed the whole menu to the default voice with no
    explanation. Now the list is retried, and a real failure names itself in the
    menu instead of looking like the server only has one voice.
    """

    def test_a_transient_failure_is_retried(self):
        p = OpenAICompat()
        calls = {"n": 0}

        def flaky(req, timeout=0):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.URLError("connection reset")
            return _Resp(json.dumps({"voices": [{"id": "af_bella"},
                                                {"id": "am_adam"}]}).encode())

        with mock.patch("providers.openai.urllib.request.urlopen", flaky), \
             mock.patch("providers.openai.time.sleep"):
            vs = p.list_voices({"base_url": "https://tower:8443", "key": "tok"})
        self.assertEqual(calls["n"], 2)
        self.assertEqual([v.id for v in vs], ["af_bella", "am_adam"])

    def test_a_real_failure_says_why(self):
        p = OpenAICompat()

        def denied(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        with mock.patch("providers.openai.urllib.request.urlopen", denied), \
             mock.patch("providers.openai.time.sleep"):
            vs = p.list_voices({"base_url": "https://tower:8443", "key": "bad"})
        self.assertEqual(len(vs), 1)
        self.assertIn("rejected the key", vs[0].label)

    def test_a_failure_still_leaves_a_usable_voice(self):
        p = OpenAICompat()

        def dead(req, timeout=0):
            raise urllib.error.URLError("name or service not known")

        with mock.patch("providers.openai.urllib.request.urlopen", dead), \
             mock.patch("providers.openai.time.sleep"):
            vs = p.list_voices({"base_url": "http://nowhere:8880"})
        self.assertEqual(vs[0].id, "af_heart")
