# Review fix round (2026-11) — memory leak + 29 findings

Merged into `main` through PR #1 from branch `claude/busy-allen-kjpd3s`
(a merge commit, so the commit IDs below are the ones on `main`). **Not
deployed** — rebuild the app to ship it. This file is the handoff: what each
finding was, what changed, where it is tested, and what still needs the real
Windows PC or the owner's decision.

## For the agent picking this up

```
git pull origin main
pip install flask waitress requests pillow pytest
python tools/run_all_tests.py          # 35 of 35 suites pass
```

CI (`.github/workflows/tests.yml`) runs the same runner on Linux 3.11-3.13
and on a Windows runner; all four were green at merge.

1. Read the "Owner decisions" section and confirm each with the owner.
2. On the Windows PC: `build_app.bat`, then work through
   docs/VERIFICATION.md → "Review fixes (2026-11) — live checks owed"
   (L1 memory soak first: `set RELAY_LIVE_SOAK=1` +
   `python -m pytest -q tests/test_windows_live_soak.py`).
3. Report the live results (for example as a comment on PR #1), and fix
   forward on a new branch if anything fails on the real desktop.

Each new test file was also run against the previous version of the module
it covers: every test fails there except three guards that pass on both by
design (failures outside the lockout window not counting, the conftest
strictness check, and "a fresh pending elevation still blocks self-heal").

## The leak (L1)

`hands/hands_relay_hands.py` `_probe_display_raw` defined a ctypes GUID struct
inside the function and called `ctypes.POINTER()` on it. `POINTER()` caches
every pointer type it creates in a process-global dict that is never pruned
(CPython ≤ 3.13), and that entry pins the class: ~6 KB per call, measured on
3.11/3.12/3.13. It ran on every `GET /state` — every gateway `/v1/info`
refresh, i.e. every 5-10 s while the packaged app runs (its own dashboard
poll): ~50-100 MB/day. Fixed by building the structs once at import
(commit `c30bb72`); `tests/test_display_probe_no_leak.py` forces the Windows
branch with faked DLLs and fails on the old code (+500 classes / 500 calls).

## Ledger

| ID | Finding | Fix | Commit | Tests |
|---|---|---|---|---|
| L1 | display probe leaks ~6 KB per /state | struct types hoisted to module level | `c30bb72` | test_display_probe_no_leak, test_windows_live_soak (live) |
| L2 | dead claim-queue head re-reserved on every release; queue unbounded | unused reservation forfeits the slot; cap 32; 60-min refresh TTL; holder/purpose bounded | `b89a165` | test_review_gateway (queue tests) |
| L3 | self-heal restart strands waitress workers + old module threads | `_retire_service`: close + dispatcher shutdown + module `stop_background()` | `9fdfc5c` (+ stop hooks in `b89a165`, `06f99e0`) | test_review_app (real waitress), test_review_gateway, test_review_parser |
| L4 | abandoned uploads (≤16 MB each) kept until the next file-drop call; no concurrency cap | purge on any authenticated request; 4 uploads / 64 MB in flight (503) | `06f99e0` | test_review_hands |
| L5 | elevation records never deleted; standalone queue never drained | 1-h retention + cap 200; standalone window denies and logs; its log widget capped | `06f99e0` | test_review_hands |
| L6 | per-peer auth maps and watcher map only grow | expired entries dropped, stale peers swept, watchers expire on write + cap 64 | `b89a165`, `06f99e0` | test_review_gateway, test_review_hands |
| I1 | gateway on 0.0.0.0 contradicts README | `RELAY_GATEWAY_HOST` (default unchanged), bind logged, README corrected | `b89a165`, `9fdfc5c`, `150b957` | test_review_app |
| I2 | foreground lookup enumerates every window + per-window failed `import psutil` | `GetForegroundWindow` path; psutil imported once | `06f99e0` | test_review_hands |
| I3 | a new OS thread per `after()` (~3.5/s) | one scheduler thread over a time-ordered queue | `9fdfc5c` | test_review_app |
| I4 | every frame decoded for its ETag / every 200 ms in screen/wait | 8-entry LRU of masked hashes keyed by the PNG's SHA-256 | `b89a165` | test_review_gateway |
| I5 | input hooks never removed, never restarted | `stop()` (unhook + WM_QUIT); dead hook thread restarts after 60 s | `06f99e0` | test_review_hands |
| I6 | README drift; dead duplicate `_tick_ms` | README corrected (token persisted, bind, receipts, lockouts); dead copy removed | `06f99e0`, `150b957` | — |
| E1 | `/focus` always fails without pywin32 (dead fallback); hwnd pin off | activation + foreground via ctypes; fallback windows carry the HWND; pywin32 optional-bundled | `06f99e0`, `9fdfc5c`, `150b957` | test_review_hands |
| E2 | held acts forwarded after their deadline (serial loop) | never START a forward past the deadline → refused-yielded; `_receipt_tick` | `b89a165` | test_review_gateway (real tick) |
| E3 | unpolled elevation stays pending forever, blocks hands self-heal | TTL applied in the app's state read + sweep before `_skip_restart` | `06f99e0`, `9fdfc5c` | test_review_app |
| E4 | gateway lockouts invisible, no alert | gateway `log_queue` drained into the window; alert via the hands' ntfy (or `RELAY_NTFY_TOPIC`) | `b89a165`, `9fdfc5c` | test_review_gateway, test_review_app |
| E5 | legacy routes 500 on bad input and on the owner's FAILSAFE | JSON 400 validation; FailSafe → 409 `error_code: "failsafe"` | `06f99e0` | test_review_hands |
| E6 | non-object bodies / list `op` → 500; receipts keep arbitrary op | `_json_body()` everywhere; op type-checked; receipts bounded | `b89a165`, `06f99e0` | test_review_gateway, test_review_hands |
| E7 | undecodable frame → HTML 500; NaN/Inf region → 500 | served without ETag / JSON 502; non-finite region → 400 | `b89a165` | test_review_gateway |
| E8 | parser never retries a failed load; model_loaded needs a /parse | retry every 60 s; background loader started by app and `python app.py` | `9fdfc5c` | test_review_parser, test_review_app |
| E9 | standalone build + launcher broken | build file/icon/motion.py fixed; launcher asks for the secret, quoted `set`; CRLF via .gitattributes; gateway strips env | `150b957` | live (VERIFICATION) |
| T1 | no CI; 16 of 29 test files not pytest | `tools/run_all_tests.py` + CI workflow | `150b957` | — |
| T2 | Windows branches never executed | cross-platform forced-branch tests + gated live soak + Windows CI job (green after test-setup fixes for Windows hosts) | `c30bb72`, `150b957`, `e12a129`, `2f2065b` | — |
| T3 | conftest monkeypatch never raised | strict except Windows-only ctypes names; fake pyautogui has FailSafeException | `06f99e0` | test_review_hands |
| T4 | lockout counting never exercised | 10-in-window, pruning, expiry, valid-token, alert tests | `b89a165` | test_review_gateway |
| T5 | self-heal restart never performed | restart path + real waitress retire test | `9fdfc5c` | test_review_app |
| T6 | retry thread stubbed in every test | `_receipt_tick` driven with a virtual clock | `b89a165` | test_review_gateway |
| T7 | Windows release evidence owed | live checklist (11 steps) added; evidence itself still owed | `150b957` | live |
| T8 | README claims untested (Pillow-missing, file-drop TTL, card expiry, queue) | tests added | `b89a165`, `06f99e0`, `9fdfc5c` | review test files |

## Owner decisions (behaviour changes to confirm)

1. **Gateway bind (I1)** — left at `0.0.0.0` so an agent that reaches the PC
   by its tailnet IP keeps working; the README now says so. If the agent goes
   through Tailscale Serve, set `RELAY_GATEWAY_HOST=127.0.0.1` (or flip the
   default in `hands_relay_v2_app.py` `SERVICE_HOSTS`).
2. **Claim queue (L2)** — a queue head that does not claim within its 30 s
   window now loses its slot (it can re-queue at the back). Waiters must
   re-POST `{"queue": true}` at least hourly; max 32 waiters.
3. **Held acts (E2)** — a held act whose hold has lapsed is never forwarded,
   even if the owner just went idle; it settles `refused-yielded`.
4. **Standalone hands + elevation (L5)** — requests are denied immediately
   (that window has no approval UI); previously they sat pending for 120 s.
5. **Input validation (E5)** — `/click` accepts `button` left / middle /
   right / primary / secondary only; malformed bodies are 400s.
6. **Test harness (T3)** — `monkeypatch.setattr` raises on a missing
   attribute again; pass `raising=False` only when a test means it.

## Left as is, on purpose

- **Duplicate yield checks** in `/type`, `/break`, `/drag`, `/focus` (I6):
  kept — tests call those views directly and rely on the in-route check,
  and after I2 the extra probe is cheap.
- **Script-style suites (T1)** are not converted to pytest; the runner and
  CI cover them. Converting is a follow-up.
- **`/v1/screen` ETag (I4)**: still decodes each *new* frame — the ETag is a
  documented contract; only repeated identical frames skip the decode.
- **Parser captioner** load is not retried (optional component; the
  detector, which gates `model_loaded`, is).
- **Windows CI job** passes but is still `continue-on-error`; drop that line
  in `.github/workflows/tests.yml` to make it a required check (it makes a
  few real Win32 calls on the runner, so watch it for flakiness first).
