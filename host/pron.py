"""Per-user pronunciation fixes: the words a voice gets wrong.

A rule is {"from": word, "to": respelling}. `from` is the word exactly as it
appears in the text; `to` is how it should sound, spelled out for the engine
(Hermione -> Her-my-oh-nee, Qui-Gon -> Kwai-Gon). The host applies the list to
the chapter text before any speech engine sees it, so every engine gets the fix,
including the local one that cannot take phonemes.

A rule is global (every book this user has), or scoped to an author, or to one
book, because a fic's money glyphs are usually the author's own convention and
sometimes just one story's. Global is the default. Rules live as JSON in
user_prefs.pron: `book` holds a book id, `author` an author name, both empty for
a global rule. Most specific wins.

Numbers are handled here too, since it is the same problem: text a voice would
misread. A comma-grouped number ("50,000") makes most engines say the commas
("five zero comma zero zero zero"), so a spoken-form pass turns those into
words. Only grouped numbers are touched: a bare year or chapter number is left
exactly as the author wrote it.
"""

import hashlib
import json
import re

from host.db import now


_ONES = ("", "one", "two", "three", "four", "five", "six", "seven", "eight",
         "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
         "sixteen", "seventeen", "eighteen", "nineteen")
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety")
_SCALES = ((10 ** 9, "billion"), (10 ** 6, "million"), (1000, "thousand"))


def _below_thousand(n):
    out = []
    if n >= 100:
        out.append(_ONES[n // 100] + " hundred")
        n %= 100
    if n >= 20:
        t = _TENS[n // 10]
        r = n % 10
        out.append(t + ("-" + _ONES[r] if r else ""))
    elif n:
        out.append(_ONES[n])
    return " ".join(out)


def number_words(n):
    """An integer in English, up to billions. 50000 -> fifty thousand."""
    if n == 0:
        return "zero"
    parts = []
    for size, name in _SCALES:
        if n >= size:
            parts.append(_below_thousand(n // size) + " " + name)
            n %= size
    if n:
        parts.append(_below_thousand(n))
    return " ".join(p for p in parts if p)


GROUPED = re.compile(r"(?<![\d.,])\d{1,3}(?:,\d{3})+(?![\d,])")


def expand_numbers(text):
    """Say a comma-grouped number as words: 50,000 -> fifty thousand.

    Only numbers that came with group separators are converted. A year, a
    chapter number, a page reference: the author's own digits are left alone,
    because "one thousand nine hundred ninety nine" is a worse reading of a
    date than the engine's own.
    """
    if not text:
        return text
    return GROUPED.sub(lambda m: number_words(int(m.group(0).replace(",", ""))), text)


def load(conn, uid):
    """This user's list, tidied: [{'from': word, 'to': respelling}].

    Anything malformed is dropped rather than raised: one bad row must not take
    down a render.
    """
    row = conn.execute("SELECT pron FROM user_prefs WHERE user_id=?",
                       (uid or "",)).fetchone()
    raw = (row["pron"] if row else "") or ""
    try:
        rules = json.loads(raw) if raw else []
    except Exception:
        return []
    if not isinstance(rules, list):
        return []
    out = []
    for r in rules:
        frm = str((r or {}).get("from", "")).strip()
        to = str((r or {}).get("to", "")).strip()
        if frm and to:
            out.append(_rule(frm, to, (r or {}).get("book"), (r or {}).get("author")))
    return out


def _rule(frm, to, book="", author=""):
    """A rule's stored shape. One scope at a time: book, else author, else all."""
    book = str(book or "").strip()
    author = str(author or "").strip()
    if book:
        return {"from": frm, "to": to, "book": book, "author": ""}
    return {"from": frm, "to": to, "book": "", "author": author}


def save(conn, uid, rules):
    """Tidy and store the list: drop blanks, drop duplicate words, keep order.

    Updates the existing prefs row in place so the other columns (default
    voice/connection) survive; only inserts when the user has no row yet.
    """
    clean, seen = [], set()
    for r in rules or []:
        frm = str((r or {}).get("from", "")).strip()
        to = str((r or {}).get("to", "")).strip()
        one = _rule(frm, to, (r or {}).get("book"), (r or {}).get("author"))
        key = (one["book"], one["author"].lower(), frm.lower())
        if frm and to and key not in seen:
            seen.add(key)
            clean.append(one)
    blob = json.dumps(clean, ensure_ascii=False)
    row = conn.execute("SELECT user_id FROM user_prefs WHERE user_id=?",
                       (uid,)).fetchone()
    if row:
        conn.execute("UPDATE user_prefs SET pron=?,updated_at=? WHERE user_id=?",
                     (blob, now(), uid))
    else:
        conn.execute(
            "INSERT INTO user_prefs(user_id,pron,updated_at) VALUES(?,?,?)",
            (uid, blob, now()))
    conn.commit()
    return clean


def for_book(rules, book_id="", author=""):
    """The rules that apply to one book: global, then this author's, then its own.

    A more specific rule wins for the same word, because that is the narrower
    statement and the user typed it on purpose.
    """
    bid = book_id or ""
    who = (author or "").strip().lower()
    out = [r for r in (rules or []) if not r.get("book") and not r.get("author")]
    if who:
        mine = [r for r in (rules or []) if r.get("author")
                and (r.get("author") or "").strip().lower() == who]
        if mine:
            words = {r["from"].lower() for r in mine}
            out = [r for r in out if r["from"].lower() not in words] + mine
    if bid:
        local = [r for r in (rules or []) if r.get("book") == bid]
        if local:
            words = {r["from"].lower() for r in local}
            out = [r for r in out if r["from"].lower() not in words] + local
    return out


def book_author(conn, book_id):
    """The author string stored on a book, for author-scoped rules."""
    row = conn.execute("SELECT author FROM books WHERE id=?", (book_id or "",)).fetchone()
    return (row["author"] if row else "") or ""


def _regex(rules):
    """One alternation, longest word first, whole-word where the word has edges."""
    alts = []
    for r in sorted(rules or [], key=lambda r: -len(r["from"])):
        frm = r["from"]
        e = re.escape(frm)
        if re.match(r"\w", frm[0]):
            # a letter may not precede it, but a digit may: "50\u029b" is the
            # amount symbol written after the number, which is the other half of
            # how these fics write money.
            e = r"(?<![^\W\d])" + e
        if re.match(r"\w", frm[-1]):
            # Whole-word, except that a digit may follow: a currency glyph glued
            # to an amount ("\u029b50,000") is still that glyph, and that is the
            # shape this rule exists for. A letter may not follow, so "cat" stays
            # out of "cats".
            e = e + r"(?![^\W\d])"
        alts.append(e)
    return re.compile("|".join(alts), re.I) if alts else None


def apply(text, rules):
    """Swap whole words for their say-as respelling, case-insensitive. The
    replacement goes in literally, never read back as a regex."""
    pat = _regex(rules)
    if not text or pat is None:
        return text
    repl = {r["from"].lower(): r["to"] for r in rules}

    def _swap(m):
        word = m.group(0)
        to = repl.get(word.lower(), word)
        if to is word or to == word:
            return word
        nxt = text[m.end():m.end() + 1]
        prv = text[m.start() - 1:m.start()]
        # A replacement glued to a number needs its own space on either side, or
        # the amount is said as part of the word: "galleonfifty thousand",
        # "fiftygalleon". Digits on both sides ("50ʛ") get both spaces.
        out = to
        if prv.isdigit() and to[:1].isalpha():
            out = " " + out
        if nxt.isdigit() and to[-1:].isalpha():
            out = out + " "
        return out

    return pat.sub(_swap, text)


def spoken(text, rules):
    """The text a voice is actually given: rules first, then numbers.

    One entry point so a render, a cache tag and an estimate can never disagree
    about what is being said.
    """
    return expand_numbers(apply(text, rules))


def token_for(text, rules):
    """A short tag for just the rules that match `text`, for the audio cache key.

    Only the matching rules go in, so adding a rule for a word that lives in one
    chapter does not invalidate chapters where that word never appears. Empty
    means no rule applies, and the chapter keeps whatever audio it already had.
    """
    pat = _regex(rules)
    if not pat or not text:
        return ""
    found = {m.group(0).lower() for m in pat.finditer(text)}
    use = [r for r in rules if r["from"].lower() in found]
    if not use:
        return ""
    raw = "|".join("%s>%s" % (r["from"].lower(), r["to"])
                   for r in sorted(use, key=lambda r: r["from"].lower()))
    return "p" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]


def impact(conn, storage, user_id, book_id=None, split_for=None):
    """Chapters already voiced that the current rules make out of date.

    For every book the user owns, look at each rendition - one voiced copy of a
    chapter, one voice. A rendition is stale when the pronunciation tag it was
    made with no longer matches the tag the rules produce for that chapter's
    text. That is the exact test the renderer uses to decide what to redo.

    `split_for(provider)` returns a provider's chunk function, so the report can
    also say how much audio is genuinely new: only the chunks with no cached
    audio for the current rules get synthesised, the rest are reused. Without a
    split function the whole chapter counts, a safe upper bound.

    Grouped by book and by (provider, voice, model, speed) so the caller can
    re-encode each voice with a real render call, scoped to just these chapters.
    """
    from host import library, jobs

    all_rules = load(conn, user_id)
    q = "SELECT id,title,author FROM books WHERE user_id=?"
    args = [user_id]
    if book_id:
        q += " AND id=?"
        args.append(book_id)
    books = []
    chapters_affected = 0
    chars_to_synth = 0
    est_seconds = 0.0
    for b in conn.execute(q, tuple(args)).fetchall():
        # a rule scoped to one book only invalidates that book's audio
        who = b["author"] if "author" in b.keys() else book_author(conn, b["id"])
        rules = for_book(all_rules, b["id"], who)
        chapters = library.chapters(conn, b["id"])
        by_idx = {c["idx"]: c for c in chapters}
        rends = {}
        for r in library.renditions(conn, b["id"]):
            rends.setdefault(r["idx"], []).append(r)
        stale = set()
        groups = {}
        for c in chapters:
            has_default = bool(c.get("audio_ref"))
            rs = rends.get(c["idx"], [])
            if not rs and not has_default:
                continue  # never voiced: nothing to redo
            try:
                text = storage.get(c["text_ref"]).decode("utf-8")
            except Exception:
                text = ""
            tag = token_for(text, rules)
            if not rs and has_default:
                # legacy row with audio but no rendition entry; treat the
                # chapter's own columns as the one voiced copy.
                rs = [{"provider": c.get("provider") or "",
                       "voice": c.get("voice") or "", "voice_label": "",
                       "model": c.get("model") or "",
                       "speed": c.get("speed") or 1.0,
                       "pron": c.get("pron") or ""}]
            for r in rs:
                if (r.get("pron") or "") == tag:
                    continue
                prov = r.get("provider") or ""
                voice = r.get("voice") or ""
                model = r.get("model") or ""
                speed = float(r.get("speed") or 1.0)
                label = r.get("voice_label") or ""
                split = split_for(prov) if split_for else None
                if callable(split):
                    sc = jobs.stale_chars(conn, storage, text, rules, split,
                                          prov, voice, model, speed)
                else:
                    sc = len(text)
                g = groups.setdefault((prov, voice, model, speed, label),
                                      {"chapters": [], "chars_to_synth": 0})
                g["chapters"].append(c["idx"])
                g["chars_to_synth"] += sc
                stale.add(c["idx"])
        if not stale:
            continue
        rends_out = []
        book_chars = 0
        book_seconds = 0.0
        for (prov, voice, model, speed, label), g in groups.items():
            secs = g["chars_to_synth"] / jobs.synth_rate(prov)
            book_chars += g["chars_to_synth"]
            book_seconds += secs
            rends_out.append({"provider": prov, "voice": voice, "model": model,
                              "speed": speed, "voice_label": label,
                              "chapters": sorted(g["chapters"]),
                              "chars_to_synth": g["chars_to_synth"],
                              "est_seconds": round(secs, 1)})
        books.append({
            "id": b["id"], "title": b["title"],
            "chapters": [{"idx": i,
                          "title": (by_idx.get(i) or {}).get("title") or ""}
                         for i in sorted(stale)],
            "renditions": rends_out,
            "chars_to_synth": book_chars,
            "est_seconds": round(book_seconds, 1),
        })
        chapters_affected += len(stale)
        chars_to_synth += book_chars
        est_seconds += book_seconds
    return {"books": books, "books_affected": len(books),
            "chapters_affected": chapters_affected,
            "chars_to_synth": chars_to_synth,
            "est_seconds": round(est_seconds, 1)}
