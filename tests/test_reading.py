"""Reading blocks: the chapter cut into sentences with a spoken length each.

The map is an estimate (one audio file per chapter, no stored chunk offsets), so
what matters here is that a sentence stays whole, a pause that speaks nothing
weighs nothing, and the audio clock lands inside the right block.
"""

import unittest

from host import reading


def blocks(text, weigh=None):
    return reading.sentences(text, weigh=weigh)


class TestSentences(unittest.TestCase):
    def test_paragraphs_and_sentences(self):
        b = blocks("One thing. Two things!\n\nA new paragraph? Yes.")
        self.assertEqual([x["t"] for x in b],
                         ["One thing.", "Two things!", "A new paragraph?", "Yes."])
        self.assertEqual([x["p"] for x in b], [0, 0, 1, 1])

    def test_titles_and_initials_do_not_break_a_sentence(self):
        b = blocks("Mr. Smith met Dr. Who and J. R. R. Tolkien at noon.")
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0]["t"], "Mr. Smith met Dr. Who and J. R. R. Tolkien at noon.")

    def test_a_number_is_not_a_sentence_end(self):
        b = blocks("It costs 3.5 dollars. That is all.")
        self.assertEqual([x["t"] for x in b], ["It costs 3.5 dollars.", "That is all."])

    def test_closing_quotes_belong_to_the_sentence(self):
        b = blocks('"Yes," she said. "No," he said.')
        self.assertEqual(b[0]["t"], '"Yes," she said.')
        self.assertEqual(b[1]["t"], '"No," he said.')

    def test_whitespace_is_collapsed(self):
        b = blocks("A   line\nwrapped   here.  And  more.")
        self.assertEqual(b[0]["t"], "A line wrapped here.")

    def test_weigh_is_used_and_zero_is_kept(self):
        b = blocks("~~~\n\nThe bell rang.", weigh=lambda s: 0 if "~" in s else len(s))
        self.assertEqual([x["c"] for x in b], [0, 14])

    def test_empty_text(self):
        self.assertEqual(blocks(""), [])
        self.assertEqual(blocks(None), [])


class TestLocate(unittest.TestCase):
    def test_halfway_lands_in_the_middle_block(self):
        b = blocks("Aaaa. Bbbb. Cccc. Dddd.")
        self.assertEqual(reading.locate(b, 0.0), 0)
        self.assertEqual(reading.locate(b, 0.5), 2)
        self.assertEqual(reading.locate(b, 1.0), 3)

    def test_a_silent_block_is_skipped_not_held(self):
        b = reading.sentences("AAAA.\n\n~~~~\n\nBBBB.",
                              weigh=lambda s: 0 if "~" in s else len(s))
        self.assertEqual(len(b), 3)
        self.assertEqual(b[1]["c"], 0)
        # nothing can land on the silent block: it occupies no time
        seen = {reading.locate(b, i / 100.0) for i in range(101)}
        self.assertNotIn(1, seen)
        self.assertIn(0, seen)
        self.assertIn(2, seen)

    def test_nothing_to_point_at(self):
        self.assertEqual(reading.locate([], 0.5), -1)
        self.assertEqual(reading.locate(blocks("All silence."), 0.5), 0)
        b = reading.sentences("~~~~", weigh=lambda s: 0)
        self.assertEqual(reading.locate(b, 0.5), -1)

    def test_a_nonsense_fraction_does_not_raise(self):
        b = blocks("Aaaa. Bbbb.")
        self.assertEqual(reading.locate(b, "later"), -1)
        self.assertEqual(reading.locate(b, float("nan")), -1)
        self.assertEqual(reading.locate(b, -5), 0)
        self.assertEqual(reading.locate(b, 99), 1)


if __name__ == "__main__":
    unittest.main()
