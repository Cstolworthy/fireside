"""Chunking, shared by the providers that cap request size."""

CHUNK_TARGET = 3500  # characters per TTS request


def chunk_text(text, target=CHUNK_TARGET):
    """Split on sentence/paragraph edges so each request stays under the cap."""
    import re
    parts, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 > target and cur:
            parts.append(cur.strip())
            cur = ""
        if len(para) > target:
            sents = re.split(r"(?<=[.!?])\s+", para)
            for s in sents:
                if len(cur) + len(s) + 1 > target and cur:
                    parts.append(cur.strip())
                    cur = ""
                cur += s + " "
        else:
            cur += para + "\n\n"
    if cur.strip():
        parts.append(cur.strip())
    return parts or [text]
