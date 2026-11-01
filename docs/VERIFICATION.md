# Manual verification steps

Checks that need the owner's Windows PC (real hands, real desktop) and
could not be run in the environment that wrote the code. Each entry says
what was verified automatically and what is still **unverified** until the
owner runs it.

## WP0 — smoke script

Verified automatically: `python tools/smoke.py --mock-hands` (7/7 PASS on
Linux against the mock hands) and the hands-not-running path (hand checks
reported as SKIP with the reason, exit 0).

**Unverified — run on the Windows PC:**

1. Install deps: `pip install -r services\gateway\requirements.txt -r services\parser\requirements.txt`
2. With the packaged app **closed**, start only the standalone hands
   (`python hands\hands_relay_hands.py`), then run `python tools\smoke.py`.
   Expect PASS on all checks, including "screenshot via /v1/screen" and
   "harmless action via /v1/act".
3. Close the hands and run `python tools\smoke.py` again. Expect the two
   hand checks as SKIP with "hands are not running", and `SMOKE PASS`.
4. With the packaged app running (Start pressed), copy its gateway token,
   `$env:RELAY_GATEWAY_TOKEN = "<token>"`, then `python tools\smoke.py --use-running`.
   Expect all PASS.

## WP0b — takeover interlock

Verified automatically (`python -m pytest tests`): 20 agent moves never
trip; a cursor deviation yields; 409 body/`Retry-After` on every act
endpoint; screenshot still served; `type` stops mid-string; quiet period
returns control; continued input extends the yield; fallback suppression
window; gateway passes the 409 through and reports `control` in
`/v1/info` ("unknown" for hands without `/status`). Input is stubbed —
no real hooks, cursor or desktop were involved.

**Unverified — run on the Windows PC (rebuild the app first: `build_app.bat`):**

1. Start the packaged app, press Start. The log should show
   "takeover interlock: watching for local keyboard/mouse input". If it
   says "input hooks unavailable", note the error — the fallback detector
   is active.
2. `$env:RELAY_GATEWAY_TOKEN = "<token>"`, then `python tools\takeover_check.py`
   and follow the prompts. Expect `TAKEOVER CHECK PASS`.
3. Watch the Hands status line in the app while moving the mouse: it
   should switch to "YIELDED" within about a second and back to "agent
   has control" about 10 s after you stop.
4. Feel check: with the app running, use the PC normally for a minute —
   mouse and typing must not feel laggy (the low-level hooks run in
   Python; lag would mean the hook callbacks are being starved).

## WP5c — keyboard chords

Verified automatically (`python -m pytest tests`): chord forwarded in order
and lower-cased; unknown names / bad shapes rejected with 400 and nothing
pressed; secret and takeover interlock enforced; `/key` unchanged; gateway
op `hotkey` with token auth and 400 pass-through. Key names `win`, `shift`,
`left`, `alt`, `f4` confirmed present in pyautogui 0.9.54's key list.

**Unverified — run with a second monitor (rebuild the app first):**

1. Put any window (e.g. Notepad) on the non-primary monitor and click it
   so it has focus. Keep hands off afterwards (the takeover interlock
   yields for 10 s after your click).
2. From another tailnet device (or the PC itself after 10 s):
   `curl -H "X-Relay-Token: <token>" -H "Content-Type: application/json" -d "{\"op\":\"hotkey\",\"keys\":[\"win\",\"shift\",\"left\"]}" http://<gateway>:5010/v1/act`
   (use `right` instead of `left` if the primary monitor is to the right).
3. Expect `{"ok": true, ...}`, the window on the primary monitor, and it
   visible in `GET /v1/screen`.
