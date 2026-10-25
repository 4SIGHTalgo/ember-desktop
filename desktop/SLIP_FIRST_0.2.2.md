# Ember 0.2.2

- Stages available approved legs at zero stake across verified live matches.
- Executes the exact event/market/selection/line already in the shared slip;
  other single and parlay stakes must be zero.
- Unready matches yield bounded FIFO turns to other contracts. A missing or stale
  leg cannot claim or submit. An uncertain submission fences further wagers.
- Shows a compact multi-match board, all observed slip legs and owned positions.
  Conservative EV comes from the selected server confidence view; historical
  Entry EV is distinguished from a current midgame estimate.
- Renews the same qualifying entry while fresh evidence supports it. First point,
  quote replacement/disqualification, stale evidence or disconnect invalidates it.
- Conditional complements remain disabled; split/closeout/approved spreads remain.

Actual-client scheduler tests use real Chrome, loopback HTTP, the production
queue/readiness/executor, and durable SQLite reservation. They cover both arrival
orders, simultaneous opportunities, B arriving while A is handled, slow/missing/
suspended A, stale inputs, exact identities and uncertain replies. No live wager
is needed. Per-match monotonic timings and outcomes are generated in OS temporary
`ember-scheduling-*/output` directories.

One browser still serializes WebDriver commands and receipt reconciliation.
Non-preemptible driver calls or uncertain submissions can prevent another fill;
the client never replays a wager to hide this limit. Unsupported provider DOM
fails closed. Fixture success is distinct from installed/live-provider verification.

Release preflight on Windows (2026-10-25): root discovery passed all 271 tests;
the explicit startup-helper, frozen/source/worker startup smoke, and dashboard
battery passed all 46 tests. The earlier transient browser failures remain part
of the private activation evidence; these successful runs do not erase them.
