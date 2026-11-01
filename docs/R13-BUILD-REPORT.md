# R13 candidate build report

Build only; no deployment or live R12 changes. Runtime Windows acceptance remains open.

## B00 — inherited

Commit `1933b3b58748d86caf81dca85b99435896c6f1e1`: frozen contracts and reproduced baseline (18 acceptance failures / 3 passes; 229 pre-existing passes / 1 skip).

## B01 — held diagnostics

Added `_held_failure_diagnostics` in `services/gateway/app.py` and used it for terminal held failures. Fresh allowlisted diagnostics exclude arbitrary upstream prose and payloads. Count bounds match hands' 1,000,000-byte request ceiling; contradictory count pairs are omitted, not repaired. Added privacy, mutation, terminal/no-replay, owner/lock and count-boundary tests.

Commands (Python = `/home/openclaw/.venvs/HandsRelay-r13/bin/python`):

- `python -m pytest -q tests/test_f4_diagnostics_reference.py tests/test_relay_fix_acceptance.py -k 'count_evidence or partial or retains or omits or rejects_wrong or p04'`: 15 passed, 20 deselected.
- `python -m pytest -q --ignore=tests/test_relay_fix_acceptance.py`: 243 passed, 1 skipped.

Source review: failure exits remove pending payload; second pump cannot dispatch terminal receipts. Direct-path parity and richer terminal states belong to B02/B03. No Windows closure claimed.

## B02 — implementation checkpoint; review gate BLOCKED

Added `classify_act_outcome` and `_attempt_act` in `services/gateway/app.py`; direct, initial queued, and held paths share validated evidence classification. Invalid/lost/contradictory responses return 502 with `unknown_outcome`, never automatic replay. Initial receipt identity exists before dispatch. Safe copied results exclude arbitrary exception text and payload echoes. Added 41 boundary tests covering direct/queued/held malformed results, contradictory evidence, pre-dispatch configuration rejection, pre-send receipt creation and post-send exceptions. B03 abort-state/UI work has not begun.

Commands:

- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_relay_fix_acceptance.py -k 'p02 or p03 or p04 or p05'`: 8 passed, 13 deselected.
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_r13_outcomes.py tests/test_f4_diagnostics_reference.py tests/test_relay_fix_acceptance.py -k 'not p01 and not p06 and not p07 and not p08 and not p09 and not p10 and not p11 and not p12 and not p13'`: 63 passed, 13 deselected.
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q --ignore=tests/test_relay_fix_acceptance.py`: **1 failed, 283 passed, 1 skipped**.
- `git diff --check`: clean.

### Named blocker: contradictory frozen regression contract

`tests/test_v3r2_remaining.py:204-205` supplies HTTP 409 with only `{"error": "user active — control yielded"}` (no boolean `ok`, no machine-readable refusal) and requires `held`. The F3 Required behavior paragraph explicitly requires a boolean `ok`, makes malformed evidence terminal unknown after possible dispatch, and allows only verified no-effect refusals to retry. The same old test also expects a malformed no-ok `offset mismatch` response to become `failed` rather than unknown (line 211).

The old test is unchanged. Preserving its behavior would violate the R13 acceptance specification; changing its expectations is prohibited by the operator's no-adjusting-expected-outputs boundary. Owner resolution of this contract conflict is required. No fixture sniffing, legacy-only bypass, xfail, or skips were introduced to hide it. B02 remains gate-blocked; B03–B18 have not been started, respecting the sequential package/review requirement. This checkpoint is NOT a completed R13 candidate.

Unresolved: whether the operator authorizes an explicit, reviewed retirement/update of this superseded R12 behavior contract. No production or packaged Windows validation was attempted.

### B02 supersession authorized and gate cleared

Muse's explicit operator authorization resolves the conflict in favor of F3. In `tests/test_v3r2_remaining.py::test_pump_maps_2xx_yield_and_everything_else`, lines 205–206 and 211 only: malformed no-`ok` yield evidence now asserts `unknown-outcome` and no pending payload (previously `held` and retained payload); malformed offset-mismatch evidence now asserts `unknown-outcome` (previously `failed`). Stimuli and every other assertion in that file are unchanged. Authority: framework F3, Required behavior, and operator's specific supersession order. This is not authorization to change further legacy expectations.

Rerun commands:
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q --ignore=tests/test_relay_fix_acceptance.py`: 284 passed, 1 skipped.
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_relay_fix_acceptance.py -k 'p02 or p03 or p04 or p05'`: 8 passed, 13 deselected.

## B03 — receipt states and visibility

Added schema-valid `aborted-owner` / `aborted` classification; missing receipt lookup now says `unavailable`, never nonexecution. UI state chips/colors explicitly represent unknown, abort, in-flight and unavailable. README documents injection-vs-effect, no replay, and volatile retention. Existing dashboard snapshot/count/feed functions preserve arbitrary state strings without remapping; new tests exercise those actual functions, polling/list endpoints and eviction. Browser interaction remains unverified; source feed has no replay control.

Commands:
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q --ignore=tests/test_relay_fix_acceptance.py`: 288 passed, 1 skipped.
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_relay_fix_acceptance.py -k 'p02 or p03 or p04 or p05'`: 8 passed, 13 deselected.

No further pre-existing test expectations changed. Source map: gateway `classify_act_outcome`, `act_queued_get`; UI `STATE_CHIP`, `STATE_COLOR`; existing model `_snapshot_receipt_rows`, `summarize_receipts`, `receipt_state_counts` audited unchanged.

## B04 — receiver-local execution lease protocol

Hands now provides authenticated `/execution/capabilities` and `/execution/lease` (version 1). `_execution_mint` stores opaque, bounded-lifetime boot-bound identities with canonical private operation digests; `_execution_check` validates operation, boot, consumption and equality expiry and atomically consumes one start. Capacity refuses rather than evicting live identities. No mint/check operation injects input. Source map: these helpers/routes in `hands/hands_relay_hands.py`; gateway negotiation and side-effect consumption are B05/B07 integration work.

Commands:
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_r13_execution_leases.py`: 11 passed.
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q --ignore=tests/test_relay_fix_acceptance.py`: 299 passed, 1 skipped.

Fixtures cover independent receiver clock, wall-time jumps, adversarial transit delay/equality expiry, wrong boot/token/operation, invalid lifetimes, authenticated minting, and a two-thread atomic-start race. Old-hands gateway refusal is tested with B05, where capability negotiation is wired. No packaged runtime claim.

## B05 — gateway lease negotiation and atomic retry reservation

Gateway `_prepare_queued_execution` negotiates authenticated version/boot, pins available foreground before digest minting, and fails closed without input on incompatible hands. `_queued_attempt` uses a context-local lease carrier so private metadata is not added to payload-key diagnostics; `_forward_act` sends it only on the wire. A hold retains one lease. Caller internal execution fields are stripped. `_pump_receipt` checks immutable monotonic expiry before dispatch, reserves one attempt under the receipt lock, performs I/O unlocked, and samples elapsed time on settlement. Concurrent eviction leaves uncertainty, not false never-started evidence.

Protocol adapter: the fixture in `tests/test_relay_fix_acceptance.py` now supplies a successful fake `_prepare_queued_execution` result so P02/P03 still inject lost/malformed **action** responses, after the new prerequisite handshake. No stimulus or expected outcome was changed. This follows the framework's explicit new-seam adapter allowance; it is not a further legacy expectation supersession. Real handshake behavior has separate B05 tests.

Commands:
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_r13_gateway_leases.py tests/test_relay_fix_acceptance.py -k 'not p06 and not p07 and not p08 and not p09 and not p10 and not p11 and not p12 and not p13'`: 16 passed, 12 deselected.
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q --ignore=tests/test_relay_fix_acceptance.py`: 306 passed, 1 skipped.

Tests cover old/incompatible hands (zero POSTs), same lease through yields, one concurrent dispatch, eviction during dispatch, and a slow first receipt expiring the second. Receipt-only helper fixtures without monotonic metadata use supplied time plus measured monotonic elapsed duration; actual queued receipts always carry receiver-independent local monotonic deadlines. First-effect enforcement remains B07.

## B06 — guarded input checkpoint; review gate BLOCKED

`_input.check` revalidates owner activity, desktop lock, expected foreground and lease after mutex acquisition/hand-travel. Failed `__enter__` releases on every exception, and never resets resting state. `_InputRefused` has a Flask handler. Focus and clipboard-set now use the shared mutex. First-effect/after-preparatory-wait consumption and multistep guards remain B07/B11 work; this checkpoint does not claim them complete.

Commands:
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_relay_fix_acceptance.py -k p07`: 3 passed, 18 deselected.
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_r13_guarded_input.py tests/test_relay_fix_acceptance.py -k 'lock_change or p07'`: 13 passed, 18 deselected. Includes all ten side-effect routes, no input/ramp reset on post-acquisition lock change, and lock acquisition by a different thread after refusal.
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q --ignore=tests/test_relay_fix_acceptance.py`: **1 failed, 315 passed, 1 skipped**.
- `git diff --check`: clean.

### Further named conflict — R7.2 single-door idle-probe expectation

`tests/test_r72_fixround.py::test_lock_check_is_lock_only_not_self_triggering_idle` is unchanged. At lines 269–271 its fake reports `(None, None)` on the first probe and `(False, 0.0)` on every subsequent probe, even before any key has been injected. Lines 287–288 require complete successful typing and exactly ONE idle probe (`calls["n"] == 1`). F2 requires a new owner/lock check under input access after acquisition, before any side effect. That second check therefore sees newly reported activity and correctly refuses; bypassing it would regress P07.

This is distinct from self-triggered idle *after* injection, which must continue to be ignored/attributed correctly. Proposed narrow resolution for operator review: keep the successful full-text assertion, change the fixture to report zero idle only once `s.written` is nonempty (actual simulated self input), and replace the single-probe assertion with evidence of the required pre-input revalidation. This preserves the original anti-self-trigger purpose without prohibiting F2. No changes to this test, its expectations, or other legacy expected outputs have been made. B07 onward is not started pending this gate, per the one-conflict-at-a-time operator direction.

### B06 supersession authorized and gate cleared

Muse authorized the minimal F2 harness correction. Only `tests/test_r72_fixround.py` lines 271 and 288 changed: simulated zero idle now begins after `s.written` contains an injected key, instead of on the second probe regardless of input; the exact-one-probe assertion becomes at least two (door and post-acquisition revalidation). The full-text successful-typing assertion and remainder of the file are unchanged. Authority: F2's required guard revalidation under input access and explicit operator authorization. This preserves the anti-self-trigger regression purpose.

Reruns:
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q --ignore=tests/test_relay_fix_acceptance.py`: 316 passed, 1 skipped.
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_r13_guarded_input.py tests/test_relay_fix_acceptance.py -k 'lock_change or p07'`: 13 passed, 18 deselected.

## B07 — first-side-effect timing checkpoint; review gate BLOCKED

Hands `_input.before_effect`, `_before_effect`, and `_inject` move lease consumption and resting-act start to the injection boundary, after preparatory waits. Every side-effect route calls this boundary, including focus and clipboard-set; clipboard reads may precede it but mutations may not. Once started, lease expiry does not abort completion. Mouse/button releases remain unguarded cleanup. Native click-down exceptions release the button. A refused paste cannot trigger typing fallback or an unnecessary clipboard restore. Existing type partial-count lock checkpoints remain in the typing implementation pending B10; first-effect timing applies to both typing paths now.

Commands:
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_r13_first_effect.py`: 15 passed. Covers all ten routes expiring during acquisition, attention delay, clipboard read delay, valid start followed by expiry, consumed-lease replay, no-effect versus injecting context/ramp behavior, and cleanup after a native press exception.
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_relay_fix_acceptance.py -k 'p01 or p02 or p03 or p04 or p05 or p07'`: 12 passed, 9 deselected.
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q --ignore=tests/test_relay_fix_acceptance.py`: **1 failed, 330 passed, 1 skipped**.
- `git diff --check`: clean.

### Further named conflict — R11 context entry treated as a real act

`tests/test_r11_resting_watch.py::test_real_act_resets_the_global_ramp`, lines 190–192, opens `_input("mouse")` and immediately expects the resting intensity to reset despite performing no injection. The framework's shared timing contract says a refused action that never started must not reset the resting ramp or clear owner-stop state. An action may acquire input access and subsequently expire during attention/read preparation; therefore entering the mutex cannot irrevocably mark a real act. B07 starts/rests the act only at the first-side-effect boundary.

Proposed minimal harness resolution: add one simulated movement via `_inject` inside this test's input context, before its existing reset assertions. Keep all intensity and `last_act_end` expectations unchanged. This makes the test actually exercise the real act its name promises. New B07 tests separately prove a no-effect context leaves resting state untouched and a real injection calls begin/end. The legacy file remains unchanged; no further expectation/harness supersession is assumed. B08 onward has not started pending this one-at-a-time gate resolution. No deployment or live R12 changes.


### B07 setup adjustment authorized and gate cleared

Muse authorized the minimal setup change in `tests/test_r11_resting_watch.py::test_real_act_resets_the_global_ramp`. Added one simulated movement, `hands._inject(hands.pyautogui.moveTo, 1, 2)`, inside the existing input context and before the existing intensity assertions. No expected values changed. Authority: B07/F1 first-side-effect timing; a context that never injects must not reset the resting ramp.

Reruns after the setup change:
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q --ignore=tests/test_relay_fix_acceptance.py`: 331 passed, 1 skipped (prior interrupted session).


## B08 — held target policy

Locked queued acts without a caller target now settle `failed` / `target_unknown` and drop the payload. A locked response cannot contribute a foreground pin. Caller `expect_focus`, including `process_instance`, is preserved. Capability pinning skips `locked: true` and includes a process instance when supplied. `_focus_matches` fails closed when a requested process instance is missing, stopped, or different. Focus re-enumerates the selected window under input access and returns `target_unavailable` if it disappeared before activation.

Commands:
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_r13_targets.py`: 3 passed.
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q --ignore=tests/test_relay_fix_acceptance.py`: 334 passed, 1 skipped.

P06 is covered by `test_p06_locked_without_target_is_terminal`. Windows packaged focus races remain unverified.


## B09 — partial: owner counter at resting-glide boundaries

`_RestingHand._move` samples `_owner_input_snapshot` before every sub-step. A changed owner-event count stops before the next pointer move, even when the pointer is still where the glide expects it. A missing snapshot does not invent owner activity. This does not yet disable resting motion on hook death or cover every non-rest route; those remain the rest of B09/B10/B11.

Commands:
- `RELAY_REPO_ROOT=$PWD /home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_relay_fix_acceptance.py -k p11`: 2 passed, 19 deselected.
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_r11_resting_watch.py tests/test_r12_rest_float.py`: 40 passed.


## B09 — owner observation service

`_observe_owner_counter` keeps the physical-event count and the monotonic time of the latest count change across actions. A hook event inside the yield window yields even when a self-input span covers the idle timestamp. A quiet hook does not treat a span-explained idle tick as owner activity; an unexplained idle change still yields. `mark_owner_coverage_degraded` stops new resting glides and `/state` reports `owner_coverage: degraded` without claiming reliable discrimination. Ordinary injections record their tick span. Tick rollover stays on the existing modular `tick_diff`. A mid-glide exception still truncates the provisional span.

Commands:
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_r13_owner_service.py tests/test_relay_fix_acceptance.py -k 'recent_owner or degraded_coverage or tick_rollover or p07 or p11'`: 8 passed, 16 deselected.
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q --ignore=tests/test_relay_fix_acceptance.py`: 337 passed, 1 skipped.


## B10 — guarded typing and narrow paste fallback

### Legacy expectation revision (Muse ruling)

Authority: B10 and F5, "Required behavior and invariants". The same bounded key loop is used whether or not jitter is on; jitter changes delays only. Owner, lock, and focus are sampled before each character. An owner event during the current unit stops the next unit. The 12-character boundary is not the contract.

Only the assertions that encoded that old boundary were changed. No other assertions in these tests were edited.

| Test | Old expectation | New expectation |
| --- | --- | --- |
| `tests/test_r72_fixround.py::test_owner_input_between_injection_and_postwrite_probe_aborts` | `chars_typed == 24` and `URL40[:24]` | `chars_typed == 18` and `URL40[:18]` |
| `tests/test_r72_fixround.py::test_type_aborts_on_lock_without_input` | `chars_typed == 24`, `len(written) == 24`, `URL40[:24]` | 18 / 18 / `URL40[:18]` |
| `tests/test_r72_fixround.py::test_probe_failure_then_recovery_still_detects_owner` | `chars_typed == 24` | `chars_typed == 18` |
| `tests/test_r72_fixround.py::test_owner_input_past_publish_grace_is_detected` | `chars_typed == 24` | `chars_typed == 14` (owner during the sleep after character 14) |
| `tests/test_r72_fixround.py::test_hook_mode_owner_on_sleep_18_aborts_at_24` | `chars_typed == 24` | `chars_typed == 18` |
| `tests/test_v3r4.py::test_type_aborts_midway_when_owner_activates` | `chars_typed == 24` and `len(writes) == 24` | 18 and 18 |

`test_owner_input_after_char12_aborts_before_char13` already stopped at the next character and was not changed.

### Behavior

`/type` keys mode, jitter true or false, writes one character per unit and sleeps the planned delay or a fixed 0.02 s. A hook event during the current write is still that owner's event and cancels the following character. No characters yet means a yield or focus refusal, not a partial abort. A focus change after characters keeps `chars_typed`. Hook responses expose `owner_attribution_limit: non-injected-events-only` (other injected input is not this relay). dwTime responses expose `same-tick-and-grace-blind`. `/state` reports the same limit. There is no clipboard generation counter: restore treats an equal string as still ours.

Paste falls back to keys only when the clipboard is missing or `set_text` fails before a mutation. After a mutation, owner/lock/focus before Ctrl+V does not paste and does not type. Restore runs only if the clipboard still holds the placed value. A chord that may have been sent returns `paste_uncertain` with no `chars_typed` and no typing fallback, and releases Ctrl and V.

Commands:
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_r13_guarded_typing.py tests/test_r72_fixround.py tests/test_v3r4.py::test_type_aborts_midway_when_owner_activates tests/test_v3r4.py::test_type_completes_when_owner_stays_away tests/test_v3r2_remaining.py::test_paste_restores_clipboard_when_hotkey_raises tests/test_r13_first_effect.py`: 50 passed.
- `RELAY_REPO_ROOT=$PWD /home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_relay_fix_acceptance.py -k 'p12 or p11'`: 4 passed, 17 deselected.
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q --ignore=tests/test_relay_fix_acceptance.py`: 346 passed, 1 skipped.


## B11 — ordinary input and resting-motion guards

Pointer steps, scroll chunks, key presses, hotkey chords, break chords, drag segments, and focus activation sample owner, lock, and focus before each unit. The first blocked unit still yields or returns the existing focus refusal. After any side effect the result is `input_aborted`, which is not a yield, and the held-diagnostic allow-list retains that code. A direct click or hotkey that fails after the native call releases the button or every chord key. Drag still releases a button that went down. Resting sub-steps also stop on lock or degraded coverage before the next move. No native absolute-pointer injection was added, so this package does not depend on B15.

Commands:
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_r13_guarded_motion.py`: 7 passed.
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q --ignore=tests/test_relay_fix_acceptance.py`: 353 passed, 1 skipped.
- `RELAY_REPO_ROOT=$PWD /home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_relay_fix_acceptance.py -k 'p11 or p12 or p07'`: 7 passed, 14 deselected.


## B12 — epoch store and exclusive gateway authority

`services/gateway/epoch_store.py` is the authority store. The gateway process loads it from `bind_claim_authority()` before `serve`. The directory is `HANDS_RELAY_STATE_DIR`, otherwise `services/gateway/state` beside the gateway module. `authority.lock` is an OS exclusive lock and dies with the process. `epoch.json` and `high-water.json` move together by replace-and-fsync. Epochs are positive integers, not UUIDs or wall times.

A second live process is denied. A killed holder lets the next process allocate a strictly higher epoch. A one-step crash (epoch file published, marker not yet updated) is adopted and not reused. Disk failure, a truncated file, a missing half of the pair, and a file rewritten to an older epoch fail closed. The store does not start again at zero. The only reset is `recover("reset-pairing")`. Claim message sequencing stays on the existing push counter until B13 binds messages to this epoch; this package does not pretend those pushes are ordered by the new epoch yet.

Commands:
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_r13_epoch_store.py`: 3 passed.
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q --ignore=tests/test_relay_fix_acceptance.py`: 356 passed, 1 skipped.


## B13 — hands epoch validation and receiver-timed permits

Authoritative pushes carry pairing, epoch, sequence, hands boot, stable claim id, and a single-use challenge fetched at send time. Hands anchors the motion permit at challenge issuance plus the remaining lease, and never later than issuance plus 2 seconds. `SYNC_REFRESH_S` is 0.5 for the later refresh loop. A higher epoch wins even with a lower sequence. Duplicate grants acknowledge without moving the deadline. A grant that arrives after its own lease does not activate. The accepted epoch is stored before motion is enabled; a failed store leaves motion off and sets `sync_error`. A new hands boot clears grants. An old boot id cannot revive them. The persisted epoch still rejects anything older.

The legacy `{held, expires_in, seq}` body still applies. `tests/test_r11_resting_watch.py::test_resting_claim_route_validates` asserts that body, and this package does not change that assertion. The bound gateway does not use that body.

P09 setup only. No assertion line in `tests/test_relay_fix_acceptance.py` changed. Exact diff against the pre-B13 file:

```diff
diff --git a/tests/test_relay_fix_acceptance.py b/tests/test_relay_fix_acceptance.py
index c76f20e..1bce8c2 100644
--- a/tests/test_relay_fix_acceptance.py
+++ b/tests/test_relay_fix_acceptance.py
@@ -8,12 +8,13 @@ They use simulated desktop/network boundaries; Windows acceptance is separate.
 The framework defines new watcher fields and claim-sync behavior used here.
 """
 import importlib.util
 from io import BytesIO
 import itertools
 import os
+import tempfile
 from pathlib import Path
 import random
 import sys
 import threading
 import types
 from urllib.parse import urlsplit
@@ -289,21 +290,26 @@ def test_p09_gateway_restart_release_reaches_surviving_hands(gateway, hands, mon
 
     monkeypatch.setattr(gateway.requests, "post", local_post)
     monkeypatch.setattr(gateway.requests, "get", local_get)
     monkeypatch.setattr(gateway._claim_push_session, "post", local_post)
     monkeypatch.setattr(gateway._claim_push_session, "get", local_get)
     monkeypatch.setattr(gateway.threading, "Thread", ImmediateThread)
+    authority_dir = tempfile.mkdtemp()
+    hands._claim_sync = hands.claim_sync.ClaimSync(os.path.join(authority_dir, "hands"))
+    gateway.bind_claim_authority(authority_dir)
     gateway._claim_push_seq = 6
     gateway._claim = {"holder": "old", "purpose": "test", "expires_at": clock.value + 60}
     gateway._push_claim_locked(gateway._claim, clock.value)
     assert hands._resting.claim_held(hands._resting._clock())
+    gateway._claim_authority.release()
     restarted = load("services/gateway/app.py")
     restarted.HANDS_SECRET = hands.SHARED_SECRET
     restarted.time = clock
     monkeypatch.setattr(restarted._claim_push_session, "post", local_post)
     monkeypatch.setattr(restarted._claim_push_session, "get", local_get)
+    restarted.bind_claim_authority(authority_dir)
     restarted._claim = None
     restarted._push_claim_locked(None, clock.value)
     assert not hands._resting.claim_held(hands._resting._clock())
```

`hands/test_process_wait.py` also copies the new sibling `claim_sync.py` into its temporary import directory, the same way it already copies `motion.py`. No assertion there changed.

Commands:
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_r13_claim_sync.py`: 6 passed.
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q --ignore=tests/test_relay_fix_acceptance.py`: 362 passed, 1 skipped.
- `RELAY_REPO_ROOT=$PWD /home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_relay_fix_acceptance.py -k p09`: 1 passed, 20 deselected.


## B14 — coalesced claim reconciliation

One worker sends the latest claim snapshot. A failed or rejected acknowledgement retries that snapshot with a new sequence and a new challenge. Backoff caps at `SYNC_RETRY_MAX_S` (0.5 s). `/v1/info` carries `claim_sync`, and the dashboard diagnostics line says whether sync is ok or degraded. Binding authority with no live claim pushes an explicit release.

A refresh of the same `claim_id` after the permit lapses does not move `last_act_end` and does not clear `owner_stopped`. A permit that was set and has expired stops the next resting sub-step. A direct glide with no permit is unchanged.

Commands:
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_r13_reconcile.py`: 5 passed.
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q --ignore=tests/test_relay_fix_acceptance.py`: 367 passed, 1 skipped.
- `RELAY_REPO_ROOT=$PWD /home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_relay_fix_acceptance.py -k p09`: 1 passed, 20 deselected (with the B13 setup).


## B15 — virtual-surface capture

`_grab(None)` is still `pyautogui.screenshot()` with no region. That is the legacy default extent. A rectangle is cropped from one all-screens bitmap. Pillow `ImageGrab.grab(all_screens=True)` is the selected backend. `pyautogui.screenshot(allScreens=True)` is only the fallback when ImageGrab cannot run, and it is not given a region tuple. With virtual origin `(vx, vy)`, global `(x, y, w, h)` is the surface crop `(x-vx, y-vy, w, h)`.

A rect that does not lie on that bitmap is HTTP 400. The response is JSON, not a padded black PNG. A bitmap whose size is not the topology union is HTTP 409. There is no rescale. The DPI mode a packaged hands process must use is Per-Monitor-V2, so these pixels match input coordinates. This module does not call `SetProcessDpiAwareness`. A second topology read after the grab must match the first. The screenshot route retries that refusal once, then returns 409. Monitor index is not treated as a permanent display identity: the response also carries the global rect.

The route still calls `_grab` with one positional argument. The realism stubs that replace `_grab` keep working. Variant responses add `X-Screen-Origin`, `X-Screen-Surface`, and `X-Screen-Rect`. The legacy frame does not.

P13's assertions were not edited. `git diff -- tests/test_relay_fix_acceptance.py` is empty. P13 passes: green at `x=-100`, blue at `x=100`.

The only check changed in `hands/test_capture_realism.py`:

- File: `hands/test_capture_realism.py`
- Old check name: `_grab forwards region tuple only when a rect is given`
- New check name: `_grab forwards a rect against the virtual surface, not as a global region`
- Old expectation: `calls["screenshot"] == [None, (10, 20, 300, 200)]`
- New expectation: the legacy call is `("pyautogui", None, False)`. The rect requests ImageGrab with `all_screens=True` and no bbox. The crop is 300 by 200 and its `(0, 0)` pixel is surface pixel `(1290, 20)`, which is global `(10, 20)` at virtual origin `x=-1280`. The global tuple is not forwarded (`calls["screenshot"] == []`).

Authority named by the ruling: P13 and the framework section the ruling asked to cite. P13 is the F7 row in the regression map. The crop implemented is the one in "F7 Secondary monitor capture uses the wrong surface": `(x-vx, y-vy, x-vx+w, y-vy+h)`. The ruling text says "P13 + the F6 framework section". F6 is claim synchronization. No F6 code and no F6 assertion were changed. No other check in `hands/test_capture_realism.py` was edited.

```diff
     calls["screenshot"].clear()
-    orig_grab(None)
-    orig_grab({"x": 10, "y": 20, "w": 300, "h": 200})
+    from PIL import ImageGrab
+    recorded = []
+    surface = Image.new("RGB", (3200, 1080), (0, 0, 0))
+    surface.putpixel((1290, 20), (7, 8, 9))
+    # recorders installed only for this check; restored in finally
     check(
-        "_grab forwards region tuple only when a rect is given",
-        calls["screenshot"] == [None, (10, 20, 300, 200)],
+        "_grab forwards a rect against the virtual surface, not as a global region",
+        recorded == [("pyautogui", None, False), ("imagegrab", None, True)]
+        and calls["screenshot"] == []
+        and cropped.size == (300, 200)
+        and cropped.getpixel((0, 0)) == (7, 8, 9),
     )
```

The full hunk is `git diff 95ddb94 -- hands/test_capture_realism.py`. The lines above are the expectation change. The recorders exist so this check can see the virtual-surface call without editing `_screenshot` or any other check.

`hands/requirements-capture.txt` names the backend: `pillow==13.0.0` (the Linux simulation) and `pyautogui`. `build_app.bat` installs that file, hidden-imports `PIL.ImageGrab`, and also bundles `hands/claim_sync.py`. The hands module loads that sibling by path; without it a packaged exe cannot start. `hands/build_hands_relay.bat` installs from the same requirements file.

Commands:
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_r13_capture.py tests/test_relay_fix_acceptance.py::test_p13_secondary_capture_uses_virtual_origin`: 9 passed.
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python hands/test_capture_realism.py`: the virtual-surface check passes. One untouched check still fails: `jitter=false: single legacy write, no per-char sleeps`. B10 types per character for both jitter values. That check was not edited.
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q --ignore=tests/test_relay_fix_acceptance.py`: 375 passed, 1 skipped.
- `RELAY_REPO_ROOT=$PWD /home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_relay_fix_acceptance.py`: 20 passed, 1 failed. The failure is P08, `last_success_at` is absent. Gateway watcher code was not modified. That field is B17. P13 passed in this run.

Unresolved: F7-C packaged Windows evidence is not part of this package. No executable was built here, and no DPI measurement was taken.


## B16 — capture variant on the gateway

`/v1/screen` ETags are `variant:pixel-hash`. The variant is `legacy`, `monitor=N`, or `region=x,y,w,h`. A conditional request matches only that same variant. Identical pixels on two variants do not 304 each other. The gateway does not keep a screenshot cache: each route calls the hands again.

Hands response headers `X-Screen-Width`, `X-Screen-Height`, `X-Screen-Monitor`, `X-Screen-Region`, `X-Screen-Origin`, `X-Screen-Surface`, and `X-Screen-Rect` are copied onto the gateway response. The gateway also sets `X-Screen-Variant`. If a hands header names a different monitor or region than the request, the gateway returns 502 and does not hash those bytes. The same refusal applies to `/v1/screen/hash`.

`/v1/screen/hash` and `/v1/screen/wait` add `variant` and `capture`. The `hash` field remains the pixel digest, so an existing `since` comparison still compares pixels. That digest is not a variant identity. Callers have to read `variant` with it.

`_fetch_screenshot` still returns the historical three-tuple. Variant headers ride a thread-local that each screen route clears before the fetch, so a test stub that returns three values cannot reuse the previous capture's headers.

F7-C is not closed. No Windows package was built in this session, so there is no executable hash, no measured topology, no DPI mode, and no image dimensions from a packaged build at 100/125/150 percent. The required mode, recorded in `capture_virtual_surface`, is Per-Monitor-V2. `hands/requirements-capture.txt` pins `pillow==13.0.0` for the Linux simulation only. pyautogui is named there and is not installed as a distribution in the test environment; the tests inject a fake. Simulation cannot close this gate.

Commands:
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_r13_capture_gateway.py`: 4 passed.
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q --ignore=tests/test_relay_fix_acceptance.py`: 379 passed, 1 skipped.
- `RELAY_REPO_ROOT=$PWD /home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_relay_fix_acceptance.py`: 20 passed, 1 failed. The failure is still P08 (`last_success_at` is absent). No acceptance assertion was edited. P13 passed.

Unresolved: F7-C packaged Windows evidence, and P08's watcher success timestamp (B17).


## B17 — relay watcher freshness

Contact and observation are different clocks. `last_seen` moves on a screen attempt and on `POST /v1/watch`. `last_success_at` moves only when the gateway validated a new capture. A failed fetch or an invalid PNG sets `last_fetch_ok` false and leaves `last_success_at` where it was. The same pixels still increment `observation_generation` and do not move `content_changed_at`.

`POST /v1/watch` is a heartbeat. It sets `process_heartbeat_at` and does not change `last_success_at` or clear `last_fetch_ok`. An error stays an error. Age since `last_success_at` of at least S is stale. S is `WATCHER_STALE_SECONDS`, default 10, and a non-positive or non-finite value falls back to 10. A paused watcher is `paused`, including when the last capture succeeded. A registration with no capture yet is `unknown`.

A 304 is a successful observation only because `/v1/screen` decodes the new capture before comparing the ETag. `last_published_at` stays null. This gateway does not publish the external watcher's files, and a capture here is not proof that a client published one.

The dashboard still lists watcher names as contact. `observations` is separate. Diagnostics add `Observation: <holder> <label>` and leave the existing mode line intact. The UI shows the observation beside the contact names.

Poll interval P=3 and request deadline T=5 are the external watcher's acceptance settings. No watcher process is in this repo, so those numbers were not copied onto the gateway's own hands timeout.

Commands:
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_r13_watcher_freshness.py tests/test_r11_resting_watch.py tests/test_relay_fix_acceptance.py::test_p08_failed_fetch_preserves_last_success_but_marks_error`: 35 passed.
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q --ignore=tests/test_relay_fix_acceptance.py`: 386 passed, 1 skipped.
- `RELAY_REPO_ROOT=$PWD /home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_relay_fix_acceptance.py`: 21 passed.

## B18 — not started

The framework names `watch-live-laptop.py` and an Abraham watcher variant. Neither file is in this repo. Their functions were not invented, and W01–W08 were not run. Watcher 38 is not closed. F7-C, the packaged Windows capture at 100/125/150 percent, is also still open.


## B18 inputs

Fetched from `v3-r13-spec` commit `5bf44ec209239662639f63cda9a3d3be205de6a2`. They are inputs under `docs/r13-inputs/watchers/`, not the candidate implementation.

| File | SHA-256 | Bytes |
| --- | --- | --- |
| `watch-live.py` (Abraham, Christopher's 2026-10-19 design) | `8d4511e54bd0ca5d5952b82f9916280087ae52d8c034f38e88fb658c9941eeaf` | 2327 |
| `watch-live-laptop.py` (ECU laptop, LAPTOP-UAPTFJ1J) | `975904f89bf98ba350de315863617c1f64efbf76ec78b11adf335132a22e58cc` | 2492 |

Symbols that are actually in both inputs:

- `fetch` — curl through `http://hatch-egress-proxy:3130`, 10s timeout, PNG magic, exceptions swallowed.
- The poll loop is not a function. It decodes with `Image.open`, writes `FULL + ".tmp"`, `LIVE + ".tmp"`, and `EPOCH + ".tmp"`, and renames those three names separately.
- `LOG` is appended only after a successful decode. A failed fetch writes nothing.

Symbols that are not in either input: an OS lock, a heartbeat, a failure record, and a consumer. The Abraham comment mentions `watch-live.pid` written by a launcher that was not supplied. That is not a lock. New repair names are in `watchers/` and are not claimed to be the input.

## B18 lock

`watchers/lock.py` `OutputLock` holds `fcntl.flock` on Linux (or `msvcrt.locking` on Windows) for the life of the process and never deletes the lock file. Abraham locks `screen-live.lock`. The laptop locks `screen-laptop-live.lock`. A second process prints `another watcher holds the output lock` and exits 1. Kill plus relaunch takes the same inode.

`watchers/watch-live.py` and `watchers/watch-live-laptop.py` are the candidate entries. Defaults keep each input's URL, home, and file names. `WATCH_HOME` is how the tests avoid `~/workspace`.

Command:
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_r13_watch_lock.py`: 2 passed (W01, both variants).


## B18 publication

`watchers/publish.py` writes `full.png`, `display.png`, and `meta.json` into `generations/<id>/` under unique temporary names, then renames one `manifest.json`. The pointer moves only forward, under the counter lock, so a slower publisher cannot aim it at a generation the winner already deleted. Cleanup removes older generation directories and never the one the manifest names. The input's three shared `.tmp` names are not used. The legacy `screen-live.png`, full image, and epoch file are rewritten only after the manifest, as a mirror. Readers that need one snapshot use `read_published`.

A crash after `full`, `display`, or `meta` leaves the previous manifest. A crash after the manifest rename exposes the new generation with matching hashes. `WATCH_DISABLE_LOCK=1` is the fault-injection build for W02, not the normal entry.

Commands:
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_r13_watch_lock.py tests/test_r13_watch_publish.py`: 14 passed. W01 still passes. W02 and W03 pass on both variants.


## B18 telemetry

`watchers/telemetry.py` keeps attempt, success, publication, heartbeat, and content-change times apart. `poll_once` writes that status before it considers the fetch done. A heartbeat, including `POST /v1/watch`, updates `process_heartbeat_at` only. Fetch failures (`http_502`, `timeout`, `invalid_png`, `disk_full`) increment `consecutive_failures` and do not move `last_success_at` or `last_published_at`. Eleven identical frames over 30 simulated seconds move the observation generation and leave `content_changed_at` on the first frame. The log line stays `changed=0`. After that, a failed fetch is an error immediately, and it is also stale once the last success is 10 seconds old. P=3, T=5, and S=10 are the status values. The input's curl timeout was 10; the repaired deadline is T=5.

The gateway integration uses B17. A validated `/v1/screen?watcher=abraham` sets the gateway success time. A later pause heartbeats `/v1/watch` and does not move that time. The local status is `paused`, not fresh.

Command:
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_r13_watch_telemetry.py`: 5 passed. W04 and W05 on both variants, plus the B17 heartbeat check.


## B18 consumer

`watchers/consumer.py` reads `manifest.json` and the status file together. Two consumers see the same full and display bytes. A clock step backward, or a manifest generation below one this consumer already accepted, is `unknown`. Age of 10 seconds with no newer success is `stale`. A new consumer after a restart still accepts a current publication.

W07 launches `watchers/watch-live.py` and `watchers/watch-live-laptop.py`. The pause file for that variant makes the next poll `paused`. Removing it resumes a fresh capture. That file is new: the inputs have no pause symbol. The supplied launch is the script itself; no separate supervisor was in the input.

W08's local rule refuses a fresh screenshot when the configured application log has not grown. Two polls three seconds apart, with the log advanced between them, are accepted. This is not the supervised workflow. TunerStudio was not running. Watcher 38 is not closed.

Command:
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_r13_watch_consumer.py tests/test_r13_watch_lock.py`: 8 passed.

## Wrap-up

Candidate `v3-r13-candidate`.

- B18 lock `60e7812`
- B18 publication `06d49ce`
- B18 telemetry `ad9b07a`
- B18 consumer: this commit

Commands after the consumer tests:
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q --ignore=tests/test_relay_fix_acceptance.py`: 411 passed, 1 skipped.
- `RELAY_REPO_ROOT=$PWD /home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_relay_fix_acceptance.py`: 21 passed.

Still open, not claimed:
- Watcher 38. W01–W07 passed here on both supplied variants. W08's supervised application-log workflow did not run.
- F7-C packaged Windows capture at 100/125/150 percent. No executable hash, topology, or DPI measurement.
- The Windows `msvcrt` lock branch was not executed. These tests used Linux `fcntl.flock`.
- `hands/test_capture_realism.py` still has the untouched failing check `jitter=false: single legacy write, no per-char sleeps`. That script is outside pytest.


## Owner tuning — resting radius

Authority: Christopher, 2026-10-22 20:50 CDT, via the standing operator. `REST_RADIUS_PX` in `hands/motion.py:304` is 2.0, was 4.0. `REST_STEP_PX`, `REST_RAMP_S`, and the glide constants are unchanged. The existing clamp and pull still scale off the radius. Build only; live R12 was not touched.

The operator ruling the same evening authorized the three checks that hardcoded the old bound. Each now reads `REST_RADIUS_PX`, so the allowed box is ±2 instead of ±4:

- `tests/test_r11_resting_watch.py:141` — the claim-held float used `abs(...) <= 4` on both axes. It now uses `hands.motion.REST_RADIUS_PX`.
- `tests/test_r12_rest_float.py:185-194` — `test_glide_never_leaves_the_radius_disc` sampled `uniform(-4, 4)`, skipped `hypot > 4.0`, and asserted `abs <= 4`. Sample range, skip threshold, and the disc assertion all use `motion.REST_RADIUS_PX`.
- `tests/test_r12_rest_float.py:304` — the episode float used `abs(...) <= 4` on both axes. It now uses `hands.motion.REST_RADIUS_PX`.

No other assertion was edited.

Commands:
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_r11_resting_watch.py tests/test_r12_rest_float.py`: 40 passed.
- `/home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q --ignore=tests/test_relay_fix_acceptance.py`: 411 passed, 1 skipped.
- `RELAY_REPO_ROOT=$PWD /home/openclaw/.venvs/HandsRelay-r13/bin/python -m pytest -q tests/test_relay_fix_acceptance.py`: 21 passed.
