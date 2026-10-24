# Contributing

Ember places real wagers, so every change needs a matching test and must keep
the fail-closed invariants:

- Ambiguity aborts the wager; never guess, never replay a submit command.
- One economic bet per contract identity, enforced by the local ledger.
- No credentials, keys, token caches, or browser profiles in the repository.
- Browser-facing changes ship with both a mock-driver test and a
  real-browser fixture test.

Run the suite before opening a pull request:

```sh
python -m unittest discover -s . -p "test_*.py"
```
