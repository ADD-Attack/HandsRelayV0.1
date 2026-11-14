# HandsRelay desktop service

The hands service controls the interactive Windows desktop and must run in the logged-in Windows session. It is never run in Docker. For most installs, use the packaged **HandsRelay v4** app, which starts the hands, gateway, and parser together.

## Standalone hands app

The standalone app is for split deployments. Build it on Windows with `build_hands_relay.bat` from this directory. It creates `HandsRelay.exe`; launch it and copy its current per-launch secret into the gateway's `HANDS_SECRET` environment variable. The packaged app instead owns and persists its hands secret automatically.

The service binds to loopback by default and authenticates requests with `X-Secret`. Tailscale Serve is the preferred remote transport. The optional Cloudflare quick tunnel is off by default (`HANDS_ENABLE_TUNNEL=1` enables it); it is not the normal deployment.

See the root [README](../README.md) for setup, the security model, and the supported hands and gateway APIs. See [VERIFICATION.md](../docs/VERIFICATION.md) for Windows-only checks that still require a real desktop.
