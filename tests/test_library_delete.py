"""Deleting from a shelf: drop one voice, every voice, or the whole book.

The blob store is content-addressed, so a book held by two people shares one
copy and the shared render cache keeps a voice's audio after it is removed. A
delete frees THIS user's storage; a blob any row still names stays on disk.
"""

import tempfile
import unittest

from host.contracts import Book, Chapter
from host import library, quota
from host.db import connect, now
from storage.local import LocalBackend


class ShelfCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fireside-del-")
        self.storage = LocalBackend(self.tmp + "/blobs")
        self.conn = connect(":memory:")
        for uid in ("u1", "u2"):
            library.create_user(self.conn, uid)

    def _book(self, uid="u1", texts=("one", "two")):
        return library.add_book(
            self.conn, self.storage, uid,
            Book(title="Fire", chapters=[Chapter("Ch %d" % i, t)
                                         for i, t in enumerate(texts)]))

    def _voice(self, bid, idx, voice, data):
        """Store audio, and record it the way the renderer does: a rendition
        plus the chapter's default pointer."""
        ref = self.storage.put(data)
        library.add_rendition(self.conn, bid, idx, "local", voice, "", 1.0,
                              ref, len(data), None, voice_label=voice)
        self.conn.execute(
            "UPDATE chapters SET audio_ref=?,audio_bytes=?,provider='local',"
            "voice=?,model='',speed=1.0 WHERE book_id=? AND idx=?",
            (ref, len(data), voice, bid, idx))
        self.conn.commit()
        return ref


class TestRemoveRenditions(ShelfCase):
    def test_removes_one_voice_and_resyncs_the_chapter(self):
        bid = self._book()
        a = self._voice(bid, 0, "Alice", b"alice-0")
        b = self._voice(bid, 0, "Bob", b"bob-0")
        out = library.remove_renditions(self.conn, self.storage, bid, voice="Alice")
        self.assertEqual(out["removed"], 1)
        voices = {r["voice"] for r in library.renditions(self.conn, bid)}
        self.assertEqual(voices, {"Bob"})
        row = [c for c in library.chapters(self.conn, bid) if c["idx"] == 0][0]
        self.assertEqual(row["voice"], "Bob")
        self.assertEqual(row["audio_ref"], b)
        self.assertFalse(self.storage.has(a), "alice's own bytes are gone")

    def test_removing_the_only_voice_clears_the_chapter(self):
        bid = self._book()
        self._voice(bid, 0, "Alice", b"alice-0")
        library.remove_renditions(self.conn, self.storage, bid, voice="Alice")
        row = [c for c in library.chapters(self.conn, bid) if c["idx"] == 0][0]
        self.assertIsNone(row["audio_ref"])
        self.assertIsNone(row["voice"])

    def test_all_voices_keeps_the_book_and_its_text(self):
        bid = self._book()
        self._voice(bid, 0, "Alice", b"a0")
        self._voice(bid, 1, "Bob", b"b1")
        out = library.remove_renditions(self.conn, self.storage, bid)
        self.assertEqual(out["removed"], 2)
        self.assertEqual(library.renditions(self.conn, bid), [])
        self.assertIsNotNone(library.get_book(self.conn, bid))
        self.assertEqual(library.chapter_text(self.conn, self.storage, bid, 0), "one")

    def test_a_cached_voice_keeps_its_bytes(self):
        """The shared render cache is why re-voicing is free. Removing the voice
        from the shelf must not delete audio the cache still points at."""
        bid = self._book()
        ref = self._voice(bid, 0, "Alice", b"alice-0")
        self.conn.execute(
            "INSERT INTO render_cache(`key`,audio_ref,audio_bytes,provider,voice,"
            "model,created_at) VALUES(?,?,?,?,?,?,?)",
            ("k1", ref, 8, "local", "Alice", "", now()))
        self.conn.commit()
        library.remove_renditions(self.conn, self.storage, bid, voice="Alice")
        self.assertTrue(self.storage.has(ref), "a cached blob stays")


class TestDeleteBook(ShelfCase):
    def test_removes_rows_and_frees_unshared_bytes(self):
        bid = self._book()
        ref = self._voice(bid, 0, "Alice", b"alice-0")
        text_ref = self.conn.execute(
            "SELECT text_ref FROM chapters WHERE book_id=? AND idx=0",
            (bid,)).fetchone()["text_ref"]
        library.delete_book(self.conn, self.storage, bid)
        self.assertIsNone(library.get_book(self.conn, bid))
        self.assertEqual(library.chapters(self.conn, bid), [])
        self.assertFalse(self.storage.has(ref))
        self.assertFalse(self.storage.has(text_ref))
        self.assertEqual(quota.usage(self.conn, self.storage, "u1")["own_bytes"], 0)

    def test_shared_with_another_shelf_keeps_the_blobs(self):
        """The same book held twice shares text and audio bytes. Removing one
        shelf takes that shelf's copy; the other's bytes stay."""
        bid1 = self._book(uid="u1", texts=("shared",))
        bid2 = self._book(uid="u2", texts=("shared",))
        r1 = self._voice(bid1, 0, "Alice", b"same-audio")
        r2 = self._voice(bid2, 0, "Alice", b"same-audio")
        self.assertEqual(r1, r2, "identical bytes are one blob")
        library.delete_book(self.conn, self.storage, bid1)
        self.assertTrue(self.storage.has(r1), "u2 still holds this audio")
        self.assertEqual(len(library.chapters(self.conn, bid2)), 1)
        self.assertIsNotNone(library.get_book(self.conn, bid2))
        # now the last holder drops it: the blob goes
        library.delete_book(self.conn, self.storage, bid2)
        self.assertFalse(self.storage.has(r1))


class TestRefInUse(ShelfCase):
    def test_unknown_ref_is_not_in_use(self):
        self.assertFalse(library.ref_in_use(self.conn, "deadbeef"))
        self.assertFalse(library.ref_in_use(self.conn, ""))


if __name__ == "__main__":
    unittest.main()
