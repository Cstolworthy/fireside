"""Generate a large, deterministic EPUB for import, storage and render tests.

Real books are big (Cat's shelf averages 20k chars a chapter, up to 34k) and no
test in this repo carried one, so every quota and import path was proved on
two chapters of "hello world". This writes a book of any size from a seeded
generator: same seed and size in, same bytes out, so a test can assert on the
chapter count and the character total instead of on a checked-in blob.

    python tools/make_big_epub.py --chars 1800000 --chapters 60 -o /tmp/big.epub
    python tools/make_big_epub.py --chars 4000 --chapters 3 -o tests/fixtures/tiny.epub

The prose is nonsense, deliberately: no copyrighted text, nothing to archive.
"""

import argparse
import hashlib
import io
import math
import random
import zipfile

WORDS = ("circuit coil winding phase current shunt sense lead bench measure "
         "field flux gap core turn wire gauge thermal drift offset noise "
         "calibrate reading sample average repeat hold ramp settle").split()

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf"
    media-type="application/oebps-package+xml"/></rootfiles>
</container>"""


def _paragraph(rng, words):
    out = []
    for _ in range(words):
        w = rng.choice(WORDS)
        out.append(w.capitalize() if not out or rng.random() < 0.08 else w)
    return " ".join(out) + "."


def chapter_text(rng, chars):
    """Padding, then whole paragraphs until `chars` characters of body text."""
    parts, n = [], 0
    while n < chars:
        p = _paragraph(rng, rng.randint(40, 120))
        parts.append("<p>%s</p>" % p)
        n += len(p) + 1
    return "\n".join(parts)[:max(0, chars) + 400]


def build(chars=1_800_000, chapters=60, seed=7, title="Big Generated Book"):
    rng = random.Random(seed)
    per = max(1, math.ceil(chars / max(1, chapters)))
    buf = io.BytesIO()
    z = zipfile.ZipFile(buf, "w")
    # The EPUB spec wants mimetype first and stored, not deflated.
    z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
               compress_type=zipfile.ZIP_STORED)
    z.writestr("META-INF/container.xml", CONTAINER)
    items, spine = [], []
    for i in range(chapters):
        name = "c%03d.xhtml" % i
        body = chapter_text(rng, per)
        z.writestr("OEBPS/" + name,
                   '<?xml version="1.0" encoding="UTF-8"?>\n'
                   '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
                   '<title>Chapter %d</title></head><body>'
                   '<h2>Chapter %d</h2>%s</body></html>'
                   % (i + 1, i + 1, body))
        items.append('<item id="c%d" href="%s" media-type="application/xhtml+xml"/>'
                     % (i, name))
        spine.append('<itemref idref="c%d"/>' % i)
    z.writestr("OEBPS/content.opf",
               '<?xml version="1.0" encoding="UTF-8"?>\n'
               '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
               'unique-identifier="bookid"><metadata '
               'xmlns:dc="http://purl.org/dc/elements/1.1/">'
               '<dc:title>%s</dc:title><dc:language>en</dc:language>'
               '<dc:identifier id="bookid">urn:uuid:%s</dc:identifier>'
               '</metadata><manifest>%s</manifest><spine>%s</spine></package>'
               % (title, _uuid(seed, chars, chapters), "".join(items),
                  "".join(spine)))
    z.close()
    return buf.getvalue()


def _uuid(*parts):
    h = hashlib.sha1(("|".join(map(str, parts))).encode()).hexdigest()
    return "%s-%s-%s-%s-%s" % (h[:8], h[8:12], h[12:16], h[16:20], h[20:32])


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chars", type=int, default=1_800_000,
                    help="approximate body characters across the whole book")
    ap.add_argument("--chapters", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--title", default="Big Generated Book")
    ap.add_argument("-o", "--out", default="big.epub")
    a = ap.parse_args()
    data = build(a.chars, a.chapters, a.seed, a.title)
    with open(a.out, "wb") as f:
        f.write(data)
    print("%s  %d chapters  ~%d chars  %d bytes"
          % (a.out, a.chapters, a.chars, len(data)))


if __name__ == "__main__":
    main()
