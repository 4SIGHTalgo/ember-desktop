# Ember 0.2.6 observed browser startup

Corrects the startup shortcut that treated a nonempty server board as evidence
that the browser was already on Table Tennis. A fresh, authenticated homepage
opens the actual TT Elite league and rechecks provider permission afterward.
Only an observed populated slip can retain the browser without that initial
reload. Provider load-error and funding guards remain active before navigation.

Native suspended cards can omit both the stake input and price. The reader
accepts this shape only with exact identity, explicit suspended state and an
unfunded native Single record mapped to that one selection. It reports no price
and never authorizes the suspended leg; a ready leg need not be starved by it.
Missing, ambiguous or funded evidence still blocks execution. Funding metadata
is kept out of the immutable claimed-quote fingerprint.

Includes the 0.2.5 official location recheck and earlier provider-error fixes.
Local real-Chrome fixtures cover the actual homepage-to-league navigation with
a live server-board snapshot, zero claims/submits, preservation of observed
staging, suspended-card omission of controls, and fail-closed unknown funding.

Local preflight passed all 289 root tests. An initial run stalled in a fake
driver-loss fixture that raised on every script, including the newly added
startup reads. That fixture now supplies observed empty/zero slip evidence and
loses the reply only at its intended final-submit boundary; the no-replay test
and full rerun passed without relaxing the production guards.
