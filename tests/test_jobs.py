"""Render pipeline: cache on (text, provider, voice, model, speed), progress."""

import tempfile
import unittest

from host.contracts import Audio, Book, Chapter, Quote, Report
from host import jobs, library
from host.db import connect
from storage.local import LocalBackend


class FakeSpeech:
    id = "fake"
    ext = "mp3"
    mime = "audio/mpeg"
    needs_key = False

    def __init__(self):
        self.calls = 0

    def synth(self, text, voice, opts, cred):
        self.calls += 1
        return Audio(data=("A[%s|%s]%s" % (voice, opts.get("model", ""), text)).encode(),
                     ext="mp3", mime="audio/mpeg")

    def list_voices(self, cred):
        return []

    def quote(self, ctx):
        return Quote()

    def report(self, ctx):
        return Report(charge="charged")


def _book(texts=("aa", "bb", "cc")):
    return Book(title="B", chapters=[Chapter("c%d" % i, t) for i, t in enumerate(texts)])


class TestRender(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fireside-jobs-")
        self.storage = LocalBackend(self.tmp + "/blobs")
        self.conn = connect(":memory:")
        library.create_user(self.conn, "u1")
        self.bid = library.add_book(self.conn, self.storage, "u1", _book())
        self.speech = FakeSpeech()

    def test_render_book_sets_audio_refs_and_job(self):
        jid = jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid,
                               voice="v1", model="m1")
        j = jobs.job(self.conn, jid)
        self.assertEqual(j["status"], "done")
        self.assertEqual(j["current"], 3)
        rows = library.chapters(self.conn, self.bid)
        self.assertTrue(all(r["audio_ref"] for r in rows))
        for r in rows:
            self.assertTrue(self.storage.has(r["audio_ref"]))

    def test_second_render_hits_cache(self):
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid, voice="v1")
        before = self.speech.calls
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid, voice="v1")
        self.assertEqual(self.speech.calls, before, "same voice must reuse the cache")

    def test_voice_change_is_a_new_render(self):
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid, voice="v1")
        before = self.speech.calls
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid, voice="v2")
        self.assertGreater(self.speech.calls, before, "new voice must synthesize again")

    def test_every_voice_is_kept(self):
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid, voice="v1")
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid, voice="v2")
        rs = library.renditions(self.conn, self.bid)
        self.assertEqual({r["voice"] for r in rs}, {"v1", "v2"})
        per = {}
        for r in rs:
            per.setdefault(r["idx"], set()).add(r["voice"])
        self.assertTrue(all(v == {"v1", "v2"} for v in per.values()),
                        "a chapter re-voiced keeps both renditions")
        rows = {r["idx"]: r for r in library.chapters(self.conn, self.bid)}
        self.assertEqual(rows[0]["voice"], "v2", "the chapter row points at the newest")
        self.assertEqual(library.rendition(self.conn, self.bid, 0, voice="v1")["voice"], "v1")

    def test_backfill_seeds_renditions_from_chapters(self):
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid, voice="v1")
        self.conn.execute("DELETE FROM renditions")
        library.backfill_renditions(self.conn)
        self.assertEqual({r["voice"] for r in library.renditions(self.conn, self.bid)}, {"v1"})

    def test_cache_shared_across_books(self):
        b2 = library.add_book(self.conn, self.storage, "u1", _book())
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid, voice="v1")
        before = self.speech.calls
        jobs.render_book(self.conn, self.storage, self.speech, "u1", b2, voice="v1")
        self.assertEqual(self.speech.calls, before, "same text+voice shares the global cache")

    def test_only_subset(self):
        jid = jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid,
                               voice="v1", only=[0, 2])
        rows = {r["idx"]: r for r in library.chapters(self.conn, self.bid)}
        self.assertTrue(rows[0]["audio_ref"])
        self.assertIsNone(rows[1]["audio_ref"])
        self.assertTrue(rows[2]["audio_ref"])
        self.assertEqual(jobs.job(self.conn, jid)["current"], 2)

    def test_render_error_is_recorded(self):
        class Boom(FakeSpeech):
            def synth(self, text, voice, opts, cred):
                raise RuntimeError("no key")
        jid = jobs.render_book(self.conn, self.storage, Boom(), "u1", self.bid, voice="v1")
        j = jobs.job(self.conn, jid)
        self.assertEqual(j["status"], "error")
        self.assertIn("no key", j["error"])


if __name__ == "__main__":
    unittest.main()

class ReservedWordColumns(unittest.TestCase):
    """MySQL reserves the word `key`; the render-cache column must be quoted so
    the dialect stays valid once FIRESIDE_DB_TYPE=mysql."""

    class _Spy:
        def __init__(self, real):
            self._real = real
            self.sql = []

        def execute(self, sql, *args):
            self.sql.append(sql)
            return self._real.execute(sql, *args)

        def commit(self):
            return self._real.commit()

    def test_render_cache_quotes_key(self):
        tmp = tempfile.mkdtemp(prefix="fireside-jobs-")
        storage = LocalBackend(tmp + "/blobs")
        real = connect(":memory:")
        library.create_user(real, "u1")
        bid = library.add_book(real, storage, "u1", _book(("aa",)))
        spy = self._Spy(real)
        jobs.render_book(spy, storage, FakeSpeech(), "u1", bid, voice="v1")
        hits = [s for s in spy.sql if "render_cache" in s.lower()]
        self.assertTrue(hits, "render cache was never touched")
        for sql in hits:
            low = sql.lower()
            self.assertNotIn(" key=", low)
            self.assertNotIn("(key", low)
            self.assertIn("`key`", low)
