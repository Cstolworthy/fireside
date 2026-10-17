"""Client core: verify before play, pull-only offline, sequential advance."""

import hashlib
import unittest

from client.core import Player, VerifyError, offline_plan, verify


def _ch(idx, ref, pv="azure", size=None):
    return {"idx": idx, "audio_ref": ref, "audio_bytes": size,
            "provider": pv, "voice": "v", "model": ""}


def _ref(data):
    return hashlib.sha256(data).hexdigest()


class TestVerify(unittest.TestCase):
    def test_ok(self):
        d = b"audio"
        self.assertTrue(verify(d, _ref(d), len(d)))

    def test_bad_checksum(self):
        with self.assertRaises(VerifyError):
            verify(b"audio", "0" * 64)

    def test_bad_size(self):
        d = b"audio"
        with self.assertRaises(VerifyError):
            verify(d, _ref(d), 999)


class TestOfflinePlan(unittest.TestCase):
    def test_pull_only_reports_unconverted(self):
        chapters = [_ch(0, _ref(b"a")), _ch(1, None), _ch(2, _ref(b"c"))]
        plan = offline_plan(chapters)
        self.assertEqual(plan["downloadable"], [0, 2])
        self.assertEqual(plan["not_ready"], [1])


class TestPlayer(unittest.TestCase):
    def setUp(self):
        self.data = [b"a", b"b", b"c"]
        self.chapters = [_ch(i, _ref(d)) for i, d in enumerate(self.data)]

    def test_cannot_play_unverified(self):
        p = Player(self.chapters)
        self.assertFalse(p.can_play(0))
        p.accept(0, self.data[0])
        self.assertTrue(p.can_play(0))

    def test_accept_rejects_bad_bytes(self):
        p = Player(self.chapters)
        with self.assertRaises(VerifyError):
            p.accept(0, b"wrong")

    def test_resume_only_if_ready(self):
        p = Player(self.chapters)
        self.assertFalse(p.resume_at(1, 5.0))
        p.accept(1, self.data[1])
        self.assertTrue(p.resume_at(1, 5.0))
        self.assertEqual(p.chapter_index, 1)

    def test_sequential_advance_only_same_voice(self):
        self.chapters[1]["voice"] = "other"
        p = Player(self.chapters)
        for i, d in enumerate(self.data):
            p.accept(i, d)
        self.assertIsNone(p.next_chapter(), "voice change must stop the run")

    def test_advance_stops_at_unverified_gap(self):
        p = Player(self.chapters)
        p.accept(0, self.data[0])
        self.assertIsNone(p.next_chapter())
        p.accept(1, self.data[1])
        self.assertEqual(p.next_chapter(), 1)


if __name__ == "__main__":
    unittest.main()
