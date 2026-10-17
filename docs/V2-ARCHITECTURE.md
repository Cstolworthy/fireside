# fireside v2 - architecture and the extract

Status: v2 is a rework of fireside from one Flask app into a **host + plugins**
server with a separate client. This document is the map for that extract, and it
names the three plugin contracts v2 is built against.

The v1 contracts (`docs/PROVIDER-CONTRACT.md`, `docs/SOURCE-CONTRACT.md`) are the
parents of two of these. v2 keeps their rule - *the system is shaped to host
integrations; integrations flex around the external system* - and adds the third
(storage) plus the accounting boundary.

## What we keep, what is new

**Extract, not a blank-page rewrite.** No history rewrites; new commits on top.
The risky, already-proven pieces stay exactly as they are:

- `sources/_backends.py` - the browser-fingerprint backend (the AO3 Cloudflare
  HTTP 525 fix: `safari170` -> 200, `chrome124` -> 403, plain urllib -> 525).
- `sources/epub.py`, `sources/_text.py` - EPUB unzip + XHTML strip.
- `providers/azure.py`, `providers/minimax.py`, `providers/openai.py`,
  `providers/local.py` - the speech clients and the Kokoro OpenAI-compatible path.
- `sources/_fetch.py` - host-owned egress + credential scoping.
- `tests/test_sources.py`, `tests/test_providers.py` - they already prove the
  parsers and providers with no network.

**New in v2:**

- a **host** that owns auth, library, jobs, storage, the credit ledger, and the
  plugin registry, and knows only the generic contracts;
- a **client** (first target: Windows, thin webview over the existing UI) that
  owns offline download, verification, and playback;
- **three plugin contracts** (reader, speech, storage), all filesystem-discovered;
- **plugin-led accounting** for the sale connection (#32-#35, #39-#45).

The old `server.py` stays as the v1 app while the host is built next to it; the
UI moves onto the host once the contracts are served. Nothing in v1 is deleted
until its v2 replacement passes the same tests.

## The three plugin contracts

Common to all three: a plugin is a directory under `plugins/<kind>/<id>/` with a
`plugin.json` manifest and a `plugin.py` exposing a class. **The host never
imports a provider by name, and a plugin ships no HTML.** A plugin never opens a
socket; the host hands it a `fetch`.

### reader (from #21; supersedes SOURCE-CONTRACT v1)

    id                    stable slug; part of the book identity
    matches(req) -> bool  req = {kind, filename, data, url}
    read(req, fetch, cred) -> book   book = {title, chapters:[{title,text}], meta}
    settings() -> [Field]            declarative; host renders the widget
    credential_hosts() -> [suffix]   credential leaves only to these hosts
    transport() -> dict [opt]        fetch policy (plain|browser + retries/backoff)
    consent(req) -> Consent [opt]    adult/consent gate; plugin-owned (#21)

Ordered: first `matches()` wins; the **generic HTTP reader runs last** (#21).
Reader execution returns structured evidence (which plugin, why it matched, what
it fetched) so a failure is legible.

### speech (from #28-#35; supersedes PROVIDER-CONTRACT v1)

    id, ext, mime, needs_key
    list_voices(cred) -> [Voice]
    synth(text, voice, opts, cred) -> Audio   Audio = {bytes, ext, mime}
    quote(ctx) -> Quote                        see accounting below
    report(ctx) -> Report                      see accounting below
    cost_per_1m_chars(cred) -> float | None    None = unpriced, NOT free

### storage (new)

    id
    put(key, bytes) -> ref
    get(ref) -> bytes
    delete(ref)
    info() -> {kind, durable, url_ttl}   the host decides nothing about location

Default storage is local (files on the server's disk). A blob store is a plugin.
The host keeps audio behind refs, never assumes a path, and never assumes the
bytes are still there - offline download re-reads from storage.

## Client-server split (#1, #21)

- One **deployment-agnostic server**. Running at home is identical to hosted;
  hosting differs only in auth mode and in whose provider key is pasted (with
  rate limiting on before our key goes on a hosted box).
- The client connects to a user-set server URL, holds a local account login,
  keeps its own per-user library, downloads selected chapters, **verifies** each
  (hash + size) before marking it ready, and plays in order with resume - all
  without the server.

## Plugin-led accounting (#32, #39)

The plugin is the sole authority. The host never infers a charge from a
successful request or from saved audio, and never turns unknown into zero.

    Quote  = {chars_est, price, currency, balance?, eligible: bool,
              refusal_reason?, authoritative: bool}
    Report = {chars_actual?, cost?, currency?, duration_s?,
              external_balance?, charge: "charged"|"unknown"|"pending",
              reconcilable: bool}

Reserve-then-settle, for the **one designated sale connection only**:

1. quote -> `eligible`? if not, block with the plugin's reason (#38);
2. atomically reserve `chars_est` from the user's Fireside balance; concurrent
   requests cannot overspend (#41);
3. run, then settle from the plugin's `report`. Charged -> debit and release the
   remainder. `unknown`/`pending` -> **stays reserved and visibly pending**; it
   is never guessed or auto-released (#42);
4. reconciliation later updates the ledger with no duplicate debit.

Every other connection is pass-through: no Fireside balance check, no
reservation, no deduction (#40). External balances are a separate metric, never
the Fireside balance (#37).

Sale pricing policy (the fee and the two lines) lives in `docs/PRICING.md`.

## Build order

1. contracts + host skeleton + plugin discovery (this doc's shapes, filesystem
   registry, health endpoint);
2. move the proven reader/speech modules behind the v2 contracts, v1 tests green;
3. storage contract + local default + the job pipeline reading/writing blobs;
4. accounting: quote/report contract, ledger, reserve/settle, sale designation;
5. client (Windows): connect, library, download+verify, offline play+resume;
6. hosted mode: server-side credential store, rate limiting gate, then our key.
