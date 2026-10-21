"""The OpenAI-compatible provider must send its token on the voice list too.

A gated server (our tower) answers 401 to an unkeyed /v1/audio/voices, which the
provider silently turns into a one-voice fallback. This pins the header on that
call, the same one synth already sends.
"""

import json
import unittest
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
