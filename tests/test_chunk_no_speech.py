"""A scene break is not a sentence.

Cat's "Junior Hallows" sat with one chapter permanently unvoiced. The chapter
was fine; the eleven-character scene break "~*~*~*~*~*~" was the problem.

The host splits a chapter on raw text. It then applies the user's pronunciation
rules to each chunk - and a rule that adds two characters can push the trailing
paragraph over the cap, so the provider's own re-split hands the server a
request whose whole input is a scene break. Kokoro answers that with 400 "input
contains no speakable text", the chapter is marked failed, and the same wall is
there on every retry.

Two rules came out of it: a piece with nothing to say is never a request, and a
chunk the host already sized is not split again.
"""

import tempfile
import unittest

from host import chunks, jobs, library, pron
from host.contracts import Audio, Book, Chapter
from host.db import connect
from providers._text import CHUNK_TARGET, chunk_text, has_speech, parts_for_request
from storage.local import LocalBackend


SCENE = "~*~*~*~*~*~"


class StrictSpeech:
    """A speech server that rejects a request with nothing to say, like Kokoro.

    Real answer: 400 {"detail": {"message": "Input contains no speakable text"}}.
    """

    id = "strict"
    ext = "mp3"
    mime = "audio/mpeg"
    needs_key = False

    def __init__(self):
        self.texts = []

    def synth(self, text, voice, opts, cred):
        if not has_speech(text):
            raise RuntimeError("HTTP Error 400: Bad Request")
        self.texts.append(text)
        return Audio(data=text.encode(), ext="mp3", mime="audio/mpeg")

    def list_voices(self, cred):
        return []


class ParagraphSpeech(StrictSpeech):
    """Speaks one paragraph per request, so the scene break gets its own turn."""

    def split(self, text):
        return text.split("\n\n")


class TestPartsForRequest(unittest.TestCase):
    def test_symbols_only_is_not_a_sentence(self):
        for text in (SCENE, "***", "* * *", "...", "   ", ""):
            self.assertFalse(has_speech(text), text)
        self.assertTrue(has_speech("Chapter 5"))
        self.assertTrue(has_speech('"Yeah."'))

    def test_a_chunk_under_the_cap_is_one_request(self):
        # the real shape of the bug: a 3499-char chunk whose last paragraph is a
        # scene break - the 3497-char chunk the host made, plus two characters a
        # pronunciation rule added. Re-splitting it emitted [3486, 11].
        text = ("a" * 3486) + "\n\n" + SCENE
        self.assertLessEqual(len(text), CHUNK_TARGET)
        self.assertEqual(len(chunk_text(text)), 2, "this text does re-split")
        self.assertEqual(parts_for_request(text), [text], "so it must not be split again")

    def test_over_the_cap_still_splits_and_drops_silence(self):
        text = ("sentence here. " * 400) + "\n\n" + SCENE
        parts = parts_for_request(text)
        self.assertGreater(len(parts), 1)
        self.assertNotIn(SCENE, parts)
        self.assertTrue(all(has_speech(p) for p in parts))

    def test_nothing_to_say_returns_nothing(self):
        self.assertEqual(parts_for_request(SCENE), [])


class TestChapterSurvivesASceneBreak(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fireside-scene-")
        self.storage = LocalBackend(self.tmp + "/blobs")
        self.conn = connect(":memory:")
        library.create_user(self.conn, "u1")
        self.speech = ParagraphSpeech()

    def test_a_scene_break_chunk_costs_no_request(self):
        book = Book(title="B", chapters=[Chapter("c0", "One line.\n\n" + SCENE + "\n\nAnother line.")])
        bid = library.add_book(self.conn, self.storage, "u1", book)
        jobs.render_book(self.conn, self.storage, self.speech, "u1", bid, voice="v1")
        self.assertNotIn(SCENE, self.speech.texts)
        row = self.conn.execute(
            "SELECT audio_bytes FROM chapters WHERE book_id=? AND idx=0", (bid,)).fetchone()
        self.assertTrue(row["audio_bytes"], "the chapter still gets audio")

    def test_a_chapter_of_nothing_but_symbols_still_finishes(self):
        book = Book(title="B", chapters=[Chapter("c0", SCENE)])
        bid = library.add_book(self.conn, self.storage, "u1", book)
        jobs.render_book(self.conn, self.storage, self.speech, "u1", bid, voice="v1")
        self.assertEqual(self.speech.texts, [])


class FlakySpeech(StrictSpeech):
    """Fails the last chunk once, the way a dropped request does."""

    id = "flaky"
    ext = "mp3"
    mime = "audio/mpeg"
    needs_key = False

    def __init__(self):
        StrictSpeech.__init__(self)
        self.broken = True

    def split(self, text):
        return text.split("\n\n")

    def synth(self, text, voice, opts, cred):
        if "second" in text and self.broken:
            raise RuntimeError("HTTP Error 404: gone")
        self.texts.append(text)
        return Audio(data=text.encode(), ext="mp3", mime="audio/mpeg")


class TestFailedChapterKeepsItsWork(unittest.TestCase):
    """A chapter that dies on its last piece must not throw away the pieces it
    already spoke - on a metered provider that is a re-bill, and on any provider
    it is the whole chapter again on every retry."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fireside-keep-")
        self.storage = LocalBackend(self.tmp + "/blobs")
        self.conn = connect(":memory:")
        library.create_user(self.conn, "u1")
        self.speech = FlakySpeech()

    def test_the_retry_does_not_re_speak_the_finished_chunks(self):
        book = Book(title="B", chapters=[Chapter("c0", "first\n\nsecond")])
        bid = library.add_book(self.conn, self.storage, "u1", book)
        jobs.render_book(self.conn, self.storage, self.speech, "u1", bid, voice="v1")
        self.assertEqual(self.speech.texts, ["first"])
        job = self.conn.execute(
            "SELECT status FROM jobs WHERE book_id=? ORDER BY created_at DESC", (bid,)).fetchone()
        self.assertEqual(job["status"], "error")
        self.speech.broken = False        # the server comes back
        jobs.render_book(self.conn, self.storage, self.speech, "u1", bid, voice="v1")
        self.assertEqual(self.speech.texts, ["first", "second"],
                         "the finished chunk came from the cache")


if __name__ == "__main__":
    unittest.main()
