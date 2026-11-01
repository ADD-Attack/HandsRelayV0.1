# R13 B00 — Frozen baseline and shared contracts

## Frozen source

- Candidate starts from `v3-r13-spec` at `5da4b0d273e826010dc1f708d10fdb661c183907`.
- The production-code baseline corresponds to R12 `ee3cf0e2dbc4bd2b2cac3ee1651471aa41a6dbd1`.
- `tests/test_relay_fix_acceptance.py` is the immutable 21-check desired-behavior gate supplied with the spec. It is not edited to make implementation pass.

## Reproduced baseline

Using Python 3.12.3, pytest 9.1.1, Pillow 13.0.0, Flask 3.1.3, and requests 2.34.2 in an isolated environment:

- `RELAY_REPO_ROOT=$PWD /home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_relay_fix_acceptance.py` — **18 failed, 3 passed** (expected R12 failure backlog).
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q` — **18 failed, 232 passed, 1 skipped**. The 232 passes include the three passing acceptance controls; the pre-existing suite baseline is therefore **229 passed, 1 skipped**.

## Shared outcome contract

Use the framework's three distinct facts: dispatch certainty, hands-reported injection result, and target-application effect. A valid complete success response can mean only `applied` (injection routine completed), not that the application achieved its desired effect. Invalid or lost responses after possible dispatch are `unknown-outcome` and terminal; do not replay. A proven pre-start refusal may be held only when target and deadline constraints are explicit. Preserve typed, bounded diagnostics without request payloads; never infer zero partial effects from a generic failure label.

## Shared timing and safety contract

A queued action has one immutable start deadline; equality is expired. The deadline constrains the first side effect, not request arrival or mutex acquisition. Use local monotonic clocks for decisions; do not compare monotonic values from different processes. A no-effect yield may retain the original receiver-minted lease, never mint a replacement automatically. After input begins, expiry alone does not interrupt it; owner, lock, and target guards still apply. Cleanup releases run even after abort/expiry. Compatibility, protocol fields, and detailed package gates remain governed by `docs/R13-FIX-FRAMEWORK.md`.

## Scope and limits

This commit records the baseline and shared contracts only. It does not claim packaged-Windows acceptance, durable exactly-once effects, or watcher closure. No live relay or production hands process is used by these tests.
