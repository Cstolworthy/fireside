"""The free example book.

One bundled public-domain text every account can add in a click, so a brand
new reader has something to hear before they own a book or a voice. The text is
a plain asset file, not a download: it is the same bytes on every deployment,
which is what lets a single pre-render (see docs) warm the render cache so the
example is free to voice for everyone after that.

The mark `EXAMPLE_MARK` is written into the book's meta, so a second add finds
the copy already on the shelf instead of making another one.
"""

import json
import os

from .contracts import Book, Chapter

EXAMPLE_MARK = "happy-prince"
# The voice the example is pre-rendered in. Kept here so a deployment can name
# a different one by setting `voice` on FIRESIDE_SALE_CONNECTION; this is only
# the fallback when the sale spec carries none.
DEFAULT_VOICE = "en-US-AndrewNeural"
_ASSET = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "assets", "example", "happy_prince.json")


def book():
    """The example as a host Book. Raises if the asset is missing, which is a
    deployment error, not a user error."""
    with open(_ASSET, "r", encoding="utf-8") as f:
        data = json.load(f)
    chapters = [Chapter(title=c.get("title", ""), text=c.get("text", ""))
                for c in data.get("chapters", [])]
    meta = {"example": EXAMPLE_MARK,
            "author": data.get("author", ""),
            "source": data.get("source", "")}
    return Book(title=data.get("title", "Example"), chapters=chapters, meta=meta)
