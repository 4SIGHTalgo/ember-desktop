# Ember 0.2.4 provider recovery

Read-only inspection of a populated native slip found that removal tracking
contains only `selectionId`. 0.2.3 required event/market attributes that the
provider does not render there, rejecting the entire populated slip before claim.

The reader now requires one consistent full event/market/selection identity from
the rendered single card's scoped selection metadata. Tracking and metadata must
agree. Missing, conflicting, external or malformed evidence still fails closed;
player labels and encoded-ID guesses never supply missing identities.

Provider load-error notices pause preparation for five seconds. Three errors
cancel the reveal observer and require a new run after provider recovery. No
selection, expansion, or final submission proceeds through a visible load error.
An uncertain submission is still fenced and never replayed. This limits client
retries; it does not claim the provider's underlying service cannot fail.

The diagnostics view distinguishes stale verified scores from stale quotes and
keeps visible odds separate from model EV. It does not fabricate freshness or
change EV/entry gates when a match is in progress.

Regression coverage uses local HTTP/real-Chrome fixtures, including selection-only
native tracking, exact nonfirst-leg execution, conflicting identities, provider
errors after claim, observer cancellation, bounded retry and unchanged no-replay
semantics. Live diagnostic inspection and installed acceptance use zero allowed
execution opportunities; no live wager is part of verification.

Local preflight: 279 root tests and 33 dashboard/browser tests passed. The first
root run exposed three fake-driver fixtures lacking the new explicit load-absence
probe; those fixtures were corrected before the successful full rerun.
