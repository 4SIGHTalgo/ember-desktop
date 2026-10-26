# Ember 0.2.8 — execution boundary fixes

This correction pairs the public client with a backend plan-serialization fix.
Split/closeout plans now use the actual selected provider outcome's identity;
the client accepts exact composite Moneyline market IDs, preserves legitimate
observation-only renewals and isolates malformed opportunities without closing
the feed. Provider load notices can recover automatically after positive
verification. Navigation has one outstanding intent, expired observers stop,
and browser reads avoid redundant work.

Known unsubmitted work continues under the same reservation. A lost arming
reply recovers only the exact bot-owned stake; submit dispatch is durable before
the command, and unresolved submission states fence worker restart. Expired
authorization or lost native permission is checked again immediately before click.

Local release gates: 314 public root tests and 49 explicit dashboard/startup
tests pass. The connected integration gate runs actual scanner output through
real Django/TLS/WSS, the public client process, Chrome provider-shaped fixtures,
server-computed claims, synthetic acceptance and durable ledgers. It includes
the frozen public worker, all four approved families, both confidence bounds and
Kelly fractions, empty-slip preparation, corrupted messages, reconnect/token
rotation, lost claim/driver replies, suspended markets, large changing pages,
notice recovery, worse quotes and uncertain-submit restart fences. All fifteen
tests pass. No live wagers are used for this verification.

No wager policy, freshness limit, price floor or deduplication protection was
relaxed. Stop before updating; review settings and press Start when ready.
