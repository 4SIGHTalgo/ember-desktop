# Ember 0.3.0 — one-at-a-time reference-window execution

Release status: local gates passed; stable remains 0.2.9 pending publication.

The client adds a selection only after the selected conservative confidence level
qualifies and funds one exact single at a time. It continues scanning multiple
matches and routing between them.

Server plans bind frozen pre-set reference probabilities to an exact selection,
contract, target set and confidence. Equal or improving conservative EV remains
eligible after points start. Any observed decrease permanently expires that
window, even if EV stays positive or rebounds. Improved odds require a newly
authorized stake. This is pre-set-reference conservative EV, not a score-conditioned
in-play probability. The public worker contains no strategy/probability model.

The complete connected suite passed 20 tests in 574.586 seconds, covering
empty-until-qualified execution,
four exact single acceptances, first-point continuity, improved-price resizing,
positive-decline/rebound cancellation with no claims, both confidence levels and
Kelly fractions, and uncertain-submission restart fencing. Source and frozen
workers recovered a lost arming reply and waited for fresh positive authorization.
Claim-request/provider-acceptance timestamps verify no new claim follows an
uncertain submit or restart. Real-browser fixtures
and mock-driver ordering tests cover single-slip cleanup and native price changes.

Public discovery passed 342 tests. Desktop/dashboard, packaged-worker startup and
native-capability checks passed 53 tests. The private pinned engine/dashboard
checks passed 294 tests, and website browser checks passed 13 tests. Platform,
publication and installation evidence will be recorded after those gates complete.
No live-provider wager was used for these checks.
