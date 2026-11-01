# R12 build report — resting float travels (wishlist #36)

**Branch:** `v3-r12-candidate-20261021`, from main `3eb1641` (full R11 source).
**Commits:** `f16f76d` (the #36 fix + tests), `934d05f` (release identity).
**Scope:** exactly one functional change (#36) plus the R11 → R12 version
identity. No #28 jiggler build-out, no #31–#35, no R7.2 fixes, no GUI work.
No exe build, no deploy, no push, main untouched.

## The defect

In R11, `motion.plan_rest` picks each float offset, but
`_RestingHand._move` executed the step as a single
`pyautogui.moveTo(target, duration=0)` — on the real desktop the pointer
teleported 1–3 px at a time (snap, hold still, snap). Measured on the
planner alone (seeded replay of `plan_rest` targets, R11-style single
emissions): worst single emitted jump was **5 px Chebyshev** (a target
across the resting disc after pull + step). Owner-observed live on R11
2026-10-21 07:13–07:15.

## The fix

### `hands/motion.py` — new planner: `plan_rest_path(x0, y0, x1, y1, rng)`

- Reuses the existing human-like machinery — it calls `_segment`, the
  same cubic-Bezier / minimum-jerk progress / value-noise sampler that
  `plan_move` (#24/#29) builds active moves from. No second motion
  system was invented. (`plan_move` itself could not be used directly:
  it returns a single step for moves under 4 px, and every float step
  is 1–3 px — the micro-path needed the sampler, not the click planner.)
- `_segment` yields float samples with per-sample delays summing to the
  glide duration (`rest_glide_seconds`: 0.08 s + 0.045 s/px, ±10 % draw,
  capped at `REST_GLIDE_MAX_S` = 0.30 s). The samples are then
  **densified**: between successive integer anchors, positions are
  interpolated so that consecutive emitted positions differ by at most
  **1 px per axis (Chebyshev ≤ 1)**.
- **Threshold and why:** 1 px is the display's own quantum — no smaller
  pointer move can be emitted — so at float scale this is the strongest
  continuity that exists: every pixel between here and there is visited,
  in order, spread over the eased duration, instead of the whole step
  landing in one frame. Densifier arithmetic uses round-half-up
  (`_rhu`): Python's banker's `round()` can open a 2 px gap on half-way
  values; round-half-up cannot when float neighbours differ by ≤ 1.
- Final emitted step is exactly the target; delays sum to the glide
  duration (drift-corrected, same pattern as `plan_move`).

### `hands/hands_relay_hands.py` — `_RestingHand._move` glides

- Plans the glide from the pointer's current position to the float
  target and injects the sub-steps (`moveTo(duration=0)` + the planned
  delays) — the same injection style `_run_path` uses for active moves.
- **Owner override mid-glide:** before every sub-step, the pointer must
  still be where the glide left it (±1 px). If it is not, the owner's
  hand is on the mouse: the glide aborts at once and `_move` returns
  None, so `step()` ends the episode exactly as it did for a fought
  instant move. Detection latency is one sub-step (~10–40 ms).
- **Provenance:** the SelfInputLedger span is recorded BEFORE injecting
  (unchanged pattern) with a provisional end of +1500 ms — the R11
  +250 ms no longer covers a glide of up to 300 ms — and
  `truncate_last` still closes the span at the true end (+50 ms), so
  float motion still never registers as owner activity.
- The input mutex is held for the glide (≤ 0.30 s) via the same
  `try_acquire` in `step()`; the resting hand still never contends with
  an act, and acts still block-acquire as before.
- `plan_rest`, `rest_intensity`, `REST_RAMP_S` (1.5 s), and
  `REST_RADIUS_PX` (4.0) are untouched — offset planning, ramp, radius,
  claim-held + agent-idle gating, act resets, and the no-timeout rule
  are byte-for-byte the R11 behaviour. Motion quality is the only delta.

## Version identity R11 → R12

Exhaustive grep of the tree (code, UI, config, build scripts) found
**no pre-existing runtime version string**: R11 never reported its own
release — its identity lived in branch/folder/commit names
(`v3-r11-candidate-20261020`, `%USERPROFILE%\HandsRelay-v3-r11`).
Every "R11" string in the source is a feature-attribution comment
(when a feature landed); those were left untouched as history.

Added, as the single reporting surface:

- `services/gateway/app.py`: `RELEASE = "v3 R12"` constant, and
  `GET /v1/info` now includes `"release": RELEASE` (additive key; the
  dashboard model and existing consumers read named keys).

That is the only place changed for version identity. If the reviewer
wants the release surfaced elsewhere (e.g. diagnostics text), it is a
one-line follow-on — not guessed at here.

## Tests

New file `tests/test_r12_rest_float.py` (9 tests):

- `test_glide_visits_every_pixel_no_jumps` — 8 glide shapes × 20 seeds:
  every emitted step ≤ 1 Chebyshev from the previous (incl. start),
  exact landing, non-negative delays, total within glide bounds.
- `test_glide_travels_instead_of_jumping` — ≥ 2 emitted steps and
  ≥ 0.08 s of travel for 3–5 px steps (the anti-snap proof).
- `test_glide_zero_length_is_empty`, `test_glide_duration_scales_with_distance_and_is_bounded`.
- `test_glide_never_leaves_the_radius_disc` — emitted sub-steps stay
  within the resting radius components.
- `test_glide_move_emits_continuous_positions` — executor-level, stubbed
  pyautogui: `_move` emits continuous positions, lands exactly, and the
  glide is recorded on the SelfInputLedger.
- `test_owner_grab_mid_glide_aborts` — pointer yanked +30 px after the
  first sub-step: `_move` returns None after exactly 1 injected sub-step.
- `test_episode_float_travel_is_continuous_and_local` — full
  `_RestingHand.step()` episode: all emitted positions (within and
  across glides) ≤ 1 Chebyshev apart and within the 4 px radius.
- `test_info_reports_release` — `/v1/info` carries `"release": "v3 R12"`.

Existing contract coverage was already in `tests/test_r11_resting_watch.py`
(ramp profile incl. no-timeout, radius/locality over 5000 `plan_rest`
steps, claim-held-only, lease expiry, act resets ramp, owner mouse +
keyboard input ends episode, still when locked/blind, never contends
with an op, moves on the self-ledger) — all kept and green.

### Results (this VM, /tmp venv: pytest 9.1.1, flask, requests,
pyautogui, waitress, pillow — same commands before and after)

| Suite | Baseline (3eb1641) | R12 candidate |
|---|---|---|
| `python -m pytest -q .` | 216 passed, 1 skipped | **225 passed, 1 skipped** |
| 15 script suites (`hands/test_*.py`, `services/gateway/test_*.py`, `test_app.py`, `test_gateway_token.py`, run as scripts) | all rc=0 (ALL PASS) | all rc=0 (ALL PASS) |

+9 is exactly the new R12 tests; the 1 skip is the pre-existing
Windows-only clipboard test, skipped identically at baseline.

## UNVERIFIED (not run here — needs the owner's Windows PC)

- **How the glide looks on the real desktop.** Continuity is proven at
  the emitted-position level; whether the travel *reads* as a resting
  hand at 1–3 px on the physical display (real pyautogui/OS pointer
  behaviour) is a live check, same status as R11's owed live receipts
  in docs/VERIFICATION.md.
- The packaged app end-to-end (`/v1/info` from the built exe) — no exe
  was built, per the task.
- Windows-only clipboard test (skipped here, as at baseline).

---

## FIX ROUND — independent review NO-GO → delta (2026-10-21)

An independent Astra-posture review of the candidate above returned
**NO-GO** (verdict: `~/workspace/relay-r12-review-20261021.md`). One
blocking finding (F1) plus two low findings (F2, F3) are fixed in
commit `488b993` on this branch. Everything else in the review —
owner-override, ledger provenance on non-fault paths, contract
preservation, version identity, the builder's test counts — was
independently verified by the reviewer and stands; the in-envelope
float behaviour was also brute-forced clean by the reviewer (70,203
glides, zero violations), so F1 was latent, not live. It blocked
because the guarantee was *stated* unconditionally while being false
in general, in the helper designated for reuse by #28/#30.

### F1 (moderate, blocking) — densifier interpolated from the moving point

`plan_rest_path` computed each sub-step as
`_rhu(ex + (gx - ex) * k / cheb)` with `ex` the **running** emitted
position, re-multiplying the remaining distance by the whole-span
fraction at every sub-step; steps grew super-linearly and spans
≥ 4 Chebyshev at longer glides emitted 2 px jumps (reviewer's repro:
`(0,0)→(40,3)` seed 1 emitted `(22,-1)→(24,0)`; reproduced here
pre-fix, plus a second jump `(26,0)→(28,1)` in the same glide).
**Fix:** each anchor span now interpolates from its fixed starting
point (`bx, by` captured before the k-loop). Ideal sub-steps then
move ≤ 1 px per axis (per-axis span delta ÷ cheb ≤ 1), and `_rhu` —
monotone, with `_rhu(v+1) = _rhu(v)+1` — preserves that bound through
rounding, so the ≤ 1 px guarantee now holds **for spans of any
length**, unqualified. The `plan_rest_path` docstring was updated to
state the construction; the guarantee wording elsewhere in this
report ("at most 1 px per axis") now matches that true scope.
Post-fix, the repro glide emits 40 steps (was 38 with two jumps),
worst consecutive step 1 px.

### F2 (low) — start frame used `int()` truncation, anchors use `_rhu`

For negative float starts the two frames disagree by up to 1 px,
which could stack with real motion into a 2 px first emitted step
(unreachable in production — the only caller passes ints — but the
function accepted floats silently). **Fix:** the start frame is
`_rhu(float(x0)), _rhu(float(y0))`, and the final-step rewrite /
empty-path fallback also emit in the `_rhu` lattice instead of
`int(...)` — positionally identical for integer targets (the
production call shape), and the continuity chain now closes
frame-to-final-step for float endpoints too.

### F3 (low) — exception mid-glide left the ledger span at 1500 ms

If `moveTo` raised mid-glide, `_move` propagated before
`truncate_last` ran, leaving the SelfInputLedger span at the widened
1500 ms provisional (R11's equivalent fault left 250 ms) — a wider
window in which real owner input could be misread as self-input, on
the fault path only. **Fix:** plan + glide in `_move` are wrapped in
try/finally with `truncate_last` in the finally. Non-fault paths are
unchanged (the close still happens at the same point, before the
fought/landing checks); on a fault the exception still propagates to
`_run`'s handler exactly as before — only the span close is added.

### F4 (informational) — not actioned

The reviewer noted the radius test asserts per-axis components rather
than the radial bound (measured transient max 4.243 px — a
pixel-quantisation effect, contract surface `plan_rest`'s clamp
untouched). Classified by the reviewer as a test-honesty note, not a
behaviour defect, and not part of the path back to GO; left as-is in
this round.

### Tests added (+4, all in `tests/test_r12_rest_float.py`)

- `test_long_glides_hold_the_1px_bound_at_any_span` — 9 shapes
  (shallow, flat, diagonal, steep, negative, offset-start) × 50
  seeds, spans to 50 px: every consecutive emitted pair (start
  included) ≤ 1 px per axis, exact landing, ≥ 1 emitted step per
  dominant-axis pixel, delays non-negative and totalling within the
  glide cap. (The pre-round suite maxed at ~5.7 px shapes — the
  region where F1 is unreachable — which is why it missed this.)
- `test_long_glide_reviewer_repro` — the exact F1 verdict case.
- `test_float_start_frame_matches_anchor_rounding` — float starts
  (incl. negatives) × int targets × 30 seeds: full chain ≤ 1 px per
  axis measured from the `_rhu` frame (F2).
- `test_move_exception_mid_glide_still_closes_ledger_span` —
  `moveTo` raises on the 2nd sub-step: the exception propagates and
  the ledger span is closed at t0 + 50, not left at t0 + 1500 (F3).

### Results after the fix round (same venv/commands as above)

| Suite | Before fix round | After fix round |
|---|---|---|
| `python -m pytest -q .` | 225 passed, 1 skipped | **229 passed, 1 skipped** |
| 15 script suites (run as scripts) | all rc=0 | all rc=0 |

+4 is exactly the new regression tests; no other test changed. The
hard contract (`plan_rest`, `rest_intensity`, `REST_RAMP_S`,
`REST_RADIUS_PX`, claim/idle gating, act resets, no timeout) is
byte-identical to the pre-round candidate — the fix-round diff is
confined to `plan_rest_path`, `_move`, and the test file.
Pre/post ad-hoc sweeps by the fixer (separate from the suite): the
550-glide long grid, the float-start grid, and 500 in-disc
float-endpoint glides all show worst consecutive step 1 px after the
fix.

### UNVERIFIED after the fix round

Unchanged from the list above: live Windows desktop perception of
the glide, the packaged exe end-to-end, and the Windows-only
clipboard test remain UNVERIFIED (not run here — no exe built, no
deploy, per the task). The delta re-review of this fix round is the
reviewer's, not the fixer's.
