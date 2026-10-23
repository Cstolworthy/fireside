"""Speech providers as units: the HTTP they send and the bytes they return.

No network. Each provider module's `urllib.request.urlopen` is replaced with a
fake that records the request and answers with a canned body, so the tests
assert on the request shape (URL, headers, SSML, JSON) and on how a response is
turned into audio, retries, and voice labels.
"""

import io
import json
import unittest

from providers import get_provider
from providers._accounting import AccountingMixin
from providers import _text
import providers.azure as azure
import providers.local as local
import providers.minimax as minimax
import providers.openai as openai


class Resp:
    def __init__(self, body, charset="utf-8"):
        self._b = body if isinstance(body, bytes) else body.encode()
        self.headers = self

    def read(self, n=None):
        return self._b[:n] if n else self._b

    def get_content_charset(self):
        return "utf-8"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Fake:
    """Replaces urlopen in one module. `answer` is a callable(req) -> bytes or
    an exception to raise; every request is kept in .reqs."""

    def __init__(self, answer):
        self.answer = answer
        self.reqs = []

    def __call__(self, req, timeout=None):
        self.reqs.append(req)
        out = self.answer(req)
        if isinstance(out, Exception):
            raise out
        return Resp(out)

    def body(self, i=0):
        return self.reqs[i].data

    def headers(self, i=0):
        return dict(self.reqs[i].header_items()) if hasattr(self.reqs[i], "header_items") else {}


class TestAzure(unittest.TestCase):
    def setUp(self):
        self.p = get_provider("azure")
        self.saved = azure.urllib.request.urlopen

    def tearDown(self):
        azure.urllib.request.urlopen = self.saved

    def test_voices_keep_only_neural_and_sort_english_first(self):
        payload = json.dumps([
            {"ShortName": "fr-FR-DeniseNeural", "DisplayName": "Microsoft Denise Online (Natural) - French",
             "Locale": "fr-FR", "Gender": "Female", "VoiceType": "Neural"},
            {"ShortName": "en-US-JennyNeural", "DisplayName": "Microsoft Jenny Online (Natural) - English (United States)",
             "Locale": "en-US", "Gender": "Female", "VoiceType": "Neural"},
            {"ShortName": "en-US-Old", "DisplayName": "Old", "Locale": "en-US",
             "Gender": "Female", "VoiceType": "Standard"},
        ])
        azure.urllib.request.urlopen = Fake(lambda req: payload)
        vs = self.p.list_voices({"key": "K", "region": "westus"})
        self.assertEqual([v.id for v in vs], ["en-US-JennyNeural", "fr-FR-DeniseNeural"])
        self.assertEqual(vs[0].label, "Jenny (en-US, Female)")
        self.assertIn("westus.tts.speech.microsoft.com", azure.urllib.request.urlopen.reqs[0].full_url)
        self.assertEqual(azure.urllib.request.urlopen.headers(0)["Ocp-apim-subscription-key"], "K")

    def test_voice_name_forms(self):
        self.assertEqual(azure._voice_name("Microsoft Guy Online (Natural) - English"), "Guy")
        self.assertEqual(azure._voice_name("en-US-JennyMultilingualNeural"), "JennyMultilingual")
        self.assertEqual(azure._voice_name("Plain Name"), "Plain Name")
        self.assertEqual(azure._voice_name(""), "")

    def test_synth_chunks_and_reports(self):
        azure.urllib.request.urlopen = Fake(lambda req: b"AUDIO")
        seen = []
        text = ("One sentence here. " * 400).strip()      # > 3500 chars -> several parts
        a = self.p.synth(text, "en-US-JennyNeural", {"rate": "-10%",
                                                    "report": lambda n, t: seen.append((n, t))},
                         {"key": "K", "region": "eastus"})
        self.assertEqual(a.ext, ".mp3")
        self.assertEqual(a.mime, "audio/mpeg")
        self.assertGreater(len(azure.urllib.request.urlopen.reqs), 1)
        self.assertEqual(a.bytes, b"AUDIO" * len(azure.urllib.request.urlopen.reqs))
        self.assertEqual(seen[-1][0], seen[-1][1])
        self.assertAlmostEqual(a.cost_usd, round(15.0 * len(text) / 1e6, 6), places=6)
        ssml = azure.urllib.request.urlopen.body().decode()
        self.assertIn("<prosody rate='-10%'>", ssml)
        self.assertIn("audio-24khz-48kbitrate-mono-mp3",
                      azure.urllib.request.urlopen.headers()["X-microsoft-outputformat"])

    def test_synth_escapes_markup(self):
        azure.urllib.request.urlopen = Fake(lambda req: b"")
        self.p.synth("a < b & c", "v", {}, {"key": "K", "region": "eastus"})
        self.assertIn("a &lt; b &amp; c", azure.urllib.request.urlopen.body().decode())

    def test_contract_bits(self):
        self.assertTrue(self.p.needs_key)
        self.assertEqual(self.p.cost_per_1m_chars({}), 15.0)
        self.assertEqual(self.p.split("hi"), ["hi"])
        self.assertEqual([f.name for f in self.p.settings()], ["key", "region"])


class TestMiniMax(unittest.TestCase):
    def setUp(self):
        self.p = get_provider("minimax")
        self.saved = minimax.urllib.request.urlopen

    def tearDown(self):
        minimax.urllib.request.urlopen = self.saved

    def test_voices_from_server(self):
        minimax.urllib.request.urlopen = Fake(lambda req: json.dumps(
            {"system_voice": [{"voice_id": "v1", "voice_name": "One"}]}))
        vs = self.p.list_voices({"key": "K", "group": "G"})
        self.assertEqual([(v.id, v.label) for v in vs], [("v1", "One")])
        self.assertIn("GroupId=G", minimax.urllib.request.urlopen.reqs[0].full_url)

    def test_voices_fall_back_to_the_builtin_four(self):
        minimax.urllib.request.urlopen = Fake(lambda req: urllib_error())
        vs = self.p.list_voices({"key": "K", "group": "G"})
        self.assertEqual(len(vs), 4)
        self.assertEqual(vs[0].id, "English_Gentle-voiced_man")

    def test_synth_hex_decodes(self):
        payload = json.dumps({"data": {"audio": b"RIFF".hex()}})
        minimax.urllib.request.urlopen = Fake(lambda req: payload)
        a = self.p.synth("hello", "v1", {"speed": 1.2, "model": "m"},
                         {"key": "K", "group": "G"})
        self.assertEqual(a.bytes, b"RIFF")
        self.assertIsNone(a.cost_usd)
        body = json.loads(minimax.urllib.request.urlopen.body().decode())
        self.assertEqual(body["voice_setting"]["speed"], 1.2)
        self.assertEqual(body["model"], "m")

    def test_synth_without_audio_is_an_error(self):
        minimax.urllib.request.urlopen = Fake(lambda req: json.dumps({"data": {}}))
        with self.assertRaises(ValueError):
            self.p.synth("hello", "v1", {}, {"key": "K", "group": "G"})

    def test_unpriced_not_free(self):
        self.assertIsNone(self.p.cost_per_1m_chars({}))
        self.assertEqual(self.p.split("x"), ["x"])


class TestLocal(unittest.TestCase):
    def test_tone_is_a_real_wav(self):
        import wave
        p = get_provider("local")
        seen = []
        a = p.synth("hello there", "tone", {"report": lambda n, t: seen.append(n)}, {})
        self.assertEqual(a.ext, ".wav")
        self.assertEqual(a.cost_usd, 0.0)
        w = wave.open(io.BytesIO(a.bytes))
        self.assertEqual(w.getframerate(), 16000)
        self.assertGreater(w.getnframes(), 0)
        self.assertEqual(seen, [1])
        self.assertEqual(p.chunks("anything"), 1)
        self.assertEqual(p.split("a b"), ["a b"])
        self.assertEqual([v.id for v in p.list_voices({})], ["tone"])
        self.assertEqual(p.settings(), [])
        self.assertFalse(p.needs_key)


class TestOpenAICompat(unittest.TestCase):
    def setUp(self):
        self.p = get_provider("openai")
        self.saved, self.saved_sleep = openai.urllib.request.urlopen, openai.time.sleep
        openai.time.sleep = lambda s: None

    def tearDown(self):
        openai.urllib.request.urlopen = self.saved
        openai.time.sleep = self.saved_sleep

    def test_voice_list_and_key_header(self):
        openai.urllib.request.urlopen = Fake(lambda req: json.dumps(
            {"voices": [{"id": "b", "name": "Bee"}, {"id": "a"}, {"name": "no id"}]}))
        vs = self.p.list_voices({"base_url": "http://box:8880/", "key": "T"})
        # an entry with no id falls back to its name as the id, which sorts last
        self.assertEqual([v.id for v in vs], ["a", "b", "no id"])
        self.assertEqual(vs[1].label, "Bee")
        self.assertEqual(openai.urllib.request.urlopen.headers()["Authorization"], "Bearer T")
        self.assertTrue(openai.urllib.request.urlopen.reqs[0].full_url.startswith(
            "http://box:8880/v1/audio/voices"))

    def test_empty_list_says_so(self):
        openai.urllib.request.urlopen = Fake(lambda req: json.dumps({"voices": []}))
        vs = self.p.list_voices({})
        self.assertEqual(len(vs), 1)
        self.assertIn("empty voice list", vs[0].label)

    def test_dead_server_names_the_reason_and_retries(self):
        err = urllib_error(code=404)
        openai.urllib.request.urlopen = Fake(lambda req: err)
        vs = self.p.list_voices({})
        self.assertIn("no voice list at that address", vs[0].label)
        self.assertEqual(len(openai.urllib.request.urlopen.reqs), 3)

    def test_why_maps_errors(self):
        self.assertEqual(openai._why(urllib_error(code=401)), "server rejected the key")
        self.assertEqual(openai._why(urllib_error(code=403)), "key not allowed")
        self.assertEqual(openai._why(urllib_error(code=500)), "server said 500")
        self.assertEqual(openai._why(RuntimeError("timed out")), "server did not answer")
        self.assertEqual(openai._why(RuntimeError("nope")), "server did not answer")

    def test_synth_posts_json_and_concatenates(self):
        openai.urllib.request.urlopen = Fake(lambda req: b"MP3")
        seen = []
        a = self.p.synth("short text", "af_heart", {"speed": 1.5, "model": "kokoro",
                                                    "report": lambda n, t: seen.append(n)}, {})
        self.assertEqual(a.bytes, b"MP3")
        self.assertEqual(seen, [1])
        body = json.loads(openai.urllib.request.urlopen.body().decode())
        self.assertEqual(body["voice"], "af_heart")
        self.assertEqual(body["speed"], 1.5)
        self.assertEqual(body["model"], "kokoro")

    def test_default_voice_and_base(self):
        openai.urllib.request.urlopen = Fake(lambda req: b"")
        self.p.synth("hi", "", {}, {})
        self.assertEqual(json.loads(openai.urllib.request.urlopen.body())["voice"], "af_heart")
        self.assertTrue(openai.urllib.request.urlopen.reqs[0].full_url.startswith(
            openai.DEFAULT_BASE))


class TestTextAndAccounting(unittest.TestCase):
    def test_chunk_text_splits_long_paragraphs(self):
        para = "A sentence here. " * 400
        parts = _text.chunk_text(para, target=500)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 600 for p in parts))

    def test_chunk_text_keeps_short_text_whole(self):
        self.assertEqual(_text.chunk_text("just this"), ["just this"])
        self.assertEqual(_text.chunk_text(""), [""])

    def test_accounting_quote_and_report(self):
        class P(AccountingMixin):
            def cost_per_1m_chars(self, cred):
                return 15.0

        q = P().quote({"chars_est": 1_000_000})
        self.assertEqual(q.price, 15.0)
        self.assertTrue(q.authoritative)
        self.assertEqual(P().report({"chars_actual": 10}).charge, "unknown")

    def test_accounting_unpriced_is_not_zero(self):
        class P(AccountingMixin):
            def cost_per_1m_chars(self, cred):
                return None

        q = P().quote({"chars_est": 1_000_000})
        self.assertFalse(q.authoritative)
        self.assertEqual(q.price, 0.0)


class _Err(Exception):
    def __init__(self, code=None):
        super().__init__("err %s" % code)
        self.code = code


def urllib_error(code=None):
    import urllib.error
    return urllib.error.HTTPError("http://x", code or 500, "e", {}, io.BytesIO(b""))


if __name__ == "__main__":
    unittest.main()
