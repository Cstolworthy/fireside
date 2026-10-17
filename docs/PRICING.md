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
