"""Text helpers shared by the source parsers. No network, no state, no imports
from the rest of the app, so a parser can be tested on a plain string."""

import html
import re

TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"[ \t\r\f\v]+")
BLANK = re.compile(r"\n{3,}")
# page chrome that survives tag-stripping and would otherwise be read aloud
_CHROME = re.compile(
    r"(?i)^(skip to (main )?(content|footer)|jump to content|toggle navigation|menu|search|search for:)$")

# chrome that never belongs in the reading text (web pages)
_KILL = re.compile(
    r"(?is)<(script|style|noscript|svg|iframe|template|form|nav|header|footer|aside)\b[^>]*>.*?</\1>")
_BLOCK = re.compile(r"(?i)</?(p|div|h[1-6]|li|tr|blockquote|section|article|main|figure|figcaption)\b[^>]*>")
_ANYTAG = re.compile(r"<[^>]+>")


def _readable_text(s: str) -> str:
    """Normalize whitespace without discarding paragraph separators."""
    s = html.unescape(s)
    s = WS.sub(" ", s)
    lines = [line.strip() for line in s.split("\n")]
    # The empty lines distinguish separate paragraphs from soft line breaks.
    # Dropping them collapses EPUB paragraph boundaries before TTS receives text.
    return BLANK.sub("\n\n", "\n".join(
        line for line in lines if not _CHROME.match(line)
    )).strip()


def text_of(xhtml: str) -> str:
    """EPUB XHTML -> readable text, retaining paragraph and line breaks."""
    s = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", xhtml)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    # Block tags separate paragraphs, unlike <br> which is a single line break.
    s = re.sub(r"(?i)</?(p|div|h[1-6]|li|tr|blockquote|section)[^>]*>", "\n\n", s)
    return _readable_text(TAG.sub("", s))


def clean_fragment(frag: str) -> str:
    """Page HTML -> readable text, retaining paragraph and line breaks."""
    s = _KILL.sub(" ", frag)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = _BLOCK.sub("\n\n", s)
    return _readable_text(_ANYTAG.sub("", s))
