# Pricing policy (fireside)

This is the business rule the accounting epics (#28, #39, #43, #44) are built
against. It settles one question the spec left implicit: where the margin lives.

## The rule

Two lines, each covers its own cost. Nothing sells below cost.

1. **Character capacity** (the designated sale connection) sells at
   **provider rate + our fee**. The fee covers the per-transaction payment
   fee (Stripe, ~3-6%) plus a small spread for infrastructure and support.
2. **The platform tier** (recurring) pays for the server, storage, and updates,
   and stays cheap. It is *not* a per-character charge and buys nothing but the
   platform — that is stated explicitly at purchase.

**BYO-key self-host stays the zero-margin floor.** A user who runs the open
source server with their own provider key pays nothing to us and is not
subsidised by anyone.

## Why not sell characters at raw provider cost

Selling at cost loses money on every sale at any volume:

- the payment fee is per-transaction, so it is a fixed toll on each purchase;
- provider cost scales with use, so a cost-exact price can never be ahead;
- if the platform tier were made to carry the whole margin instead, it would be
  priced for the heaviest user — exactly the one who costs the most.

The earlier draft of #43/#44 had the sale rate set equal to the provider rate
("no markup or fee adjustment"). That number was set to zero by omission, not by
policy. Chris's call on 2026-10-16: put the policy here, keep the arithmetic.

## How this fits the spec without changing it

The spec is right that the host stays dumb and the plugin is the sole authority
on price (#33, #43, #44). So the fee is **not** applied by the host. It lives
inside the rate the designated sale connection's plugin reports:

- the **sale connection** is a Fireside-authored plugin wrapping the chosen
  provider; it reports a rate that already includes our fee;
- every other connection is pass-through and reports the raw provider rate;
- `characters = floor(payment / reported_rate * 1_000_000)` from #43 is then
  correct exactly as written — no host-side markup, no host-side fee math.

The user sees the rate and the number of characters it buys. The provider's
public price is a lower bound they can compare against; the difference is the
fee, which we do not hide behind a fake "credit" abstraction.

## What stays out of scope

- Currency conversion. Rate and sale currency must match (#44).
- Retrofitting fee onto already-purchased characters. Purchased quantities are
  fixed; later price changes affect future purchases only (#45).
- Local/open-source engines. If a self-hosted TTS engine is wired in as a
  provider, its rate is 0 and it can be the sale connection at 0 + fee; that is
  the real supply lever against Azure's commitment-tier wall.

## Mechanics: the character ledger (2026-10-20)

The unit of the credit ledger is the character, and the plugin is the authority
on it, so the host stays dumb:

- The sale connection's plugin reports `sale_rate_per_1m` = (provider rate +
  per-1M-chars infra fee) * markup. Both the infra fee and the markup are fields
  on the plugin's own config screen; the host never computes a price.
- A payment of `payment` USD buys `characters = floor(payment / rate * 1_000_000)`
  and the balance becomes characters. The rounding is floored, so the fraction
  the money cannot fund lands on us, not the buyer.
- A metered run holds its estimated characters before it starts (the host holds
  `Quote.price` in `Quote.currency`), the plugin reports the real count after,
  and a run the balance cannot cover is refused up front. An `unknown` report
  stays held and visibly pending, never guessed.

Payments sit behind `host/payments.py`: no provider ships, so the purchase route
refuses ("purchases are held until a payment provider lands") until an operator
registers one. `GET /credits` is read-only (balance, rate, bundles);
`POST /credits/purchase` is the only route that turns a payment into characters.

Still to build: the platform+storage monthly tier (a recurring subscription
separate from character capacity), and the Stripe provider once it is registered.

## Storage: a held book bills its own bytes

Chris's rule (2026-10-20), and the launch model: **drop a book, and it consumes
storage.** Every distinct blob a user's shelf names - chapter text, the audio,
its renditions, its cover - counts against that user's plan, once per blob.

- **No sharing discount.** Two people holding the same book each bill its bytes.
  Content addressing still keeps one physical copy on disk; billing is per shelf,
  never per copy. This was a deliberate simplification: the refcount/auto-release
  model was more code and more ways to be wrong, and it charged nobody for the
  common case.
- **The allowance is the platform tier's** (2 GB at launch). Bytes above it bill.
  An account with no plan is on the platform tier (payments are held for launch),
  so it is never locked out of its own library.
- **A write that would cross the allowance is refused up front** (import, cover,
  render). Existing bytes are never silently removed to make room.
- **Promote-to-shared is parked.** Handing a book to the shared catalog so it
  stops billing you is a real later feature; nothing here releases storage
  automatically.

Cost lever: because storage is a pluggable backend, Azure Blob lifecycle rules
move rarely-read books down through hot -> cool -> cold -> archive. Blob storage
at the hot tier is ~$0.018/GB/mo (LRS, US), cool ~$0.01, archive ~$0.002, so the
bytes are rounding error next to the server; the tier's real cost is the box.
