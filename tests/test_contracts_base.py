"""The contract base classes and the v1->v2 shape adapters.

A plugin is not required to subclass these, but the base classes are the
documented shape and their defaults are what a plugin inherits when it opts in.
An un-implemented method must raise, not silently pass, so a half-written plugin
fails loudly at the seam instead of returning None into the host.
"""

import unittest

from host import contracts as c


class TestReaderBase(unittest.TestCase):
    def test_defaults(self):
        r = c.Reader()
        self.assertEqual(r.order, 100)
        self.assertEqual(r.settings(), [])
        self.assertEqual(r.credential_hosts(), [])
        self.assertEqual(r.transport(), {})
        self.assertTrue(r.consent({}).ok)

    def test_required_methods_raise(self):
        r = c.Reader()
        with self.assertRaises(NotImplementedError):
            r.matches({})
        with self.assertRaises(NotImplementedError):
            r.read({}, None, None)


class TestSpeechBase(unittest.TestCase):
    def test_defaults(self):
        s = c.Speech()
        self.assertEqual((s.ext, s.mime, s.needs_key), ("mp3", "audio/mpeg", True))
        self.assertIsNone(s.cost_per_1m_chars({}))

    def test_required_methods_raise(self):
        s = c.Speech()
        for fn, arg in ((s.list_voices, {}), (s.synth, "t"), (s.quote, {}), (s.report, {})):
            args = (arg, "v", {}, {}) if fn == s.synth else (arg,)
            with self.assertRaises(NotImplementedError):
                fn(*args)


class TestStorageBase(unittest.TestCase):
    def test_size_reads_the_blob_unless_absent(self):
        class S(c.Storage):
            def __init__(self):
                self.bytes = {}

            def put(self, data):
                return "r"

            def has(self, ref):
                return ref in self.bytes

            def get(self, ref):
                return self.bytes[ref]

            def delete(self, ref):
                self.bytes.pop(ref, None)

        s = S()
        self.assertEqual(s.size("missing"), 0)
        s.bytes["r1"] = b"12345"
        self.assertEqual(s.size("r1"), 5)
        self.assertEqual(s.info().kind, "local")

    def test_required_methods_raise(self):
        s = c.Storage()
        for fn, arg in ((s.put, b"x"), (s.has, "r"), (s.get, "r"), (s.delete, "r")):
            with self.assertRaises(NotImplementedError):
                fn(arg)


class TestAdapters(unittest.TestCase):
    def test_to_book_passes_a_book_through(self):
        b = c.Book(title="T")
        self.assertIs(c.to_book(b), b)

    def test_to_book_passes_anything_with_chapters_through(self):
        class Double:
            chapters = []

        d = Double()
        self.assertIs(c.to_book(d), d)

    def test_to_book_coerces_a_v1_dict(self):
        b = c.to_book({"title": "", "chapters": [{"title": "one", "text": "a"},
                                                 c.Chapter(title="two", text="b")]})
        self.assertEqual(b.title, "Untitled")
        self.assertEqual([ch.title for ch in b.chapters], ["one", "two"])
        self.assertEqual(b.chapters[1].text, "b")

    def test_to_book_leaves_an_unknown_shape_alone(self):
        self.assertEqual(c.to_book(42), 42)

    def test_to_audio(self):
        a = c.Audio(data=b"x")
        self.assertIs(c.to_audio(a), a)

        class V1:
            bytes = b"y"
            ext = "wav"
            mime = "audio/wav"

        out = c.to_audio(V1())
        self.assertEqual((out.data, out.ext, out.mime), (b"y", "wav", "audio/wav"))
        self.assertEqual(c.to_audio("nope"), "nope")


if __name__ == "__main__":
    unittest.main()
