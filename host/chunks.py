"""The re-render unit: one provider request, not one chapter.

A long chapter is spoken as several requests (see providers/_text.CHUNK_TARGET).
The old pipeline cached the concatenated result under one key, so a single word
change re-synthesised the whole chapter. Here the cache key is per request
chunk, so a rule that touches one sentence re-synthesises one chunk and reuses
the rest - same request count for the check, far less audio regenerated.

This module is the one place both the renderer and the impact estimate compute a
chunk's identity, so "what is stale" and "what re-renders" can never drift.
"""

import hashlib


def cid(text):
    """A chunk's content address: two identical chunks share one cached audio."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def cache_key(text_ref, provider, voice, model, speed, extra=""):
    """The render_cache key for one unit of audio.

    `text_ref` is a chapter's storage ref (whole chapter) or a chunk's content
    address (one request). `extra` is the pronunciation tag for the rules that
    match this text, so a rule change moves the key and only the matching units
    miss.
    """
    raw = "|".join([text_ref or "", provider or "", voice or "",
                    model or "", "%.3f" % float(speed if speed is not None else 1.0),
                    extra or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def split(speech, text):
    """The request chunks a provider will make of `text`.

    A provider owns its own request boundaries, so it may expose `split`; when
    it does not, the text is one unit and behaviour matches the old pipeline.
    """
    fn = getattr(speech, "split", None)
    if callable(fn):
        try:
            parts = list(fn(text))
            return parts or [text]
        except Exception:
            return [text]
    return [text]
