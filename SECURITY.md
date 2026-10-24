# Security policy

Ember moves real money through your browser, so reports are handled directly
and quickly.

- Report privately to **security@getembersignals.com** (or use GitHub's
  private vulnerability reporting on this repository).
- Include reproduction steps and affected versions.
- Do not open public issues for anything involving credentials, keys, or
  exploitability of the wager path.

This repository is client-side only. It must never contain API keys, tokens,
profiles, or credentials of any kind — CI rejects a build if a private file
ends up in the worker bundle.

Design guarantees worth testing: fail-closed execution (an ambiguous page
state aborts the wager, never guesses), one economic bet per contract
identity, and refusal to replay a submit command after any connection loss.
