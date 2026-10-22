"""Library: books + chapters persist, text lives behind storage refs."""

import tempfile
import unittest

from host.contracts import Book, Chapter
from host import library
from host.db import connect
from storage.local import LocalBackend


def _book(title="Fire", texts=("one", "two", "three")):
    return Book(title=title, chapters=[Chapter("Ch %d" % i, t)
                                       for i, t in enumerate(texts)])


class TestLibrary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fireside-lib-")
        self.storage = LocalBackend(self.tmp + "/blobs")
        self.conn = connect(":memory:")

    def test_add_and_list(self):
        library.create_user(self.conn, "u1")
        bid = library.add_book(self.conn, self.storage, "u1", _book())
        books = library.list_books(self.conn, "u1")
        self.assertEqual(len(books), 1)
        self.assertEqual(books[0]["chapters"], 3)
        self.assertEqual(books[0]["rendered"], 0)
        self.assertEqual(library.get_book(self.conn, bid)["title"], "Fire")

    def test_chapter_text_roundtrips_through_storage(self):
        library.create_user(self.conn, "u1")
        bid = library.add_book(self.conn, self.storage, "u1", _book())
        self.assertEqual(library.chapter_text(self.conn, self.storage, bid, 1), "two")

    def test_same_text_shares_one_blob(self):
        library.create_user(self.conn, "u1")
        a = library.add_book(self.conn, self.storage, "u1", _book(texts=("dup", "x")))
        b = library.add_book(self.conn, self.storage, "u1", _book(texts=("dup", "y")))
        ra = self.conn.execute("SELECT text_ref FROM chapters WHERE book_id=? AND idx=0",
                               (a,)).fetchone()["text_ref"]
        rb = self.conn.execute("SELECT text_ref FROM chapters WHERE book_id=? AND idx=0",
                               (b,)).fetchone()["text_ref"]
        self.assertEqual(ra, rb, "same text should be one content-addressed blob")

    def test_books_are_per_user(self):
        library.create_user(self.conn, "u1")
        library.create_user(self.conn, "u2")
        library.add_book(self.conn, self.storage, "u1", _book())
        self.assertEqual(library.list_books(self.conn, "u2"), [])

    def test_rendition_by_voice_can_name_the_speed(self):
        """Two rows for one voice at different speeds are different audio.

        The player already knows which row it showed the user, speed included,
        so asking for a voice without a speed used to be able to hand back the
        other row's blob - a different length, and a player that checks what it
        got would refuse to play it.
        """
        library.create_user(self.conn, "u1")
        bid = library.add_book(self.conn, self.storage, "u1", _book())
        for speed, tag in ((1.0, "normal"), (1.1, "fast")):
            library.add_rendition(self.conn, bid, 0, "fake", "v", "", speed,
                                  "ref-" + tag, 100, voice_label="V")
        self.assertEqual(library.rendition(self.conn, bid, 0, "fake", "v")["audio_ref"],
                         "ref-normal")
        self.assertEqual(
            library.rendition(self.conn, bid, 0, "fake", "v", speed=1.1)["audio_ref"],
            "ref-fast")
        self.assertEqual(
            library.rendition(self.conn, bid, 0, "fake", "v", speed="1.0")["audio_ref"],
            "ref-normal")


if __name__ == "__main__":
    unittest.main()
