"""Where the voice is, in the words on the page.

A chapter is one audio file, and the render throws its chunk boundaries away, so
nothing recorded lines the text up against the sound. This module builds the
cheap approximation: split the chapter into sentences, weight each one by how
long it takes to say, and lay them end to end across the recording. Sentence
level, so a pause that runs long costs a sentence of drift, not the chapter's.

Two things it does not do, on purpose:

  * it does not know chunk boundaries. A render that stores them can replace
    the estimate for new books; old books keep this map and keep working.
  * it does not guess words. Azure emits word-boundary events; Kokoro does not,
    so a word-level map would be right on one engine and a lie on the other.

Weights use the text a voice is actually given (`pron.spoken`), so a glyph a
pronunciation rule turns into a word counts as the word, not the glyph. A
sentence that speaks nothing (a scene break, `~~~`) weighs nothing and is
skipped by `locate` instead of holding the highlight still.
"""

import re

PARA = re.compile(r"\n\s*\n+")
SPACE = re.compile(r"\s+")

# A terminal run with any closing quotes or brackets that belong to the
# sentence, followed by whitespace. What comes after decides whether it was
# really the end of something.
_BOUND = re.compile(r"[.!?\u2026]+[\"'\u201d\u2019)\]]*(?=\s)")
_TAIL_WORD = re.compile(r"([A-Za-z][A-Za-z'\u2019.]*)$")
_OPENER = "\"'\u201c\u2018([{\u2014"

# Titles and the usual shorthand. A period after one of these is not the end of
# a sentence often enough to be worth the wrong splits.
_ABBREV = {
    "mr", "mrs", "ms", "mx", "dr", "prof", "sr", "jr", "st", "mt", "vs", "etc",
    "eg", "ie", "cf", "al", "no", "vol", "ch", "pt", "pp", "fig", "approx",
    "dept", "est", "inc", "ltd", "co", "capt", "col", "gen", "lt", "sgt", "rev",
}


def _starts_sentence(rest):
    """Does `rest` (what follows the whitespace) begin a new sentence?"""
    ch = rest[:1]
    if not ch:
        return False
    # A capital, an opening quote or bracket starts something. A lowercase
    # letter, a digit or a dash does not: "3 p.m. and later", "vol. 2".
    return ch.isupper() or ch in _OPENER


def _is_abbrev(before):
    """Is the token just before a period a title or an initial, not a word?"""
    m = _TAIL_WORD.search(before.rstrip())
    if not m:
        return False
    tok = m.group(1).rstrip(".")
    if tok.lower() in _ABBREV:
        return True
    # A lone capital is an initial: "J. R. R. Tolkien", "E. Nesbit".
    return len(tok) == 1 and tok.isupper()


def _para_sentences(para):
    """The sentences of one paragraph, in order, whitespace collapsed."""
    out = []
    start = 0
    for m in _BOUND.finditer(para):
        end = m.end()
        rest = para[end:].lstrip()
        if not rest:
            break                      # the tail is flushed below
        if not _starts_sentence(rest):
            continue
        if _is_abbrev(para[:m.start() + 1]):
            continue
        piece = para[start:end].strip()
        if piece:
            out.append(piece)
        start = end + (len(para[end:]) - len(rest))
    tail = para[start:].strip()
    if tail:
        out.append(tail)
    return out


def sentences(text, weigh=None):
    """The chapter as reading blocks: one per sentence, in order.

    Each block is {"p": paragraph, "t": the words, "c": spoken characters}.
    `weigh` is how long a sentence takes to say; default is its length. The
    client walks the cumulative `c` against the audio clock, so a block nobody
    speaks is skipped rather than held.
    """
    out = []
    pi = 0
    for raw in PARA.split(text or ""):
        p = SPACE.sub(" ", raw).strip()
        if not p:
            continue
        for s in _para_sentences(p):
            try:
                w = weigh(s) if weigh else len(s)
                w = int(w)
            except (TypeError, ValueError):
                w = len(s)
            if w < 0:
                w = 0
            out.append({"p": pi, "t": s, "c": w})
        pi += 1
    return out


def locate(blocks, frac):
    """The block `frac` of the way through the chapter, by spoken length.

    `frac` is audio time over audio duration. Returns an index into `blocks`,
    or -1 when there is nothing to point at.
    """
    total = sum(b.get("c") or 0 for b in blocks)
    if not blocks or total <= 0:
        return -1
    try:
        frac = float(frac)
    except (TypeError, ValueError):
        return -1
    if frac != frac:                   # NaN
        return -1
    at = max(0.0, min(0.999999999, frac)) * total
    run = 0
    for i, b in enumerate(blocks):
        run += b.get("c") or 0
        if run > at:
            return i
    return len(blocks) - 1
