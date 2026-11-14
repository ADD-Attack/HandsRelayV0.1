# HandsRelay v4 roadmap closeout

This file replaces the original implementation prompt, which described an early v2-era tree and is not an accurate description of the current repository. The product line is HandsRelay v4. The GitHub repository still has the historical `HandsRelayV0.1` slug; renaming that remote is an owner-controlled action and is listed at the end of this closeout.

## Implementation work packages

| Package | Disposition | Evidence or reason |
| --- | --- | --- |
| WP0 — Windows end-to-end smoke script | Declined | A smoke script that sends desktop input cannot promise a harmless action on an unknown foreground window. Health, info, screenshot, and the live checklist are documented in `docs/VERIFICATION.md`; the owner can run the action check with a known target. No script silently injects input. |
| WP0b — takeover interlock | Implemented; live-only confirmation remains | Hands reject acts on owner activity, gateway preserves the yield response, and `/v1/info` exposes PC/probe state. Automated coverage is in the test runner; physical-input verification remains in `docs/VERIFICATION.md`. |
| WP1 — UI Automation control tree | Declined | The supported contract is pixel capture plus window inventory/focus and parser labels. Adding a second, privileged OS-control tree would broaden the desktop-control surface and duplicate current targeting capabilities. |
| WP2 — custom-tools registry | Declined | Arbitrary registered tool execution is outside this relay's fixed, typed API and would create a second command allowlist and audit surface. Existing named operations remain individually validated. |
| WP3 — batched actions | Declined | A batch can continue issuing input after the owner changes windows or takes control. Per-act receipts, owner-yield handling, and foreground pinning provide safer recovery without a multi-action execution primitive. |
| WP4 — trajectory recording | Declined | Retaining action histories risks storing typed text and other sensitive desktop context. The existing receipt format keeps parameter names only; full trajectory persistence is intentionally not added. |
| WP5 — browser element adapter | Declined | Browser DOM control requires a browser driver and domain-policy authority not present in this Windows desktop relay. It remains a separate integration rather than a core dependency. |
| WP6 — nested desktop / host-app split spike | Executable prototype declined; assessment supplied | Provisioning a second desktop/agent runtime is outside the no-live-changes boundary; the supplied document is an architecture assessment, not an executed feasibility prototype. See [`SPIKE-NESTED-DESKTOP.md`](SPIKE-NESTED-DESKTOP.md). Recommendation: do not add either mode to this product without a separate security and operator decision. |

## Product identity and remote rename

The source, executable, UI, service labels, and current setup documentation identify the product as **HandsRelay v4**. Existing per-user credentials remain at `%USERPROFILE%\\.HandsRelayV0.1\\` so an upgrade continues to find the gateway token and hands secret; this is a documented compatibility storage path, not the product version. Legacy `start-v2.bat` and `hands_relay_v2_app.py` entry points forward to the v4 implementation.

The GitHub slug and description are remote account metadata. Proposed reviewable change: rename `ADD-Attack/HandsRelayV0.1` to `ADD-Attack/HandsRelay-v4` and set its description to: `HandsRelay v4 — authenticated Windows desktop relay with a gateway, hands service, and optional screen parser.` This commission does not rename the remote; the owner must approve that account-level change.

## Verification boundary

Run `python tools/run_all_tests.py` from the repository root. CI is defined in `.github/workflows/tests.yml`. The Windows desktop, UAC, lock-state, multi-monitor, physical takeover, and real OmniParser-weight checks are not reproducible in this Linux work environment; exact steps and limits are recorded in [`VERIFICATION.md`](VERIFICATION.md). No live relay was deployed, restarted, or reconfigured for this commission.

The authoritative item-by-item commission disposition table is [COMMISSION-CLOSEOUT.md](COMMISSION-CLOSEOUT.md).
