# Hands (Windows, native — never Docker)

The desktop driver. A container has no interactive Windows desktop session,
so this part runs on the host as a normal process — the same constraint
UFO² and OmniTool run into (UFO needs a live console session; OmniTool
sidesteps it with a whole VM, which we rejected: the point is driving
the owner's real desktop).

Current state: the v1 HandsRelay app, carried forward unchanged —
single window, per-session generated secret, constant-time compare,
failed-guess lockout with ntfy phone alerts, Waitress, Tailscale Serve
preferred (tailnet-only, no public URL) with Cloudflare quick tunnel as
fallback, keeps the PC awake while running.

Build: run `build_hands_relay.bat` here on the Windows PC; it produces
`HandsRelay.exe`.

Next steps for v2 hands:
- Register with the gateway and serve /v1/screen + /v1/act to it.
- Add a UI Automation (pywinauto) element endpoint — exact control rects
  from the OS before reaching for pixel clicks (steal list, docs/ROADMAP.md).
