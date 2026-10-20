"""Pronunciation fixes: store, apply, cache tag, and render wiring."""

import os
import tempfile
import unittest

from host import connections, jobs, library, pron
from host.contracts import Audio, Book, Chapter, Quote, Report
from host.db import connect
from storage.local import LocalBackend


class FakeSpeech:
    id = "fake"
    ext = "mp3"
    mime = "audio/mpeg"
    needs_key = False

    def __init__(self):
        self.calls = 0
        self.texts = []

    def synth(self, text, voice, opts, cred):
        self.calls += 1
        self.texts.append(text)
        return Audio(data=text.encode(), ext="mp3", mime="audio/mpeg")

    def list_voices(self, cred):
        return []

    def quote(self, ctx):
        return Quote()

    def report(self, ctx):
        return Report(charge="charged")


def _book(texts=("Qui-Gon spoke.", "nothing here.", "Qui-Gon again.")):
    return Book(title="B", chapters=[Chapter("c%d" % i, t) for i, t in enumerate(texts)])


class TestPron(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")

    def test_save_load_roundtrip_and_tidy(self):
        rules = [{"from": "Qui-Gon", "to": "Kwai-Gon"},
                 {"from": "  ", "to": "x"},          # blank dropped
                 {"from": "Qui-Gon", "to": "dup"},   # duplicate dropped
                 {"from": "Jinn", "to": "Jin"}]
        out = pron.save(self.conn, "u1", rules)
        self.assertEqual([r["from"] for r in out], ["Qui-Gon", "Jinn"])
        self.assertEqual(pron.load(self.conn, "u1"), out)
        self.assertEqual(pron.load(self.conn, "nobody"), [])

    def test_apply_whole_word_case_insensitive(self):
        rules = [{"from": "Qui-Gon", "to": "Kwai-Gon"}]
        self.assertEqual(pron.apply("Qui-Gon Jinn met qui-gon.", rules),
                         "Kwai-Gon Jinn met Kwai-Gon.")
        self.assertEqual(pron.apply("aQui-Gonb", rules), "aQui-Gonb")

    def test_token_for_only_matching_rules(self):
        rules = [{"from": "Qui-Gon", "to": "Kwai-Gon"},
                 {"from": "Hermione", "to": "Her-my-oh-nee"}]
        t1 = pron.token_for("Qui-Gon spoke.", rules)
        self.assertTrue(t1)
        self.assertEqual(pron.token_for("nothing here", rules), "")
        # a chapter that only has Qui-Gon is unaffected by the Hermione rule
        self.assertEqual(t1, pron.token_for("Qui-Gon spoke.", [rules[0]]))

    def test_prefs_and_pron_do_not_clobber(self):
        pron.save(self.conn, "u2", [{"from": "A", "to": "B"}])
        connections.set_prefs(self.conn, "u2", voice="v")
        self.assertEqual(pron.load(self.conn, "u2"), [{"from": "A", "to": "B"}])
        self.assertEqual(connections.get_prefs(self.conn, "u2")["default_voice"], "v")
        connections.set_prefs(self.conn, "u2", connection_id="c1")
        self.assertEqual(pron.load(self.conn, "u2"), [{"from": "A", "to": "B"}])


class TestRenderAppliesPron(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fireside-pron-")
        self.storage = LocalBackend(self.tmp + "/blobs")
        self.conn = connect(":memory:")
        library.create_user(self.conn, "u1")
        self.bid = library.add_book(self.conn, self.storage, "u1", _book())
        self.speech = FakeSpeech()

    def test_rule_applied_and_only_matching_chapters_reencode(self):
        pron.save(self.conn, "u1", [{"from": "Qui-Gon", "to": "Kwai-Gon"}])
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid, voice="v1")
        self.assertIn("Kwai-Gon spoke.", self.speech.texts)
        self.assertEqual(self.speech.calls, 3)
        # same rules again: every chapter is a cache hit, nothing is synthesized
        self.speech.calls = 0
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid, voice="v1")
        self.assertEqual(self.speech.calls, 0, "unchanged pron must hit the cache")
        # add a rule that only touches chapter 1; chapter 0 must not re-synth
        pron.save(self.conn, "u1", [{"from": "Qui-Gon", "to": "Kwai-Gon"},
                                    {"from": "nothing", "to": "nuthin"}])
        self.speech.calls = 0
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid, voice="v1")
        self.assertEqual(self.speech.calls, 1, "only the chapter with the new word re-renders")
        self.assertIn("nuthin here.", self.speech.texts)


class TestPronRoutes(unittest.TestCase):
    def test_set_then_get_over_the_app(self):
        from host.app import App
        tmp = tempfile.mkdtemp(prefix="fireside-pronapp-")
        app = App(plugins_root=os.path.join(tmp, "plugins"),
                  db_path=os.path.join(tmp, "a.db"), require_auth=False)
        st, out = app.pron_set({"user": "u1"},
                               {"rules": [{"from": "Qui-Gon", "to": "Kwai-Gon"}]})
        self.assertEqual(st, 200)
        self.assertEqual(out["rules"], [{"from": "Qui-Gon", "to": "Kwai-Gon"}])
        st, out = app.pron_get({"user": "u1"}, {})
        self.assertEqual(st, 200)
        self.assertEqual(out["rules"], [{"from": "Qui-Gon", "to": "Kwai-Gon"}])


if __name__ == "__main__":
    unittest.main()
