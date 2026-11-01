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
  `%USERPROFILE%\.HandsRelayV0.1\gateway-token.txt` — generated on first run,
  reused after that (delete the file to rotate it). It is shown in the window
  with a Copy button so the operator can hand it to the agent once; the same
  token then keeps working across relaunches.
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

- Gateway: http://127.0.0.1:5010/health — agent API under /v1 (screen, act, parse, info)
- Parser: http://127.0.0.1:5011/health — reports whether the model is loaded

The parser answers /parse only once OmniParser v2 weights are in its
models directory (`python services/parser/fetch_weights.py`, heavy deps in
`services/parser/requirements-model.txt`). Until then it returns 501 —
deliberately: a driver must never mistake "no model" for "empty screen".

## Smoke test (one command)

`tools/smoke.py` starts the parser and gateway natively (loopback only,
throwaway gateway token), checks `/health`, token enforcement and
`/v1/info`, and — when the hands are running — pulls one screenshot and
presses a lone `shift` key through the gateway. One PASS/FAIL/SKIP line
per check; secrets are never printed. If the hands are not running, the
hand checks say so as SKIP instead of failing.

```
python tools\smoke.py                # hands at 127.0.0.1:5000, secret from
                                      # %USERPROFILE%\.HandsRelayV0.1\hands-secret.txt
python tools\smoke.py --use-running  # packaged app already running: set
                                      # $env:RELAY_GATEWAY_TOKEN first
python tools\smoke.py --mock-hands   # no real hands (any OS)
python tools\smoke.py --no-action    # skip the shift key press
```

Requirements: `pip install -r services\gateway\requirements.txt -r services\parser\requirements.txt`.
Exit code 0 means every executed check passed.

## Hands (Windows)

The hands run **inside the packaged app** (above) — same Flask routes from
`hands/hands_relay_hands.py`, served headless on 127.0.0.1:5000 by the app
process, with the app-owned persistent secret described above.

For a split deployment (services run separately, e.g. under Docker), the
standalone hands app is still available:

```
cd hands
build_hands_relay.bat   :: builds "HandsRelay.exe" (v1 hands, carried forward)
```

Standalone, the hands app works as it always did: per-session generated
secret shown in its window, constant-time compare, lockout with phone
alerts (ntfy), Waitress, Tailscale Serve preferred over any public tunnel,
no public URL at all when Tailscale is present. In that mode you copy its
session secret into the gateway's `HANDS_SECRET` yourself.

## Keyboard chords

`/v1/act` accepts `{"op": "hotkey", "keys": ["win", "shift", "left"]}`
(hands: `POST /hotkey`), pressed with `pyautogui.hotkey` — keys go down in
order and come up in reverse. Names are checked against pyautogui's key
list; an unknown name is a 400 that names it, and nothing is pressed. 1–6
keys per chord. Single-key `{"op": "key"}` is unchanged.

Useful chords: `["win", "shift", "left"]` moves the focused window to the
monitor on the left (screenshots only see the primary display, so this is
how to pull a window back into view); `["alt", "tab"]`; `["ctrl", "l"]`.

## Takeover interlock — the person at the PC always wins

When you use the PC yourself, the agent stops driving — immediately and
automatically. The hands watch for physical input the agent did not send
(the mouse moving when no agent action moved it, or a local key or
mouse-button press) and **yield**:

- `move` / `click` / `type` / `key` / `scroll` (and `hotkey`) answer
  **HTTP 409** `{"ok": false, "error": "user active — control yielded"}`
  with a `Retry-After` header; the gateway's `/v1/act` passes it through
  unchanged. Screenshots keep working. A `type` interrupted mid-string
  stops there and reports how many characters it typed.
- `/v1/info` reports `"control": "agent"`, `"yielded"`, or `"unknown"`
  (hands unreachable / older hands), and `control_retry_after` while yielded.
- The packaged app's Hands status line shows who has control; the
  standalone hands window shows it in its status line; both log each
  change.
- Control returns after a quiet period with no local input — default
  **10 seconds**, set with `MUSE_TAKEOVER_QUIET_SECONDS`. No click needed.

**How agent input is told apart from yours.** On Windows the hands install
low-level keyboard and mouse hooks; Windows flags input sent by software
(pyautogui's `SendInput`) as *injected*, so only non-injected input counts
as you (mouse *moves* reported by the hook are additionally ignored while an
agent action is in flight, because pyautogui positions the cursor with
`SetCursorPos`, which may not be flagged as injected). A cursor-deviation poll (every 100 ms) backs this up: the cursor
moving while no agent action is in flight counts as you; agent moves
re-baseline the cursor when they finish. If the hooks cannot be installed,
the hands log it and fall back to `GetLastInputInfo` with a 0.3 s
suppression window around agent actions — **tradeoff:** in fallback mode a
key you press while an agent action is running (or within 0.3 s of it) is
missed; mouse movement is still caught by the cursor poll.

Drivers should treat a 409 as "back off": wait `Retry-After` seconds (or
poll `/v1/info` until `control` is `agent`), take a fresh screenshot — the
person may have changed what's on screen — and replan.

`MUSE_TAKEOVER=0` disables the interlock entirely (not recommended; the
app logs a warning). Guided check on the PC: `python tools\takeover_check.py`.

## Security model

- Nothing listens publicly by default. Preferred transport is Tailscale
  Serve (tailnet-only); a public tunnel is a fallback, never the design.
- The gateway token is stored per machine in the packaged app
  (`%USERPROFILE%\.HandsRelayV0.1\gateway-token.txt`), generated on first run
  and reused across launches so an agent holding it is not locked out by a
  relaunch; delete the file to rotate it. The hands secret in the packaged app
  is the other stored credential:
  app-owned, kept in the user's profile (`%USERPROFILE%\.HandsRelayV0.1\`),
  and only ever used on the localhost gateway↔hands link — it never
  crosses the network. (The standalone hands app still uses a per-session
  secret instead.)
- Failed auth attempts are logged; 10 in 10 minutes pauses auth for
  5 minutes and pushes a phone alert.
- Design rule inherited from v1's audit: the URL is obscurity, not a
  control — the secret and the transport are the controls.


## Troubleshooting: "bad secret" 401s through a proxy

If authenticated requests fail with `{"error": "bad secret"}` even though the secret is correct, suspect the agent-side HTTP proxy before the relay, the tunnel, or the secret.

Some sandbox/egress proxies handle plain-HTTP requests by *fetching* them (absolute-form): the proxy rebuilds the request itself and silently drops custom headers such as `X-Secret` — and query parameters such as `?secret=` — before forwarding. The relay then quite correctly rejects what's left. Switching from header auth to query auth does not help; both are stripped.

Bisect instead of guessing:

1. On the Windows host itself, prove relay + secret:
   `curl.exe -H "X-Secret: <secret>" http://127.0.0.1:5000/screenshot -o test.png`
   A real screenshot is tens to hundreds of KB; the rejection body is 34 bytes.
2. Repeat the same test from each hop in your chain (forwarder, tunnel). The first leg that turns a good secret into a 401 is the culprit.

Fix: force a true CONNECT tunnel through the proxy. CONNECT passes the request bytes untouched, headers and query string included:

```
curl --proxytunnel -x http://<proxy-host>:<proxy-port> -H "X-Secret: <secret>" http://<relay-host>:<port>/screenshot
```

In Python, `http.client` does the same: open the connection to the proxy, call `set_tunnel(target_host, target_port)`, then send the request normally.

(Learned the expensive way, Oct 2026: two evenings of tunnel and secret theories were exonerated by the bisection above; CONNECT fixed it in one request.)

MIT © 2026 Christopher Moore
