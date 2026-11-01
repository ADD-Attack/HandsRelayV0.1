# HandsRelay v2 — execution brief for Claude Opus 5.5

_You are Claude Opus 5.5, working in the repo `ADD-Attack/HandsRelayV0.1`.
Your job: complete the work packages below, in order, testing as you go,
and commit your work. This file is your spec — if something here conflicts
with the code, trust the code for current state and this file for intent,
and note the conflict in your handoff report._

_Intended run context: an Opus 5.5 coding session (for example a
Muse / OpenRouter-backed setup). API keys and tokens are supplied by
the operator's environment — never write one into this repo._

## Non-negotiables

- **Docker is optional, never required.** Both services are plain Python
  and must keep running natively on Windows. The compose file is an
  alternative deployment path, not the design.
- **The hands never run in Docker.** They need the interactive Windows
  desktop session. Do not containerize them, and do not design around a VM.
- **No secrets or personal data in the repo.** No tokens, session secrets,
  live tunnel URLs, IP addresses, email addresses, or home addresses —
  in code, docs, tests, logs, or commit messages. Refer to "the owner" /
  "the operator", never a person's name, outside the LICENSE file.
- **Do not weaken the security model.** Gateway: token auth with
  constant-time comparison and failed-guess lockout. Hands: a fresh
  session secret per launch. Nothing listens publicly by default;
  Tailscale is the preferred transport. A public tunnel is a fallback,
  never the design.
- **Do not break existing contracts casually.** The hands API
  (screenshot / move / click / type / key / scroll, `X-Secret` auth) and
  the parser set-of-marks contract (numbered elements with bbox + center)
  are relied on by the gateway. Extend them; if you must change one,
  update gateway + docs + tests in the same commit.
- **Never fake verification.** If a step needs the owner's physical PC, a
  GPU, or an account you don't have, implement everything that can be
  implemented, write the exact manual verification step down, and say
  plainly that it is unverified. A claim without a run behind it is a bug.

## Current state (starting point — verify against the code)

- `services/gateway/` — Flask + Waitress. Token auth + lockout;
  `/health`, `/v1/info` (backend reachability), `/v1/screen` (proxies the
  hands screenshot), `/v1/act` (forwards move/click/type/key/scroll),
  `/v1/parse` (forwards to the parser). The gateway reaches the hands via
  the `HANDS_URL` and `HANDS_SECRET` environment values.
- `services/parser/` — Flask + Waitress. Loads OmniParser v2 weights
  (icon detector, optional Florence captioner) from its models directory
  when present (`fetch_weights.py` downloads them; heavy deps are in
  `requirements-model.txt`). With no detector loaded, `/parse` returns
  501 — deliberately, so a driver never mistakes "no model" for "empty
  screen".
- `hands/` — the v1 packaged Windows app carried forward: screenshot,
  mouse, keyboard, per-session generated secret, lockout with phone
  alerts, Tailscale Serve preferred.
- Smoke-tested so far: gateway against a mock hands (auth rejection,
  screenshot proxying, action forwarding, wrong-secret error path) and
  the parser's honest not-loaded state. **Not yet proven:** end-to-end
  against the real hands on the owner's PC, and parser detection with
  real weights on a GPU.

## Work packages (do them in this order)

### WP0 — Windows end-to-end smoke script
Add a script (PowerShell or Python) that starts the parser and gateway
natively, checks `/health` and `/v1/info`, and — when the hands app is
running — pulls one screenshot and performs one harmless action through
the gateway. It prints a clear pass/fail per check and never prints a
secret.
**Done when:** one command on a Windows PC produces the report; when the
hands are not running it says exactly that instead of failing obscurely.

- [x] **Status:** implemented (`tools/smoke.py`, `tests/mock_hands.py`);
  verified against the mock hands. Run on the owner's PC: see
  `docs/VERIFICATION.md` (WP0).

### WP0b — Takeover interlock (the user always wins)
Safety feature, outranks every feature package below. Raised by the
owner on 2026-10-13 after agent keystrokes landed in the wrong window
while he was using the PC himself: when the person at the keyboard is
active, the agent must stop driving, immediately and automatically.

- The hands watch for physical input that did **not** come from the
  agent: the mouse ending up somewhere the agent's own act calls did
  not put it, or a local key / mouse-button press. On detection the
  hands enter a YIELDED state.
- While yielded, the act endpoints refuse with HTTP 409 and a body like
  `{"error": "user active — control yielded", "ok": false}`, and the
  gateway passes that refusal through unchanged, so a well-behaved
  driver backs off instead of fighting the user for the cursor.
- Control returns after a quiet period with no local input
  (configurable, default ~10 seconds), with no human action required.
- The agent's own input must never trip it: track the cursor positions
  the agent itself commanded and only yield on deviation; design the
  keyboard side so agent-sent keys don't self-trigger (a brief
  suppression window around act calls is acceptable — document the
  tradeoff).
- The gateway's `/v1/info` reports the current state
  (`"control": "agent"` or `"control": "yielded"`), and the hands show
  the state visibly (window/console status line) so the person at the
  PC can see who has control.

**Done when:** with the stack running, moving the physical mouse makes
the next act call return 409 and `/v1/info` show yielded; after the
quiet period acts succeed again; 20 consecutive agent-issued moves
never trip the interlock; and the behavior is documented in the README.

- [x] **Status:** implemented (hands `Takeover`, `/status`, 409 gate;
  gateway `control` in `/v1/info` + 409 pass-through; app status line;
  `tools/takeover_check.py`); unit + HTTP tests pass on Linux with stubbed
  input. **Unverified on Windows** (hooks, real cursor) — see
  `docs/VERIFICATION.md` (WP0b).

### WP1 — UIA endpoint in the hands
Add UI Automation to the hands (pywinauto): an endpoint returning the
control tree of the foreground window / desktop (name, control type,
bounding rect, center, automation id, enabled, off-screen), and an act
endpoint that clicks / invokes / sets text on an element by id from that
tree. Same session-secret auth as the existing endpoints. Add the
matching gateway route.
**Done when:** the tree for Notepad and File Explorer comes back with
usable rects, a named control in a test window is activated through UIA
(not pixels), and the pixel endpoints are unregressed.

### WP2 — Custom-tools registry in the gateway
A small registry where named tools can be registered and invoked through
the gateway (auth'd, allowlisted by name, every call logged). Ship two
or three safe tools first — for example launching an allowlisted app,
and reading window titles. Office/COM automation may be added behind an
optional import; it must not become a hard dependency.
**Done when:** tools are invokable through the gateway, unregistered
names are rejected, calls appear in the log, and unit tests with mocks
cover registration + rejection.

### WP3 — Batched actions (speculative multi-action support)
Extend `/v1/act` with a batch form: an ordered list of actions executed
in sequence, stopping at the first failure, returning a per-step result,
with an optional screenshot after the final step. Document the contract
in the README. (Motivation: planners can emit 3–5 steps per model call
instead of one round-trip per click.)
**Done when:** a batch runs in order, a failing step halts the batch and
is identified in the response, and the single-action form is unchanged.

### WP4 — Audit log + trajectory recording
The gateway records each session's actions to a local JSONL trajectory:
timestamp, op, target summary, result, and a screenshot hash (not full
screenshots by default). The storage directory is configurable, secrets
never appear in the log, and the format is documented so a failed run
can be replayed and debugged.
**Done when:** a smoke run produces a complete trajectory file, a secret
scan of the file comes back clean, and the README documents where logs
live and how they are rotated or deleted.

### WP5 — Browser element-index adapter (interface only)
Design and stub an adapter interface for browser surfaces in the
browser-use style: numbered DOM elements the planner can act on by
index, with a domain allowlist enforced by the adapter. Do not pull a
browser driver into the gateway's core dependencies; a real integration
belongs behind an optional extra.
**Done when:** the interface, the allowlist rule, and a fake-adapter
test exist, and the README marks real browser integration as optional
and not yet wired.

### WP5b — "Just works" network access on first run
Field failure (2026-10-15, owner's machine): the packaged app started
cleanly — all three services green, loopback self-check HTTP 200 — yet
the gateway was unreachable from the tailnet. Windows Defender
Firewall's Private profile (which the Tailscale adapter uses) silently
blocks inbound connections to any app not on its allowed list, and the
normal "allow this app?" prompt never produced a working rule for the
packaged build: PyInstaller serves from a temp extraction path (rules
created against it don't stick to the real exe), the prompt disappears
if the app closes while it is pending, and an allow granted to one
build's path does not cover the next build's path. The owner had to
create an inbound port rule by hand to get in. A tool whose first-run
experience is manual firewall surgery does not "just work."

On Start, the app must ensure its gateway port (5010) is reachable:
1. After the gateway reports healthy on loopback, probe for the inbound
   firewall rule the app owns (a named rule, e.g. "HandsRelay v2 —
   gateway 5010", TCP inbound, port-scoped, Private profile). If it is
   missing, offer its creation in the UI: one button that requests
   elevation (UAC) and creates it with `New-NetFirewallRule` (or
   `netsh advfirewall firewall add rule`). Never create it silently
   and never without the owner's click.
2. The rule is port-scoped, never program-path-scoped, so rebuilds and
   moving the exe never invalidate it. It is idempotent (re-running
   Start never duplicates it) and it is removed by an explicit
   "Remove firewall rule" control in the same window — not on Stop or
   exit, which may be temporary.
2b. The same check must also scan for enabled inbound BLOCK rules
   whose program/name matches the relay executable (e.g. "HandsRelay
   v2.exe"): Windows creates these silently when a firewall prompt
   for the app is dismissed, a block rule overrides any allow rule,
   and — the actual 2026-10-15 root cause, found only after the port
   rule was in place — two such rules made the gateway unreachable
   from every peer while loopback and self-checks stayed green. If
   found, the UI must name them and offer one-click removal under the
   same elevation flow.
3. The app must never claim the gateway is reachable when it has only
   verified loopback: the status line distinguishes "running
   (loopback OK)" from "reachable from the network," and when the
   rule is absent the UI says so in plain words, with the one-click fix
   next to it.
4. Do NOT solve this by disabling the firewall, switching the
   adapter's network profile, or shelling out to blanket
   profile-off commands — the sanctioned mechanism is the single
   named, port-scoped, removable rule. Hands (5000) and parser (5011)
   stay loopback-only and out of scope.
**Done when:** on a Windows machine whose Private profile has no
allow rule for the app, pressing Start surfaces the offer; accepting
it makes GET /v1/info succeed from another tailnet device with no
further manual steps; declining leaves the app running with the
not-reachable state shown honestly; the removal control deletes the
rule and the status line reflects that on the next poll.

### WP5c — Keyboard chords in hands
Field failure (2026-10-15, owner's machine): the Discord window sat on
a second monitor, unreachable — hands screenshots only see the primary
display, and every documented way to pull a window across monitors
(Win+Shift+Arrow, the Alt+Space → Move menu, Win+P) needs a key
*combination*. The hands `/key` endpoint is `pyautogui.press()` on a
single key, so none of them can be sent. The session was rescued by a
different surface entirely (Discord web in the owner's browser); the
capability gap in hands is real and will recur.
1. Add a chord endpoint to hands: `POST /hotkey {"keys": ["win",
   "shift", "left"]}` implemented with `pyautogui.hotkey(*keys)`.
   Validate key names against pyautogui's key set and reject unknown
   names with a 400; keep single-key `/key` exactly as is.
2. Expose it through the gateway as `/v1/act {"op": "hotkey",
   "keys": [...]}` with the same token auth as every other act call.
3. No new persistent permissions are involved — chords are input,
   same trust level as the existing click/type/key surface.
**Done when:** from a second tailnet device, with a window focused on
a non-primary monitor, `{"op": "hotkey", "keys": ["win", "shift",
"left"]}` moves that window to the primary monitor and a following
`/v1/screen` shows it there.

- [x] **Status:** implemented (hands `POST /hotkey`, gateway op `hotkey`);
  tests pass on Linux with stubbed input. **Unverified on Windows** (real
  chord, second monitor) — see `docs/VERIFICATION.md` (WP5c).

### WP6 — Spike only: nested desktop + host/app agent split
Research-only. Write `docs/SPIKE-NESTED-DESKTOP.md` assessing a
Picture-in-Picture style nested desktop (agent works in a nested
session, owner keeps theirs) and a host/app agent split for this relay:
feasibility on current Windows, security implications, and a clear
recommendation. **Do not implement either** — the spike's acceptance
criteria are what a future decision gets made on.

## Definition of done (every work package)

- Code + tests/smoke coverage for the new behavior; existing smoke
  behavior unregressed (`python -m py_compile` clean on touched files;
  gateway/parser run against the mock hands without errors).
- README and this file updated in the same commit: the package's box
  gets checked, and any new operator step (env value, command, manual
  check) is written down where an operator will actually find it.
- A secret scan of the diff comes back clean (no tokens, secrets,
  URLs-with-secrets, IPs, emails, addresses).

## Commit protocol

- Work on a branch named `opus-5-5-roadmap`; do not commit directly to
  `main`.
- Small commits: one work package per commit at most, split further when
  a package has separable steps. Message format: `WP<n>: <what changed>`.
- Run the relevant tests before each commit. If a test cannot run in
  your environment (needs Windows, a GPU, or the real hands), say so in
  the commit body and add the manual step to `docs/VERIFICATION.md`.
- Never commit: `.env` files, model weights, screenshots containing
  personal data, trajectory logs, build artifacts, or `__pycache__`.
- Push the branch when each work package lands. Do **not** merge to
  `main` yourself — the merge is the owner's call. Your final handoff
  (below) is what he reviews.
- **If you do not have push access to this repo** (permission error on
  push): do not stall, and never ask the owner to share a personal
  token. Fork the repo to your own account, push the same
  `opus-5-5-roadmap` branch to your fork, and open a pull request from
  the fork's branch into this repo's `main`, with the handoff report as
  the PR description. If you cannot open PRs either, push to the fork
  and put the fork URL + branch name at the top of your handoff report
  so the owner can pull it himself. State plainly in the handoff which
  route you used (direct push, fork + PR, or fork only).

## Handoff report (end of run)

Report, in this order:
1. Work packages completed, with the commit hash for each.
2. Tests actually run and their results — and which verifications are
   still manual-only, with the exact steps for the owner to run them.
3. Anything you changed about the contracts in "Non-negotiables", or
   "none".
4. Open risks or follow-ups you noticed but did not fix.

## Rejected alternative (do not resurrect without new evidence)

Assembling off-the-shelf engines instead of building this: UFO² as the
desktop engine pointed at a custom model endpoint + a browser-use CLI +
Relay v1 as the fallback. It was weighed on 2026-10-13 and the build was
chosen. The steal list that seeded the work packages above came from
browser-use, UFO², and OmniParser; the packages are the durable form of
that list.
