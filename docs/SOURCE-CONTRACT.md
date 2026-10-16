# Source contract (v1)

The app already has two sources hardcoded: an EPUB upload and a generic web-page
fetcher, handled inline in `_ingest` / `_ingest_url`. That is the ingest-side
twin of the speech-provider seam: nobody named it, and unnamed seams drift.

The idea (Chris, 2026-10-16): a plugin per source. An EPUB reader, a generic
http reader, and later an AO3 reader that detects an AO3 URL and knows how to
scrape the needed text. Each source can surface its own screen under settings
(e.g. paste an AO3 session cookie so locked fics come through).

The loader is later. The contract is the part that stops the two existing
sources from growing apart, and that a third source can be written against.

## The interface

One source = one module exposing:

1. `id` - stable slug (`epub`, `web`, `ao3`, ...). It is part of the book
   identity and the meter tag. Never rename it once books exist under it.
2. `matches(req) -> bool`. `req = {kind: "upload"|"url", filename, data, url}`.
3. `read(req, fetch, cred) -> book`,
   `book = {title, chapters: [{title, text}], meta: {...}}`. The shape is exactly
   what `parse_epub` / `parse_web` already return, so nothing downstream changes.
4. `settings() -> [Field]`, `Field = {key, label, type: "text"|"secret"|"select",
   help, placeholder}`. Declarative. The host renders the widget. **A source
   ships no HTML**, same reason a provider never holds the login.
5. `credential_hosts() -> [host suffix]`. Host suffixes the credential may be
   sent to; `[]` if the source takes no credential.

## Who performs the request (Q1: my call)

Not plugin, not a naked host: split it.

- The **host owns egress and the credential.** The plugin never opens a socket,
  never holds the login, and never learns which door the credential came through
  (paste-cookie now, a real sign-in later). Every fetch is logged and metered in
  one place.
- The **plugin owns matching and parsing.** It calls the `fetch(url)` the host
  handed it, then maps the response to a book.

A plugin with its own sockets would be a second place credentials can leak and a
second place requests are not audited. Handing it a `fetch` keeps the AO3-specific
scraping logic where it belongs while leaving one auditable chokepoint.

## Credential scoping

The credential only leaves to the hosts a source declared in
`credential_hosts()`, attached as a `Cookie` header by the host. For AO3 that is
`archiveofourown.org` (matched on host or a dot boundary, so `www.` counts and
`notarchiveofourown.org` does not). The credential is never logged.

## Credentials: paste-cookie now (Q3)

One slot per (source, user). `paste-cookie` now; a real sign-in flow later - two
doors, one slot, exactly like a provider key. The cookie is a `secret` Field.

- Local app: kept in the browser (localStorage) and sent per request.
- Hosted: stored server-side per user, encrypted, never returned to the client.

## Precedence

Sources are tried in registration order; first `matches()` wins. The generic
`web` source is the fallback and **must be registered last**. `epub` matches any
upload. Ordering guarantees only one source consumes a request, so book ids
cannot collide across sources.

## Book identity

The host computes `book_id = sha1(canonical request)`:

- upload -> `sha1(bytes)[:12]`
- url    -> `sha1(url)[:12]`

Same url re-ingested is idempotent (same book).

## Drift, named and closed (v1)

The two sources now live in `sources/` on this contract: `epub.py`, `web.py`,
plus the registry in `sources/__init__.py`.

- `server.py`'s `_ingest` and `_ingest_url` call `sources.ingest(req, cred)`
  instead of calling `parse_epub` / `parse_web` directly; the parsers moved out
  of `server.py` into the source modules.
- The host's HTTP egress moved to `sources/_fetch.py`, which enforces the
  credential scope and is the one place a source's requests pass through.
- `book_id` now comes from `sources.request_key`, one definition for both kinds.
- `tests/test_sources.py` checks the registry, the parsers on plain strings, the
  credential-scope rule, and the real HTTP ingest flow for an EPUB upload, with
  no external network.

Still later: the loader (discovery of third-party source plugins, isolation,
install). That is expansion, not revenue: do not let it eat the hosted-billing
path.

## Not in scope here

- AO3 itself: that is a source someone builds against this contract. It is
  heavier than the others because it acts with the user's session cookie, which
  is exactly why the credential lives host-side and is scoped.
