# Ember 0.3.1 — falling-price reference windows and live DraftKings repairs

Release status: ported from the live-proven private worker; stable remains 0.3.0
until local acceptance and publication complete.

Reference windows now open only while conservative EV is positive at the set
anchor. After that they accept only an unchanged or lower price while EV stays
positive. Any observed price rise, or EV no longer positive, permanently ends the
window: a rising price means the market moved against the selection, which the
frozen pre-set probability cannot see. The window kind is now
`pregame_non_increasing`. Version 0.3.0 rejects it and fails closed, which means
no signal is acted on until this update is installed. The client ledger, slip
reads and market readiness keep the lowest observed price as a ceiling, and the
server claim checks the same ceiling.

DraftKings repairs from live runs:
- Closeout signals reach the Moneyline board, including DraftKings' renamed
  "Winner" and "Match Winner" sections. Non-table-tennis pages are left after a
  grace period, so reading My Bets is never interrupted.
- Ember routes between matches in place in one tab, because a freshly opened
  DraftKings event tab stalled for 20 to 30 seconds in live runs. It closes the
  tabs of finished matches while always keeping one open, and it reattaches to
  the last worked tab.
- A location failure that persists is rechecked after 2, 4, 8 and 16 minutes,
  then every 30 minutes, instead of pausing wagering for hours. A confirmed
  wagering permission resets that schedule.
- Session keep-alive continues while Ember waits, never clicks through an
  overlay, and backs off after a click that never returns. A sign-out during a
  run raises an alert.
- PICK CLOSED at submission is a miss for that contract only, and the run
  continues. The contract is never retried.
- Driver commands outlast DraftKings' long main-thread tasks (25 s), and a
  clicked leg must land in the slip before the plan reads it.
- Execution-error misses record only the exception class and Ember's own code
  location. Per-command timing and stage marks also record no page text.
- A no-wager rehearsal mode arms the server-sized slip and never dispatches the
  wager command. It is used for release acceptance.

Source: the private client at d9e03c5 (worker 9B3F045A) ran live for about
10.3 hours on 2026-11-01 and 11-02. It placed 3 wagers (Points Spread, Games
Spread, Game 3) with no wrong submissions. The first closeout that reached the
Moneyline board failed closed on a locked button. This release ports that engine:
10 changed modules are byte-identical copies, 2 are new (`ember_tabs`,
`ember_navigation`), and 4 public variants (`ember_client`, `ember_browser_lifecycle`,
`ember_market_readiness`, `tt_market_exec`) carry the same changes line for
line. No formula, freshness limit, price floor, economic reservation or replay
guard was weakened, and the public worker still contains no strategy model.

Pre-release gates and acceptance are recorded below as they complete.
