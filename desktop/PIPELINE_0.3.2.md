# Ember 0.3.2 — monitoring continues after a skipped bet

Release status: stable remains 0.3.1 until publication completes.

On 2026-11-02 to 11-04, 0.3.1 ended its own run three times after a bet it did
not submit, because it could not verify the single slip empty afterwards
(#1039, #1064, #1103). Every later signal was then missed. Once the run had
stopped, nothing kept the DraftKings session active, and DraftKings signed the
account out for inactivity.

- The contract stays blocked, as before: it is never released or retried, and
  it is journaled as a miss. The run now continues. Every later signal already
  has to verify the single slip before it can arm, and the idle slip upkeep
  removes unfunded leftovers. If a stake Ember armed could not be cleared, the
  run still stops, because only you can check a funded slip.
- While the slip still says "Placing your bets", the receipt wait runs up to 30
  seconds after the click instead of 12. Ember never clicks again during that
  wait. Closeout #1017 was placed but still showed that state at 12.9 seconds.
  Any other state keeps the 12-second bound, and a placing state that lasts past
  30 seconds stays unconfirmed.

No formula, price floor, freshness limit, reservation or replay guard changed.
An unconfirmed submission still blocks Start until it is cleared.

Gates: the new `test_wager_submission` checks that the placing state is
awaited and bounded and that the normal bound still applies. `test_ember_client`
checks three things: a leftover pick blocks only its contract, the next contract
arms only on a verified slip, and a stake left in the slip still stops the run.
These tests fail against 0.3.1. Shared modules are byte-identical to the private
source (`fb3e221`), apart from the 8 known public variants. This release was
gated on tests and 3-OS CI only, with no device rehearsal.
