RELAY FIX FRAMEWORK — REVIEWED HANDOFF — 2026-10-22

Start with Relay_Fix_Framework_Reviewed_2026-10-22.md or its Word copy.
They contain the same 14-page specification: F1–F7, watcher 38, invariants,
testable closure criteria, dependency-ordered packages, and challenge results.

BASELINE
  ADD-Attack/HandsRelayV0.1
  ee3cf0e2dbc4bd2b2cac3ee1651471aa41a6dbd1 (public R12)

CONTENTS
  Relay_Fix_Framework_Reviewed_2026-10-22.md
  Relay_Fix_Framework_Reviewed_2026-10-22.docx
  test_relay_fix_acceptance.py       Desired-behavior starter acceptance suite
  baseline_acceptance_results.txt   Unchanged R12: 18 failed, 3 passed
  verified_source_map.json          74 verified existing source locations
  F4_REFERENCE_UNVERIFIED.patch     Optional narrow diagnostic-retention patch
  original_review_probes.py         Historical reproduction script, not a gate
  verification.txt                 Commands and limits
  SHA256SUMS.txt                    Content hashes excluding this checksum file

RUN THE ACCEPTANCE SUITE
Use an isolated candidate checkout and Python environment with the candidate's
dependencies, pytest and Pillow. Run this file in its own process. From the
unpacked handoff directory on Linux/macOS:

  RELAY_REPO_ROOT=/absolute/path/to/candidate python -m pytest -q test_relay_fix_acceptance.py --tb=short

PowerShell:
  $env:RELAY_REPO_ROOT = 'C:\path\to\candidate'
  python -m pytest -q test_relay_fix_acceptance.py --tb=short

The 13 named probes collect 21 checks. The 18 failures on unchanged R12 are
expected desired-fix assertions, not fixture setup errors. Three controls pass.
Mandatory supplemental and Windows acceptance cases are specified in the
framework and are not all executable in this starter file.

Port fixtures to new lease, synchronization-lifecycle and capture seams as
those protocols are implemented. Preserve stimulus and expected observables.
Do not remove assertions, add xfail, or disable features to make the suite green.
P04 accepts failed during the narrow F4 stage; the final F3 state contract is
stricter. P09's import-twice restart seam must become a temporary-store lifecycle
test after persisted gateway epochs are implemented.

REFERENCE PATCH
Apply only to a disposable checkout of the baseline, then review:
  git apply --check /path/to/F4_REFERENCE_UNVERIFIED.patch
  git apply /path/to/F4_REFERENCE_UNVERIFIED.patch
  python -m pytest -q .

The patch is explicitly UNVERIFIED for production/Windows. Local isolated tests
passed 233 with 1 skipped; P04 passed separately. The framework lists missing
cross-field count validation and other limits. It is not full F4/F3 closure.
The original pinned review checkout and live relays were not changed.

VERSION SELECTION
Use this REVIEWED handoff for the resumed task. An older background session
also generated a framework without Reviewed in its filename. This package
incorporates a separate source and test challenge, including independent
owner/focus cases, a cross-thread lock check, healthy controls, and the revised
build dependencies. Do not mix test/spec files between the two packages.

MISSING EXTERNAL EVIDENCE
Both watcher implementations and packaged Windows tests are still required
for their exact source map/runtime closure. The specification itself is ready
for the team's review and implementation planning. No deployment is authorized.
