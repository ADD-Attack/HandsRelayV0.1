# HandsRelay v4 finish commission — 2026-11-13

Repository-only candidate for ticket `20261113-producer-22`. No live relay deployment, restart, reconfiguration, account metadata update, or external publication was performed. This is not a claim of a verified Windows release.

## Review fixes

| ID | Disposition | Evidence |
| --- | --- | --- |
| F1 | Built locally; remote metadata flagged | v4 app/module, executable, UI, service labels, build script and launcher. Old module and launcher forward to v4. `.HandsRelayV0.1` is explicitly retained for compatibility; regression tests reuse both existing credential files. Remote slug/description proposal is in ROADMAP; account changes require owner action. |
| F2 | Built | README native quickstart uses two separate terminals, each starting at the repository root, with dependencies and environment settings. |
| F3 | Built | “hands” is plural in prose; “hands service” is singular. |
| F4 | Built | Current user-facing documentation uses en-US; historical quoted records are marked as history. |
| F5 | Built | Plain “Actuation” heading and reader-facing roadmap; historical stage labels are no longer current setup instructions. |
| F6 | Built | File drop, wait conditions, PC lock/sleep and find-text have separate headings. |
| F7 | Built | Standalone setup is centralized in hands/README; root README links to it. Preferred private transport and optional public fallback are explained once per entry document. |

## Full wishlist dispositions

“Built” includes functionality already present at the baseline; it does not mean newly invented or live-verified in this commission. Test references below are suites executed by `tools/run_all_tests.py`. Full historical requests remain in WISHLIST-FULL; this table governs their current status, including the extract's duplicate entries.

| Item | Disposition | Evidence or one-line reason |
| --- | --- | --- |
| 1 Persistent gateway token | Built | `test_gateway_token.py`; finish regression reuses the legacy file. |
| 2 File drop | Built | `hands/test_filedrop_state.py`; allowlist, chunks, checksum and atomic commit. |
| 3 Window inventory/focus | Built | `hands/test_actuation.py`, `tests/test_review_hands.py`. |
| 4 Drag | Built | `hands/test_actuation.py`, `tests/test_r9_motion.py`. |
| 5 Wait conditions | Built | `hands/test_process_wait.py`, `services/gateway/test_services.py`. |
| 6 Clipboard | Built | `tests/test_r72_fixround.py`; real clipboard is a gated Windows check. |
| 7 Autostart/self-heal | Built | `tests/test_review_app.py`; finish regression tests legacy Run-key migration on toggle. |
| 8 Elevation bridge | Built | `hands/test_elevation.py`, `services/gateway/test_find_elevate.py`; actual UAC approval remains manual. |
| 9 Fast typing/timeout | Built | `hands/test_capture_realism.py`, `services/gateway/test_act_ops.py`. |
| 10 Monitor capture | Built | `hands/test_capture_realism.py`, `services/gateway/test_screen_params.py`. |
| 11 Native-resolution region capture | Built | Same capture and screen-parameter suites. |
| 12 Find text | Built | `services/parser/test_find.py`; real weights remain manual. |
| 13 Lock/sleep state | Built | `tests/test_r9_display_state.py`; physical lock/sleep remains manual. |
| 14 Conditional hash | Built | `services/gateway/test_screen_hash.py`. |
| 15 Claim/queue | Built | `services/gateway/test_coordination.py`, `tests/test_review_gateway.py`. |
| 16 Keystroke timing | Built | `hands/test_capture_realism.py`. |
| 17 Mouse dynamics | Built | `tests/test_r9_motion.py`. |
| 18 Yielded-act accounting | Built | `services/gateway/test_coordination.py`, `tests/test_review_gateway.py`. |
| 19 Foreground interrupt | Built | `hands/test_actuation.py`; real terminal interruption remains manual. |
| 20 Held-act foreground check | Built | `tests/test_r9_sweep_acts.py`. |
| 21 Receipt for every queued submission | Built | `tests/test_r9_sweep_acts.py`. |
| 22 Response integrity | Built | `tests/test_r9_sweep_acts.py`; remote proxy behavior remains unverified. |
| 23 Web dashboard | Built | Existing local-web/native-shell GUI; `tests/test_r9_gui.py`. |
| 24 Human-like click/drag paths | Built | `tests/test_r9_motion.py`; subjective feel is not asserted by unit tests. |
| 25 Separate model-specific GUI redesign | Declined | Retain the already-built web dashboard and deliver targeted visual/state polish; a separate model-specific redesign has no additional acceptance design and is not represented as completed. |
| 26 Windows clipboard crash | Built | `tests/test_r72_fixround.py`; real clipboard check is Windows-gated. |
| 27 Type self-abort/queued foreground scatter | Built | `tests/test_r72_fixround.py`, `tests/test_r9_sweep_acts.py`. |
| 28 Decaying idle motion | Built | `tests/test_r9_motion.py`; optional, off by default. |
| 29 Motion reference | Built through 24/28 | `docs/R9-MOTION-DESIGN.md`, `tests/test_r9_motion.py`. |
| 30 Behavioral motion layer | Built through 24/28 | Same design map and motion suite. |
| 31 Pause external polling on takeover | Declined | This repository contains no external agent polling loop to suspend/resume; relay claim/yield remains available to that client. |
| 32 Visible poll tracker | Built; paused/stopped distinction declined | Dashboard shows watcher heartbeat age; no protocol pause signal exists, so silence is not mislabeled as pause. |
| 33 Paste guard | Built | Before/after owner checks, lock checks, foreground recheck before Ctrl+V, explicit method/guard/count; finish regressions cover before/after takeover and foreground change. A sent paste cannot be retracted. |
| 34 Live claim countdown | Built | Existing one-second dashboard refresh computes expiry; `tests/test_r11_resting_watch.py`, `tests/test_r9_gui.py`. |
| 35 Owner-abort label | Built | Terminal `aborted-owner` receipt and amber “owner took over” GUI label; finish regression covers direct and held settlement without replay. |
| 36 Continuous resting float | Built, live quality unverified | 120 ms interpolated move, duration checked in `tests/test_r11_resting_watch.py`; actual desktop feel remains manual. |
| 37 Product rename | Built locally; live/remote changes declined here | v4 current source and launchers; legacy paths retained intentionally. Live folders, shares and shortcuts are excluded by the accepted no-deployment boundary; remote metadata/publishing requires owner action. |

## Roadmap dispositions

WP0 smoke injection: declined; WP0b interlock: built; WP1 UI Automation: declined; WP2 arbitrary tools: declined; WP3 batches: declined; WP4 trajectory storage: declined; WP5 DOM adapter: declined; WP6 executable nested-desktop prototype: declined (architecture assessment supplied instead). Each reason is in ROADMAP. No roadmap request is silently left pending.

## Tests and release status

- Initial baseline of the inherited working tree: 35/35 suite processes successful; 3 live-gated tests skipped across two suites.
- Added commission regressions: 11 tests passed on Linux/Python 3.12 (paste provenance, foreground refusal, terminal receipts/no replay, heartbeat ages, import alias, autostart migration, both credential files).
- Final full-run result: **36/36 suite processes successful, 0 failed; 3 live-gated tests skipped across two suite processes.** Of these, 16 are script-style suites; the pytest suites report 285 passed and 3 skipped. Command: `python tools/run_all_tests.py` using an isolated environment with Flask, waitress, requests, Pillow and pytest.
- CI definition runs that command on Linux Python 3.11/3.12/3.13 and Windows Python 3.12. Windows failures are no longer masked by `continue-on-error` (the baseline Windows job was already green at merge).
- **Remote CI for this candidate is unverified.** No push or PR was published under this commission's owner-stop boundary. Earlier merge CI is not evidence for this source candidate.
- Windows executable build, live desktop/UAC/physical takeover/multi-monitor/clipboard/soak and real model weights remain explicitly live-unverified in VERIFICATION.md.

Version-reference accounting is in [VERSION-SWEEP.md](VERSION-SWEEP.md); remaining old names are compatibility or explicitly historical references.

## Polish

One v4 identity in current product surfaces, en-US reader-facing prose, organized API sections, separate-terminal setup, watcher ages, and a non-failure owner-takeover receipt treatment. UI rendering changes are covered by source/model tests but have not been visually accepted on the Windows target.
