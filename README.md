# HandsRelay v2

Let a personal AI agent operate a Windows PC — screenshots,
mouse, keyboard — with a security posture designed for leaving it reachable
while the owner is away from home.

v2 is a rebuild of the single-file HandsRelay around three parts:

| Part | Runs where | What it does |
| --- | --- | --- |
| **hands** | Windows host, native (never in Docker) | The desktop driver: screenshot, mouse, keyboard, window control. A container has no interactive Windows desktop session, so the hands must live on the host — the same constraint every desktop agent (UFO², OmniTool) runs into. |
| **gateway** | Docker or native Python | The agent-facing API: token auth, failed-guess lockout, routing of screen/action calls to the hands and parse calls to the parser. This is the only surface the agent talks to. |
| **parser** | Docker or native Python (GPU optional) | Screen parsing (OmniParser-class): screenshot in → labeled, numbered UI elements out, for surfaces with no DOM and no UI Automation tree (games, canvas, custom UI). Optional component; the relay works without it. |

Roadmap (what we're stealing from browser-use, UFO², and OmniParser, and in
what order): [docs/ROADMAP.md](docs/ROADMAP.md).

## Packaged app (Windows) — the normal way to run it

One icon, one button. Build once:

```
build_app.bat   :: builds "HandsRelay v2.exe"
```

Then just double-click **HandsRelay v2.exe** and press **Start**. The one
window runs all three parts in-process — hands on 127.0.0.1:5000, gateway on
:5010, parser on :5011 — with a status line for each. **Stop** (or closing the
window) stops everything. No other app is needed: the v1 relay is retired,
and the hands run headless inside this app (no separate window, no tunnel —
the gateway is the only surface an agent talks to).

Secrets in the packaged app:

- The **gateway token** persists per machine at
  `%USERPROFILE%\.HandsRelayV0.1\gateway-token.txt` — generated on first
  run, reused after that (delete the file to rotate it), so an agent keeps
  working across relaunches. The window shows it on demand (Show / Copy);
  it is never logged.
- The **hands secret** is owned by the app and persists per machine at
  `%USERPROFILE%\.HandsRelayV0.1\hands-secret.txt` — generated on first run,
  reused after that (delete the file to rotate it). The app feeds the same
  value to the gateway and the hands, so the two can never disagree about
  it the way the old two-app setup could.

## Quickstart (services)

Docker is an option, never a requirement — both services are plain
Python and run natively just as well.

With Docker:

```
cp .env.example .env
docker compose up --build
```

Native (PowerShell, on the Windows PC itself):

```
cd services\parser;  pip install -r requirements.txt;  python app.py
cd services\gateway; pip install -r requirements.txt
$env:RELAY_GATEWAY_TOKEN = "<long random token>"
$env:HANDS_URL    = "http://127.0.0.1:5000"
$env:HANDS_SECRET = "<the hands app's current session secret>"
$env:PARSER_URL   = "http://127.0.0.1:5011"
python app.py
```

- Gateway: http://127.0.0.1:5010/health — agent API under /v1 (screen, screen/hash, monitors, windows, clipboard, act, act/queued, hands/claim, hands/release, parse, find, elevate, info, filedrop, wait)
- Parser: http://127.0.0.1:5011/health — reports whether the model is loaded

**Change detection without the pixels:** `GET /v1/screen/hash` returns a
SHA-256 of the current frame's pixels with the bottom `mask_bottom` px
(default 80 — the taskbar) excluded, so a watcher can poll a tiny JSON
response and only fetch the full PNG when the hash changes. With
`?stable=1` the gateway samples two frames ~0.65 s apart and hashes
their per-pixel minimum, so a blinking caret or spinner phase cannot
flip the result. `/v1/screen` also carries that hash as an `ETag` and
answers a matching `If-None-Match` with `304`. If the gateway lacks
Pillow, the hash route returns 501 and `/v1/screen` serves without an
ETag — never a wrong hash.

The parser answers /parse only once OmniParser v2 weights are in its
models directory (`python services/parser/fetch_weights.py`, heavy deps in
`services/parser/requirements-model.txt`). Until then it returns 501 —
deliberately: a driver must never mistake "no model" for "empty screen".
The parser has no auth of its own, so it listens on loopback by default —
standalone and in the packaged app alike; the Docker deployment sets
`PARSER_HOST=0.0.0.0` (in docker-compose.yml) for its internal network,
where the published port is already bound to 127.0.0.1 on the host.

## Coordination: hands claim + queued act

Two gateway features keep multiple agent sessions (and the owner) from
fighting over one desktop. Both are covered by the gateway token, like
every other /v1 route.

**Hands claim (advisory).** A session that is about to drive takes a
claim first so other sessions can see who holds the desktop:

- `POST /v1/hands/claim` `{"holder", "purpose", "ttl_seconds"?, "queue"?}` —
  grants when free (`200 {"granted": true, "claim": {...}}`); the same
  holder re-claiming refreshes the lease. TTL defaults to 900 s, clamped
  30–3600 s; expiry is lazy (an expired claim is simply absent). If
  another holder holds it: `409` with the current claim — or, with
  `"queue": true`, a FIFO queue slot (`200 {"granted": false, "queued":
  true, "position": N}`).
- When a claim is released or expires, the queue head gets a 30 s
  reservation: only that holder's claim can be granted during the grace
  window; everyone else still gets `409` / a queue slot. A head that does
  not claim within its window **forfeits its queue slot** (it can queue
  again at the back).
- The queue holds at most 32 waiters (`409` with `"queue_full": true`
  beyond that). Re-POSTing `{"queue": true}` keeps a waiter's position and
  refreshes its entry; entries not refreshed for 60 minutes are dropped.
  Holder names are capped at 128 characters, purposes at 500.
- `POST /v1/hands/release` `{"holder"}` — only the current holder can
  release (`403` otherwise; releasing an expired claim is an ok no-op).
  `{"holder", "cancel_queue": true}` instead drops that holder's queue
  entry.
- `GET /v1/hands/claim` — `{"claim": {...}|null, "queue": [...],
  "reserved_for": holder|null}`. `GET /v1/info` carries the same claim
  object as `hands_claim`.

The claim is **advisory coordination only — it does not gate
`/v1/act`**. A session that skips claiming can still act; well-behaved
sessions check the claim first and back off politely.

**Queued act (yielded-input accounting).** When the owner is actively
using the desktop, the hands answer an act with `409` (the act yielded).
Plain `POST /v1/act` surfaces that as-is and is unchanged.
`POST /v1/act/queued` takes the same body plus optional `hold_seconds`
(default 30, clamped 1–120):

- Any result other than a yield-`409` is returned exactly as `/v1/act`
  would return it, plus the `id` of a receipt filed for it (every
  submission files one, so a lost response can still be reconciled).
- A yield-`409` (`error_code` `"yield"` / `"locked"`; older hands: error
  mentions "yield" / "user active") files a receipt and answers
  `202 {"queued": true, "id": ..., "state": "held"}`. A retry thread
  re-forwards held acts in submission order, about once a second, until
  their deadlines: a non-`409` answer settles the receipt `applied`,
  still-`409` at the deadline settles it `refused-yielded`, and an
  unreachable hands settles it `failed`. **A held act is never forwarded
  once its deadline has passed** — if an earlier act (say a long `type`)
  kept the retry thread busy past it, it settles `refused-yielded` instead
  of landing late.
- `GET /v1/act/queued/<id>` returns the receipt; an unknown or evicted
  id answers `404` with `"state": "expired"`. `GET /v1/act/queued`
  lists receipt summaries (`id`, `op`, `state`, `submitted_at`), newest
  first. At most 200 receipts are kept.
- A receipt records the op and the payload's **key names only — never
  payload values** (typed text may be sensitive). The payload itself is
  held in memory only while the receipt is held and dropped when it
  settles.

**File-drop (gateway):** `POST /v1/filedrop` with
`{"folder", "filename", "data_b64", "sha256"?}` drops one file onto the
PC in a single call. The gateway computes the SHA-256 itself when it is
not supplied, verifies it against a supplied one before sending anything
(a mismatch is a 400), then runs the hands' chunked upload protocol
(256 KB raw chunks) and returns `{"ok", "path", "size", "sha256"}`.
Any hands error is surfaced with its status. On the hands side the
protocol is `POST /filedrop/begin` (returns an `upload_id` and
`chunk_max` 524288), `POST /filedrop/chunk` (strictly sequential
offsets; a wrong offset is a 409 carrying the expected offset),
`POST /filedrop/commit` (verifies size and SHA-256, then writes via a
temp file + atomic replace in the target folder), and
`POST /filedrop/abort`. Uploads are capped at 16 MB and expire after
10 minutes (any authenticated hands request purges expired ones). At
most 4 uploads / 64 MB of declared size may be in flight at once; `begin`
beyond that answers 503. Drops only land in allowlisted folders: the hands config
file key `"filedrop_dirs"` (a list of absolute paths), or the
`HANDS_FILEDROP_DIRS` environment variable (`os.pathsep`-separated,
which wins when set). With no allowlist configured, every filedrop
endpoint returns 501. Filenames must be bare names — no path separators
or `..`.

**Wait-for (gateway):** `POST /v1/wait` with
`{"condition": {...}, "timeout_seconds": 30, "poll_seconds": 1}` polls
until a condition holds or the deadline passes, returning
`{"ok": true, "held": true, "waited_seconds": ..., "condition": ...}`
or, on deadline, `{"ok": true, "held": false, "timeout": true, ...}`.
`timeout_seconds` clamps to 1–300 and `poll_seconds` to 0.2–10; an
unknown condition type is a 400 immediately. Condition types:
`{"type": "screen_hash_change", "stable": bool?}` (the masked frame
hash — see `/v1/screen/hash` — differs from the starting sample; 501 if
the gateway lacks Pillow), `{"type": "screen_region_change",
"region": [x, y, w, h]}` (only that region's pixels, clamped to the
frame, change), `{"type": "window_title", "contains": "..."}` (a
window title contains the text, via the hands' `GET /window_titles`;
if the hands answers 404/501 the wait returns
`{"ok": false, "error": "hands does not support window titles"}`
immediately instead of burning the timeout), and
`{"type": "parser_model_loaded"}` (the parser's `/health` reports
`model_loaded` true — the parser loads its model in the background at
startup and retries a failed load every 60 s, `PARSER_RETRY_SECONDS`, so
weights fetched while it runs are picked up without a restart).

**PC lock/sleep state:** the hands' `GET /state` returns
`{"ok": true, "platform": "windows"|"other", "locked": true|false|null,
"idle_seconds": number|null}` — on Windows the lock state comes from
the input desktop (`Winlogon` means locked) and idle time from the
last-input timestamp; off Windows, or on any probe failure, the values
are `null` (never a 500). The hands also expose `GET /window_titles`
(`{"ok": true, "titles": [...]}`). The gateway's `GET /v1/info` now
carries a `"pc_state"` field with the hands' `/state` payload, fetched
inside the same 5-second-cached reachability probe as the other info
fields; it is `null` when the hands is unreachable or `/state` errors,
and all existing `/v1/info` fields are unchanged.
## Actuation (v3 P4 — wishlist #3, #4, #6, #9, #19)

New hands routes, all behind the same X-Secret gate as the existing
routes (and, through the gateway, the same X-Relay-Token gate):

| Hands route | Gateway surface | What it does |
| --- | --- | --- |
| `POST /hotkey {keys: ["ctrl", "l"]}` | act op `hotkey` | Press a true key chord via `pyautogui.hotkey()`. |
| `POST /break {times?: 1..3}` | act op `break` | Ctrl+C chord(s), 0.15 s apart. |
| `POST /drag {x1, y1, x2, y2, button?, duration?}` | act op `drag` | Mouse-down at the start point, move, mouse-up at the end point. |
| `GET /windows` | `GET /v1/windows` | Open windows: title, process, pid, bounds, monitor index, visible, focused. Off Windows it returns an empty list with `"supported": false`. |
| `POST /focus {title, exact?}` | act op `focus` | Focus the first window whose title matches (substring, case-insensitive; full-title when `exact` is true); restores a minimised window first. No match → 404. Works without pywin32 (ctypes); pywin32, when installed, only adds process names to `/windows`. |
| `POST /clipboard/set {text}` | act op `clipboard_set` | Set the clipboard (Windows: ctypes `CF_UNICODETEXT`; else pyperclip when importable; otherwise 501). |
| `GET /clipboard/get` | `GET /v1/clipboard` | Read the clipboard (same backends, same 501 when unavailable). |

**Clipboard read is sensitive.** It stays behind the same authentication
as every other hands/gateway route, and *every read writes a line to the
hands log queue* (`clipboard read via relay`), so the owner sees reads
in the app window as they happen.

**Failsafe.** Throwing the pointer into a screen corner (pyautogui's
FAILSAFE) aborts the act in progress; the hands answer
`409 {"error_code": "failsafe"}` and log it to the app window. An agent
must treat it as the owner saying *stop* — it is not a yield, so a queued
act settles `failed` rather than being retried. Malformed act bodies
(`/move`, `/click`, `/key`, `/scroll` included) are a JSON `400`.

**Why `/break` exists:** the older `/key` op calls `pyautogui.press()`
with ONE key name, so a chord string like `"ctrl+c"` sent to `/key` was
never a real chord — nothing was interrupted. `/break` and `/hotkey`
send true chords via `pyautogui.hotkey()`, so the interrupt lands.

**Fast `/type`:** `POST /type` now also accepts
`{method: "auto"|"paste"|"keys"}` (absent = the old behaviour for short
text). In `auto`, text over 120 chars goes through the clipboard when a
backend exists: the current clipboard is saved, replaced with the text,
pasted with Ctrl+V, then restored. Any clipboard failure falls back to
keystrokes rather than failing, and the response reports what happened:
`{"ok": true, "method_used": "paste"|"keys", "chars": n,
"elapsed_seconds": x}`. On the gateway, the act timeout for `type` is
sized to the payload — `max(30, min(600, 15 + len(text)/6))` seconds,
since the keystroke path runs at worst ~6 chars/s — while every other
op keeps the flat 30 s.
## Capture variants (hands + gateway)

`GET /v1/monitors` lists the monitors as rectangles in virtual-desktop
coordinates (`{x, y, w, h, primary}` — origins can be negative for a
monitor left of or above the primary). `GET /v1/screen` and
`GET /v1/screen/hash` accept two mutually exclusive selectors,
forwarded to the hands:

- `?monitor=N` — capture monitor N (0-based) only. Out of range is a
  400 from the hands naming how many monitors exist.
- `?region=x,y,w,h` — capture that rectangle in virtual-desktop
  coordinates (a region may span monitors, including negative origins).
  Malformed regions are a 400 from the gateway without the hands being
  called.

With no selector the full primary frame is returned exactly as before,
and the ETag/hash always describe the variant actually requested.
Responses from the hands carry `X-Screen-Width`/`X-Screen-Height` of the
returned image, plus `X-Screen-Monitor` or `X-Screen-Region` when a
selector was used.

## Input realism (hands)

Typing and pointing are human-paced by default, and both behaviors are
planned by pure functions (`_plan_key_delays`, `_plan_path`) so the
timing and geometry are unit-tested off-desktop
(`hands/test_capture_realism.py`):

- **Keystroke jitter** (`POST /type`, default on): each character is
  written individually with a delay after it — base uniform
  0.04–0.16 s, plus 0.05–0.20 s after a space and 0.10–0.30 s after
  sentence-ending punctuation. If a plan would exceed
  `max(5 s, 0.16 s/char)` it is scaled down proportionally. The
  response reports `delays_planned` and `planned_seconds`.
  `{jitter: false}` restores the old single fast write.
- **Mouse dynamics** (`POST /move`, `POST /click`, default on): the
  cursor follows a curved quadratic-bezier path with an ease-in-out
  profile, a slight overshoot past the target, and a settle onto the
  exact target, over `clamp(0.25 + distance/2500, 0.3, 1.1)` seconds;
  a human click hovers 0.05–0.15 s before clicking.
  `{human: false}` restores the old direct move.

Live-only verification steps for these features (real multi-monitor
geometry, negative-origin regions, jitter/dynamics feel) are listed in
[docs/VERIFICATION.md](docs/VERIFICATION.md).
**Find text on screen:** `POST /v1/find` (parser: `POST /find`) with
`{"image_b64": "...", "text": "...", "case_sensitive": false}` runs the
same parse pipeline and returns only the elements whose label contains
the text — `{"ok": true, "matches": [...], "count": N, "width": W,
"height": H}`, with each match keeping the full set-of-marks shape
(`id`, `label`, `bbox`, `center`, `interactable`). Empty text is a 400;
with no model loaded, /find returns the same 501 as /parse.

## Packaged app: autostart + self-heal

- **Start on login** (opt-in, off by default): the checkbox under the
  Start/Stop row in the packaged app writes/removes the value
  `HandsRelay v2` under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`,
  pointing at the app (the quoted executable when frozen, quoted Python
  plus the quoted script when run from source). Nothing is written until
  the owner toggles it.
- **Self-heal:** the app's periodic self-check feeds each service's
  liveness into a failure counter. Three consecutive failures restart a
  service the app itself started (at most 3 restarts per service), then
  the app logs that it is giving up and stops restarting it. A service
  that was already running elsewhere is never restarted, and if the
  gateway itself is down the other services' health is treated as
  unknown rather than failed, so one outage does not restart everything.

## Elevation bridge

Some programs only run elevated. The bridge lets an agent *request* an
elevated launch without ever being able to cause one:

- The operator pre-configures an allowlist in the hands config file
  (`hands/hands_relay_config.json`), key `elevate_allowlist`, mapping a
  request name to static `exe`/`args` strings, e.g.
  `{"notepad-elevated": {"exe": "notepad.exe", "args": ""}}`. The agent
  supplies only a name — never command text.
- `POST /v1/elevate {"name": "..."}` (hands: `POST /elevate`) creates a
  pending request and returns its id; an unconfigured allowlist is a
  501 and an unknown name is a 404. `GET /v1/elevate/<id>` reports the
  state: `pending`, `denied`, `expired` (pending for over 120 s),
  `launched`, or `launch_failed`.
- The request is shown in the packaged app window as a modal dialog
  naming the request and the exact configured exe/args. **Only the
  person at the PC clicking Approve (Yes) runs it** — elevated, via the
  Windows UAC `runas` verb, so the UAC prompt still appears too. Deny
  (No) does nothing. There is no auto-approve path, no timeout-approve,
  and no config flag that approves.
- A request that times out closes its card on time even if the agent
  never polls it. Settled requests are kept for an hour (then pruned; the
  table holds at most 200). The standalone hands app has no approval UI,
  so it denies elevation requests immediately and logs why.

## Hands (Windows)

The hands run **inside the packaged app** (above) — same Flask routes from
`hands/hands_relay_hands.py`, served headless on 127.0.0.1:5000 by the app
process, with the app-owned persistent secret described above. The
standalone hands window's Cloudflare quick tunnel is opt-in
(`HANDS_ENABLE_TUNNEL=1`) and off by default — a public tunnel is a
fallback, never the design.

For a split deployment (services run separately, e.g. under Docker), the
standalone hands app is still available:

```
cd hands
build_hands_relay.bat   :: builds "HandsRelay.exe" (standalone hands)
```

`start-v2.bat` then starts the parser and gateway in console windows for
that split setup; it asks for the hands app's session secret.

Standalone, the hands app works as it always did: per-session generated
secret shown in its window, constant-time compare, lockout with phone
alerts (ntfy), Waitress, Tailscale Serve preferred over any public tunnel,
no public URL at all when Tailscale is present. In that mode you copy its
session secret into the gateway's `HANDS_SECRET` yourself.

## Security model

- The hands and the parser listen on loopback only. **The gateway listens
  on all interfaces (0.0.0.0) by default** — in the packaged app and when
  run directly — so an agent can reach it by the PC's tailnet IP; the token
  is what protects it. Set `RELAY_GATEWAY_HOST=127.0.0.1` when the gateway
  is only reached through Tailscale Serve or a local proxy (the app window
  logs where the gateway listens). Docker publishes it on 127.0.0.1 only.
  Preferred transport is Tailscale (tailnet-only); a public tunnel is a
  fallback, never the design.
- Two credentials are stored, both app-owned in the user's profile
  (`%USERPROFILE%\.HandsRelayV0.1\`): the gateway token (what the agent
  presents) and the hands secret (only ever used on the localhost
  gateway↔hands link — it never crosses the network). Delete a file to
  rotate it. (The standalone hands app still uses a per-session secret.)
- Failed auth attempts are logged; 10 in 10 minutes from one address
  pauses auth for that address for 5 minutes. Gateway rejections and
  lockouts appear in the packaged app's log, and a lockout pushes a phone
  alert through the hands' ntfy topic (a standalone gateway alerts via
  `RELAY_NTFY_TOPIC`). The standalone hands app alerts on its own lockouts.
- Design rule inherited from v1's audit: the URL is obscurity, not a
  control — the secret and the transport are the controls.

## Running the tests

```
pip install flask waitress requests pillow pytest
python tools/run_all_tests.py
```

runs every suite, each in its own process (the suites install
`sys.modules` fakes and must not share an interpreter): files with
pytest test functions via `python -m pytest`, the older script-style
files (`run()` + `check()`) directly. Plain `pytest` at the root is not
equivalent — it collects nothing from the script-style files. CI runs the
same command on every push and pull request
(`.github/workflows/tests.yml`). Windows-live behaviour (real actuation,
hooks, UAC, lock detection, the display probe) is stub-tested only; the
live checklist, including the memory soak, is in
[docs/VERIFICATION.md](docs/VERIFICATION.md).

## Operating notes for agents

*For an agent handed this repo cold. The sections above are the protocol; these are the field rules that keep a session honest. Distilled from sessions where the failures were almost all around the relay — perception, app state, power, comms — not in it.*

**Before you say "ready"**
- A green `/v1/info` is necessary, not sufficient. Ready = relay up + target app in a known state + capture path proven live + power chain checked + owner channel working.
- "Ready", "started" and "running" require two independent live checks seconds apart — e.g. a frame under 3 s old *and* a log file growing between polls. One channel agreeing with itself proves nothing.
- A feed that reports the same frame hash forever may be fetching nothing. Verify liveness with a direct fetch before trusting it.

**Reading the screen**
- Verify-by-screenshot has a quality bar: compressed full-screen frames are not evidence for small text or gauges. Crop and zoom digital readouts; a value you cannot read cleanly is *unread*, never estimated.
- Physical-state claims come from underlying data — logs, digital values — never from a glance at a busy dashboard.
- When the owner states a physical fact (what he plugged in, switched on, or started), that outranks your screen interpretation. Reconcile your data to his statement, not the reverse.

**Acting**
- If an action's expected effect does not appear within its normal latency, stop and read the *full frame* for a modal dialog before any retry. A waiting dialog is the first suspect; input fired at a UI that is not listening goes nowhere — and this relay runs unelevated, so its input is silently dropped into elevated windows (UIPI): an act reporting success is not proof it landed. Verify by capture.
- Keys go to the *focused* window; clicks go to coordinates on the *captured* display. With several windows or monitors those can silently diverge — verify your target window is the focused, topmost one before acting on it.
- First contact with any app includes a recon pass: how start/stop actually work there — prompts, defaults, busy states, secondary capture paths. Note it before real work.
- After any stop or regroup, re-verify state from zero. Never resume from the pre-stop picture.

**Power & comms**
- Inventory every power source in the chain at session start: the PC's charge, the phone if it is the uplink, any device-side power the work depends on. Flag low readings early.
- Tethers and hotspots turn themselves off. A dead feed mid-session is a power/uplink suspect first, not a relay suspect.
- Note clock offsets on every device whose logs you will correlate; device timestamps are not wall time.

**Owner channel**
- A sent message is not a seen message. If the owner seems to be working from partial information, re-deliver the content itself — never point at "the message above".

## Relay Bench

The hands have a test page: **Relay Bench** — a scored battery (clicking, holding and dragging, a 6x6 mate-in-1, instruction traps, a flash-capture test) for grading a computer-use agent working through this relay.

**Run it: https://moorefolio.com/relay-bench/**

The bench lives on the portfolio site, not in this repo — this link is its only presence here.

MIT © 2026 Christopher Moore
