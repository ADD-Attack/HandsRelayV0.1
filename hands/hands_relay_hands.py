"""
HandsRelay -- let Muse drive your Windows mouse/keyboard, with your consent.

One small window. No terminals.

WHAT IT DOES
  - Starts the relay server on 127.0.0.1:5000 (localhost only -- nothing on
    your network can reach it directly).
  - Opens a Cloudflare quick tunnel only on its own explicit opt-in
    (HANDS_ENABLE_PUBLIC_TUNNEL=1, in addition to
    HANDS_ENABLE_TUNNEL=1); off by default — a public tunnel is never
    an automatic fallback from a failed Tailscale Serve.
  - Generates a FRESH strong secret on every launch. The old workflow (one
    fixed secret, reused across sessions) is gone: when you close the app,
    that secret stops existing.
  - You paste the URL + secret to Muse in chat when you want a session.
  - Close the window (or press Stop) and everything dies instantly: server,
    tunnel, secret.

SECURITY NOTES
  - This is remote-control software. Only run it while you want Muse
    driving, and keep an eye on the screen.
  - The tunnel URL is reachable by anyone who has it -- the secret is the
    lock. The app makes the secret long and random for you; don't share the
    URL or secret with anyone but Muse.
  - The secret is compared in constant time (hmac.compare_digest), and
    failed guesses are written to the log area so you can see them. After
    10 bad guesses inside 10 minutes, authentication pauses for 5 minutes
    (lockout) -- restart the app to clear it early.
  - Served by Waitress (a production WSGI server) instead of the Flask
    development server.
  - Transport: if Tailscale is installed on this PC, the app uses
    Tailscale Serve -- the relay is then reachable only inside your
    tailnet and there is NO public URL at all. A public Cloudflare
    tunnel is a separate explicit opt-in and is never started
    automatically when Tailscale fails. Responses carry X-Robots-Tag: noindex
    so tunnel URLs stay out of search indexes.
  - pyautogui.FAILSAFE is on: slam the mouse into any screen corner to
    abort the current action.

WHAT MUSE CAN DO WITH IT (unchanged from cursor_relay.py)
  GET  /health              liveness only, no secret needed
  GET  /screenshot          PNG of your screen
  GET  /monitors            monitor rectangles (virtual-desktop coords)
  POST /move    {x, y}      move the cursor
  POST /click   {x, y, button}  click
  POST /type    {text}      type text
  POST /key     {key}       press a key ("enter", "tab", "esc", ...)
  POST /scroll  {clicks}    scroll
  Every call except /health needs header  X-Secret: <the app's secret>

ADDED IN V3-P4 (wishlist #3, #4, #6, #9, #19) — same X-Secret gate:
  POST /hotkey  {keys: ["ctrl", "l"]}   press a true key chord
  POST /break   {times?: 1..3}          Ctrl+C chord(s) — see its docstring
  POST /drag    {x1, y1, x2, y2, button?, duration?}
  GET  /windows                         list open windows
  POST /focus   {title, exact?}         bring a window to the foreground
  POST /clipboard/set {text}            set the clipboard
  GET  /clipboard/get                   read the clipboard — SENSITIVE: it
                                        stays behind the X-Secret gate and
                                        every read is written to the log
                                        area (see log_line call in the
                                        route), so the operator can see
                                        reads in the app window.
  POST /type also accepts {method: "auto"|"paste"|"keys"}: long text can
  go through the clipboard (paste) instead of slow keystrokes.
CAPTURE + INPUT REALISM (v3)
  GET /screenshot?monitor=N   capture monitor N (0-based) only
  GET /screenshot?region=x,y,w,h
                               capture that rect in virtual-desktop coords
                               (monitor and region are mutually exclusive)
  /type types with per-keystroke jitter by default ({jitter: false}
  restores the old single fast write). /move and /click travel a curved,
  human-like path by default ({human: false} restores the old direct
  move). Both planners are pure functions (_plan_key_delays, _plan_path)
  so the timing/shape logic is testable without a desktop.
"""

import base64
import binascii
import ctypes
import datetime
import hashlib
import hmac
import io
import json
import math
import os
import queue
import random
import re
import secrets as secrets_mod
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.request
import uuid

import pyautogui
from flask import Flask, request, jsonify, send_file

PORT = 5000

# Fresh per launch, from the OS CSPRNG. 24 bytes -> a 32-character
# URL-safe string (~190 bits). Never stored, never reused.
SHARED_SECRET = secrets_mod.token_urlsafe(24)

pyautogui.FAILSAFE = True
# R4 (F2): PAUSE stays 0. pyautogui's PAUSE sleeps after EVERY call, and
# the v3 input-realism planners (_plan_path, _plan_key_delays) already
# own the deliberate delay budget — a global 0.15 s tax per call made a
# ~50-step mouse path pay ~7.5 s on top of its planned 0.3-1.1 s and
# capped typing at ~3-5 chars/s regardless of the jitter plan. FAILSAFE
# above is the independent emergency stop and is unaffected.
pyautogui.PAUSE = 0


def _load_motion():
    """hands/motion.py, loaded by path: the app loads THIS file by path
    too (and PyInstaller bundles both side by side), so a plain import
    would depend on sys.path."""
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "motion.py")
    bundled = os.path.join(getattr(sys, "_MEIPASS", ""), "hands", "motion.py")
    if not os.path.exists(path) and os.path.exists(bundled):
        path = bundled
    spec = importlib.util.spec_from_file_location("hands_relay_hands_motion", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


motion = _load_motion()

# ---------------------------------------------------------------- server

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1_000_000  # requests are tiny JSON


def _json_body():
    """The request's JSON object, or {} for a missing/invalid/non-object
    body. Review E6: the old `get_json(force=True, silent=True) or {}`
    idiom let a JSON array through, and the next `.get` raised a 500."""
    body = request.get_json(force=True, silent=True)
    return body if isinstance(body, dict) else {}


def _failsafe_response(_exc):
    """Review E5: pyautogui's FAILSAFE (the owner slamming the pointer into
    a screen corner) is the owner's emergency stop. It used to surface as
    a bare HTML 500, which an agent cannot tell from a bug -- and the
    queued-act pump just recorded "failed". It is now a structured,
    non-yield 409 that says: stop and check with the owner."""
    log_line("FAILSAFE: owner moved the pointer into a screen corner - act aborted")
    return (
        jsonify(
            {
                "ok": False,
                "error": "owner triggered the failsafe (pointer in a screen corner) - "
                "act aborted; stop and check with the owner before acting again",
                "error_code": "failsafe",
            }
        ),
        409,
    )


_FAILSAFE_EXC = getattr(pyautogui, "FailSafeException", None)
if isinstance(_FAILSAFE_EXC, type) and issubclass(_FAILSAFE_EXC, BaseException):
    app.register_error_handler(_FAILSAFE_EXC, _failsafe_response)


_USER32 = None


def _user32():
    """Private user32 handle with pointer-sized HWND argtypes (Windows only).

    Private so these argtypes never leak into pyautogui's / pygetwindow's
    use of the shared ctypes.windll.user32. Used for focus and foreground
    lookups so neither needs pywin32 (review E1, I2).
    """
    global _USER32
    if _USER32 is None:
        from ctypes import wintypes

        lib = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        lib.GetForegroundWindow.argtypes = []
        lib.GetForegroundWindow.restype = wintypes.HWND
        lib.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        lib.GetWindowTextLengthW.restype = ctypes.c_int
        lib.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        lib.GetWindowTextW.restype = ctypes.c_int
        lib.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        lib.GetWindowThreadProcessId.restype = wintypes.DWORD
        lib.IsIconic.argtypes = [wintypes.HWND]
        lib.IsIconic.restype = wintypes.BOOL
        lib.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        lib.ShowWindow.restype = wintypes.BOOL
        lib.SetForegroundWindow.argtypes = [wintypes.HWND]
        lib.SetForegroundWindow.restype = wintypes.BOOL
        _USER32 = lib
    return _USER32

# ---- failed-guess lockout ------------------------------------------------
_fail_lock = threading.Lock()
MAX_FAILS = 10        # bad secrets allowed inside the window...
FAIL_WINDOW = 600     # ...of 10 minutes, then auth pauses for...
LOCKOUT_SECONDS = 300  # ...5 minutes (restart the app to clear early)


# ---- phone alerts (ntfy) + persistent security log -----------------------
_BASE_DIR = os.path.dirname(
    os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)
)
_CONFIG_PATH = os.path.join(_BASE_DIR, "hands_relay_config.json")
_LOG_FILE = os.path.join(_BASE_DIR, "hands_relay.log")


def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_config(cfg: dict) -> None:
    try:
        with open(_CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
    except Exception:
        pass


_cfg = _load_config()
ALERT_TOPIC = str(_cfg.get("ntfy_topic") or "").strip()
if not ALERT_TOPIC:
    ALERT_TOPIC = "HandsRelay-" + secrets_mod.token_hex(6)
    _cfg["ntfy_topic"] = ALERT_TOPIC
    _save_config(_cfg)


def file_log(message: str) -> None:
    try:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(_LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except Exception:
        pass


def alert_phone(title: str, message: str) -> None:
    """Best-effort push to the owner's phone via ntfy.sh (topic above)."""

    def _send():
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{ALERT_TOPIC}",
                data=message.encode("utf-8"),
                headers={"Title": title, "Tags": "warning"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


_fail_by_peer = {}
_lockout_by_peer = {}
_PEER_SWEEP_AT = 256  # review L6: sweep stale per-peer entries past this many peers


def _peer_key():
    try:
        return request.remote_addr or "-"
    except RuntimeError:
        return "-"


_ACT_PATHS = {
    "/move",
    "/click",
    "/type",
    "/key",
    "/scroll",
    "/hotkey",
    "/drag",
    "/focus",
    # R4 (F5): /break injects real Ctrl+C chords and /clipboard/set
    # overwrites the owner's clipboard — both are input/state the owner
    # can collide with, so both sit behind the same WP0b yield.
    "/break",
    "/clipboard/set",
    # H4: reads are covered too. /clipboard/get can expose a password
    # the owner just copied, and /screenshot can expose the screen
    # they are working on, so both yield when the owner is active or
    # the desktop is locked — the same as any act.
    "/clipboard/get",
    "/screenshot",
}


def _yield_if_active():
    """409 and do not run the op when the owner was active recently.

    _probe_lock_state is the only seam: (locked, idle_seconds), or
    (None, None) off Windows / on failure. None idle means inactive.
    Yield only when idle_seconds < HANDS_YIELD_IDLE_SECONDS (default 5).

    R4 (F6): a LOCKED desktop also yields — acts injected at the
    Winlogon desktop go nowhere yet reported success, so queued
    receipts settled "applied" for input that never landed. The 409
    carries error_code "locked" so the gateway holds and retries the
    act until unlock instead of settling it (F11: the code, not the
    prose, is the contract; the prose keeps the word "yielded" so an
    older gateway still classifies it by substring).
    """
    try:
        threshold = float(os.environ.get("HANDS_YIELD_IDLE_SECONDS", "5"))
    except (TypeError, ValueError):
        threshold = 5.0
    locked, idle_seconds = _probe_lock_state()
    # R9 (#28): input inside a recorded resting-hand span is ours, not the
    # owner's — owner idle keeps counting from the owner's last input.
    idle_seconds = _LEDGER.owner_idle(idle_seconds, _tick_ms())
    if locked is True:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "desktop locked — control yielded",
                    "error_code": "locked",
                }
            ),
            409,
        )
    if idle_seconds is not None and idle_seconds < threshold:
        body = {
            "ok": False,
            "error": "user active — control yielded",
            "error_code": "yield",
        }
        # R9 (#20): the window this act was aimed at, captured at the
        # first (yielded) attempt, so the gateway can pin the held
        # retry to it instead of firing into whatever is in front later.
        hwnd = _foreground_hwnd()
        if hwnd is not None:
            body["foreground_hwnd"] = hwnd
        return jsonify(body), 409
    return None


def _foreground_window_native():
    """The foreground window straight from GetForegroundWindow, or None
    off Windows / on any failure (the seam tests stub).

    Review I2: finding the focused window used to enumerate EVERY window
    and resolve every window's process name -- on each owner-active yield
    and on every expect_focus check, i.e. once a second per held receipt.
    This is one window and one process lookup. Review E1: it needs no
    pywin32, so the hwnd pin for held acts works in the packaged exe.
    """
    if os.name != "nt":
        return None
    try:
        from ctypes import wintypes

        user32 = _user32()
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        length = max(0, int(user32.GetWindowTextLengthW(hwnd)))
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, len(buf))
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return {
            "title": buf.value,
            "process": _process_name(pid.value),
            "pid": int(pid.value) or None,
            "focused": True,
            "_hwnd": int(hwnd),
        }
    except Exception:
        return None


def _foreground_window():
    """The focused window dict, or None: the native lookup first, then the
    _enum_windows() scan (off Windows / if the native call fails)."""
    win = _foreground_window_native()
    if win is not None:
        return win
    for win in _enum_windows():
        if win.get("focused"):
            return win
    return None


def _foreground_hwnd():
    """Handle of the foreground window as an int, or None. Never raises."""
    try:
        win = _foreground_window()
    except Exception:
        return None
    if not win:
        return None
    hwnd = win.get("_hwnd")
    return int(hwnd) if isinstance(hwnd, int) and not isinstance(hwnd, bool) else None


def _focus_matches(expect, win):
    """True when the foreground window satisfies an expect_focus spec.

    expect is a title substring (str) or {"title": ..., "process": ...,
    "hwnd": ...}; each field given must appear (case-insensitive
    substring) in the foreground window's matching field, and hwnd must
    equal its window handle exactly. An empty spec never matches.
    """
    if isinstance(expect, str):
        expect = {"title": expect}
    if not isinstance(expect, dict) or win is None:
        return False
    want_title = expect.get("title")
    want_process = expect.get("process")
    want_hwnd = expect.get("hwnd")
    if not want_title and not want_process and want_hwnd is None:
        return False
    if want_hwnd is not None:
        try:
            if int(want_hwnd) != int(win.get("_hwnd")):
                return False
        except (TypeError, ValueError):
            return False
    if want_title and str(want_title).lower() not in (win.get("title") or "").lower():
        return False
    if want_process and str(want_process).lower() not in (win.get("process") or "").lower():
        return False
    return True


def _focus_guard():
    """Refuse an act whose payload names its target window
    (expect_focus) when that window is not the foreground one.

    R4 (F3): a queued act can sit held for up to 120 s while the owner
    switches windows; without a guard it fires into whatever is in
    front when the yield clears (2026-10-19: a Reset click aimed at a
    chess page landed in a chat window). The refusal is a NON-yield
    409 (error_code "foreground_changed") so the queued-act pump
    settles the receipt failed instead of retrying the act into the
    wrong window for the rest of the hold. Fails closed: when the
    foreground window cannot be determined, an act that asked for
    verification is refused rather than fired blind.
    """
    body = _json_body()
    expect = body.get("expect_focus")
    if expect is None:
        return None
    if _focus_matches(expect, _foreground_window()):
        return None
    return (
        jsonify(
            {
                "ok": False,
                "error": "foreground changed — act refused",
                "error_code": "foreground_changed",
            }
        ),
        409,
    )


def _auth_decide(provided):
    """One critical section. A valid secret is never locked out.

    An empty SHARED_SECRET does not compare equal to an empty header.
    Those rejects are not counted: the packaged app assigns the secret
    after import, and a blank config must not burn the lockout budget.
    """
    now = time.monotonic()
    peer = _peer_key()
    with _fail_lock:
        secret = SHARED_SECRET or ""
        if secret and hmac.compare_digest(provided, secret):
            return "ok", False
        until = _lockout_by_peer.get(peer)
        if until is not None:
            if now < until:
                return "locked", False
            del _lockout_by_peer[peer]  # expired: forget it (review L6)
        if not secret:
            return "bad", False
        times = [t for t in _fail_by_peer.get(peer, ()) if now - t < FAIL_WINDOW]
        times.append(now)
        tripped = len(times) >= MAX_FAILS
        if tripped:
            _lockout_by_peer[peer] = now + LOCKOUT_SECONDS
            _fail_by_peer.pop(peer, None)
        else:
            _fail_by_peer[peer] = times
        if len(_fail_by_peer) + len(_lockout_by_peer) > _PEER_SWEEP_AT:
            # Review L6: per-peer entries used to live forever.
            for p in [p for p, u in _lockout_by_peer.items() if now >= u]:
                del _lockout_by_peer[p]
            for p in [p for p, ts in _fail_by_peer.items() if not ts or now - ts[-1] >= FAIL_WINDOW]:
                del _fail_by_peer[p]
        return "bad", tripped


@app.before_request
def gate():
    if request.path == "/health":
        return  # liveness only, per this file's contract: a lockout or a
        # secret-less probe must never read as "down" (the gateway probes
        # /health to decide reachability, and a supervisor restart on a
        # false "down" would wipe the very lockout that is protecting us)
    provided = request.headers.get("X-Secret") or ""
    decision, tripped = _auth_decide(provided)
    if decision == "locked":
        return jsonify({"ok": False, "error": "locked out"}), 429
    if decision != "ok":
        file_log("bad secret attempt")
        if tripped:
            log_line(
                "LOCKOUT: too many bad secrets -- authentication paused "
                "for 5 minutes"
            )
            file_log("LOCKOUT triggered (10 bad secrets inside 10 minutes)")
            alert_phone(
                "HandsRelay LOCKOUT",
                "Someone just made 10 bad secret guesses at your relay. "
                "Authentication is paused for 5 minutes.",
            )
        log_line(f"rejected request to {request.path} (bad secret)")
        return jsonify({"ok": False, "error": "bad secret"}), 401
    # Review L4: abandoned uploads (up to 16 MB each) used to be purged only
    # by the next file-drop call, which might never come. Any authenticated
    # request now does it -- the gateway's /state poll runs every few seconds.
    _purge_uploads()
    if request.path in _ACT_PATHS:
        refused = _yield_if_active()
        if refused is not None:
            return refused
        refused = _focus_guard()
        if refused is not None:
            return refused


@app.after_request
def noindex(resp):
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@app.get("/health")
def health():
    return {"ok": True}


# ---- capture seams ---------------------------------------------------------
# Kept as module-level functions so tests can stub the Windows layer and
# exercise the endpoint logic anywhere.


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.c_uint32),
    ]


_MONITORINFOF_PRIMARY = 1


def _monitor_rects():
    """Monitor rectangles in virtual-desktop coordinates.

    Each entry: {"x", "y", "w", "h", "primary"}. Origins can be negative
    (a monitor left of / above the primary). On Windows the list comes
    from EnumDisplayMonitors; anywhere else (or on failure) it falls
    back to a single primary rect from pyautogui.size().
    """
    if os.name == "nt":
        try:
            rects = []
            user32 = ctypes.windll.user32
            enum_proc = ctypes.WINFUNCTYPE(
                ctypes.c_bool,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.POINTER(_RECT),
                ctypes.c_ssize_t,
            )

            def _cb(hmonitor, _hdc, _clip, _data):
                info = _MONITORINFO()
                info.cbSize = ctypes.sizeof(_MONITORINFO)
                if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
                    r = info.rcMonitor
                    rects.append(
                        {
                            "x": int(r.left),
                            "y": int(r.top),
                            "w": int(r.right - r.left),
                            "h": int(r.bottom - r.top),
                            "primary": bool(info.dwFlags & _MONITORINFOF_PRIMARY),
                        }
                    )
                return True

            if user32.EnumDisplayMonitors(None, None, enum_proc(_cb), 0) and rects:
                return rects
        except Exception:
            pass  # fall through to the single-rect fallback
    w, h = pyautogui.size()
    return [{"x": 0, "y": 0, "w": int(w), "h": int(h), "primary": True}]


def _grab(rect):
    """Capture rect ({"x","y","w","h"}) or, with None, the full frame.

    pyautogui on Windows captures the virtual desktop, so a region with
    a negative origin works through the same call.
    """
    if rect is None:
        return pyautogui.screenshot()
    return pyautogui.screenshot(
        region=(int(rect["x"]), int(rect["y"]), int(rect["w"]), int(rect["h"]))
    )


def _parse_region(raw):
    """'x,y,w,h' -> rect dict, or None if malformed / non-positive size."""
    try:
        parts = [int(p) for p in str(raw).split(",")]
    except ValueError:
        return None
    if len(parts) != 4 or parts[2] <= 0 or parts[3] <= 0:
        return None
    return {"x": parts[0], "y": parts[1], "w": parts[2], "h": parts[3]}


@app.get("/monitors")
def monitors():
    rects = _monitor_rects()
    return {"ok": True, "monitors": rects, "count": len(rects)}


@app.get("/screenshot")
def screenshot():
    monitor_raw = request.args.get("monitor")
    region_raw = request.args.get("region")
    if monitor_raw is not None and region_raw is not None:
        return jsonify({"ok": False, "error": "pass monitor or region, not both"}), 400
    rect = None
    monitor_idx = None
    region_rect = None
    if monitor_raw is not None:
        try:
            monitor_idx = int(monitor_raw)
        except ValueError:
            return jsonify({"ok": False, "error": "monitor must be an integer"}), 400
        rects = _monitor_rects()
        if monitor_idx < 0 or monitor_idx >= len(rects):
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": f"monitor {monitor_idx} out of range: "
                        f"{len(rects)} monitor(s) available",
                    }
                ),
                400,
            )
        rect = rects[monitor_idx]
    elif region_raw is not None:
        region_rect = _parse_region(region_raw)
        if region_rect is None:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "region must be 'x,y,w,h' (ints, w and h > 0)",
                    }
                ),
                400,
            )
        rect = region_rect
    img = _grab(rect)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    resp = send_file(buf, mimetype="image/png")
    resp.headers["X-Screen-Width"] = str(img.size[0])
    resp.headers["X-Screen-Height"] = str(img.size[1])
    if monitor_idx is not None:
        resp.headers["X-Screen-Monitor"] = str(monitor_idx)
    if region_rect is not None:
        resp.headers["X-Screen-Region"] = (
            f"{region_rect['x']},{region_rect['y']},"
            f"{region_rect['w']},{region_rect['h']}"
        )
    return resp


# ---- input-realism planners (pure; rng is injected) ------------------------


def _plan_key_delays(text, rng):
    """Per-character typing delays (seconds slept AFTER each character).

    Base delay is uniform in [0.04, 0.16]; a character that ends a word
    (space) adds uniform [0.05, 0.20] and one that ends a sentence
    (. ! ?) adds uniform [0.10, 0.30]. If the total exceeds the budget
    max(5.0, len(text) * 0.16) every delay is scaled down proportionally
    so long pastes stay bounded.
    """
    delays = []
    for ch in text:
        delay = rng.uniform(0.04, 0.16)
        if ch == " ":
            delay += rng.uniform(0.05, 0.20)
        elif ch in ".!?":
            delay += rng.uniform(0.10, 0.30)
        delays.append(delay)
    budget = max(5.0, len(text) * 0.16)
    total = sum(delays)
    if total > budget and total > 0:
        scale = budget / total
        delays = [d * scale for d in delays]
    return delays


def _flag(env_name, cfg_key, default):
    """Feature flag: env (1/0, true/false) wins over the config file."""
    raw = os.environ.get(env_name)
    if raw is not None and raw.strip():
        return raw.strip().lower() not in ("0", "false", "no", "off")
    val = _cfg.get(cfg_key)
    return default if val is None else bool(val)


def _human_motion_default():
    """R9 motion flag (#24/#29/#30): default ON. A request's "human"
    field still overrides per act."""
    return _flag("HANDS_HUMAN_MOTION", "human_motion", True)


# (Review I6: a first _tick_ms definition lived here and was silently
# replaced by the one in the lock/idle section below, which returns None
# off Windows. Only that one ever ran; the dead copy is gone.)


# One input stream at a time, with hand-travel between devices (#30 rule
# 4). Every injecting op holds it; the resting hand only ever try-acquires.
_INPUT = motion.InputMutex(clock=lambda: time.monotonic(), sleep=lambda s: time.sleep(s))
# Resting-hand injections, so the yield probe can tell them from the owner (#28).
_LEDGER = motion.SelfInputLedger()


class _input:
    """with _input("mouse"|"keys"): hold the input mutex for one op."""

    def __init__(self, kind):
        self.kind = kind

    def __enter__(self):
        _INPUT.acquire(self.kind, random)
        _resting.act_begin()
        return self

    def __exit__(self, *exc):
        # R11: every real agent act resets the resting-hand ramp to zero.
        _resting.act_end(time.monotonic())
        _INPUT.release(self.kind)
        return False


def _human(d):
    h = d.get("human")
    return _human_motion_default() if h is None else bool(h)


def _plan_path(x0, y0, x1, y1, rng, width=None, path_seconds=None):
    """Human-like pointer path: list of (x, y, delay_after) steps.

    R9: delegates to motion.plan_move — cubic Bezier with 1-2 random
    control points, Fitts velocity profile, value-noise tremor, and per
    movement one of structural miss / overshoot+settle / direct. The
    final step is always exactly (x1, y1).
    """
    return motion.plan_move(x0, y0, x1, y1, rng, width=width, path_seconds=path_seconds)


def _run_path(steps):
    for sx, sy, delay in steps:
        pyautogui.moveTo(sx, sy, duration=0)
        time.sleep(delay)


def _move_human(x, y, rng=None, width=None, path_seconds=None):
    """Move the cursor to (x, y) along a planned human-like path."""
    rng = rng if rng is not None else random
    x0, y0 = pyautogui.position()
    _run_path(_plan_path(x0, y0, x, y, rng, width=width, path_seconds=path_seconds))


def _box(d):
    """Optional target box {w, h} from an act body, else (None, None)."""
    try:
        w = float(d["w"]) if d.get("w") is not None else None
        h = float(d["h"]) if d.get("h") is not None else None
    except (TypeError, ValueError):
        return None, None
    if w is None or h is None or w <= 0 or h <= 0:
        return None, None
    return w, h


_CLICK_BUTTONS = ("left", "middle", "right", "primary", "secondary")


def _xy(d):
    """(x, y) ints from an act body. Review E5: /move, /click, /key and
    /scroll used to index and int() the body unchecked, so a missing or
    malformed field was an HTML 500; they now answer a JSON 400 like
    /drag always has."""
    try:
        return int(d["x"]), int(d["y"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("x and y must be integers") from exc


def _bad_request(message):
    return jsonify({"ok": False, "error": message}), 400


@app.post("/move")
def move():
    d = _json_body()
    try:
        x, y = _xy(d)
    except ValueError as exc:
        return _bad_request(str(exc))
    with _input("mouse"):
        if _human(d):
            w, h = _box(d)
            if d.get("attention"):
                time.sleep(motion.attention_delay(random, d.get("text_chars")))
            _move_human(x, y, width=min(w, h) if w else None)
        else:
            pyautogui.moveTo(x, y, duration=0.2)
    return {"ok": True}


@app.post("/click")
def click():
    """Click at (x, y). Human mode (default, R9 #24/#29/#30):

    optional {w, h}: the target box centred on (x, y) — the landing
    point is Gaussian inside it and its size sets the Fitts speed;
    optional attention (default true) + text_chars: the pre-move
    hesitation. Timing is fitted to the +0.5 s per-click budget.
    """
    d = _json_body()
    try:
        x, y = _xy(d)
    except ValueError as exc:
        return _bad_request(str(exc))
    button = d.get("button", "left")
    if button not in _CLICK_BUTTONS:
        return _bad_request("button must be one of " + ", ".join(_CLICK_BUTTONS))
    if not _human(d):
        with _input("mouse"):
            pyautogui.moveTo(x, y, duration=0.2)
            pyautogui.click(x=x, y=y, button=button)
        return {"ok": True}
    rng = random
    w, h = _box(d)
    with _input("mouse"):
        x0, y0 = pyautogui.position()
        tx, ty = motion.landing_point(x, y, w, h, rng) if w else (x, y)
        dist = math.hypot(tx - x0, ty - y0)
        attention = (
            motion.attention_delay(rng, d.get("text_chars"))
            if d.get("attention", True)
            else 0.0
        )
        travel = motion.fitts_duration(dist, min(w, h) if w else None, rng) if dist >= 4 else 0.02
        hover = motion.hover_delay(rng)
        hold = motion.click_hold(rng)
        attention, travel, hover, hold = motion.fit_click_timing(
            dist, attention, travel, hover, hold
        )
        if attention:
            time.sleep(attention)
        _move_human(tx, ty, rng, path_seconds=travel)
        time.sleep(hover)
        pyautogui.mouseDown(x=tx, y=ty, button=button)
        try:
            time.sleep(hold)
        finally:
            pyautogui.mouseUp(x=tx, y=ty, button=button)
    return {
        "ok": True,
        "x": tx,
        "y": ty,
        "timing": {
            "attention": round(attention, 3),
            "travel": round(travel, 3),
            "hover": round(hover, 3),
            "hold": round(hold, 3),
            "budget": round(motion.click_budget(dist), 3),
        },
    }


def _type_dispatch(method, keys_fn, paste_fn, text):
    """Pick the type path; returns (method_used, note)."""
    if method == "keys":
        return keys_fn(), None
    if method == "paste":
        try:
            return paste_fn(), None
        except Exception as exc:
            return keys_fn(), f"paste requested but unavailable ({exc}); used keys"
    if len(text) > 120 and _clipboard_backend() is not None:
        try:
            return paste_fn(), None
        except Exception as exc:
            return keys_fn(), f"paste failed ({exc}); used keys"
    return keys_fn(), None


@app.post("/type")
def type_text():
    """Type text: clipboard paste for long payloads, jittered keys else.

    {text, method?: "auto"|"paste"|"keys", jitter?: bool}

    Method (wishlist #9): "auto" pastes when the text is over 120 chars
    and a clipboard backend exists — the paste path saves the current
    clipboard and restores it afterwards; "paste" forces that path;
    "keys" forces keystrokes. Any clipboard failure falls back to
    keystrokes instead of failing the request (see "note").

    Jitter (wishlist #16): the keystroke path types per character with
    human-band delays from _plan_key_delays unless jitter is false —
    false restores the original single pyautogui.write(interval=0.02).

    Response: {"ok", "method_used", "chars", "elapsed_seconds",
    "delays_planned", "planned_seconds"} — the delay fields describe the
    keystroke plan (0 on the paste path).
    """
    refused = _yield_if_active()
    if refused is not None:
        return refused
    d = _json_body()
    text = d.get("text")
    if not isinstance(text, str):
        return jsonify({"ok": False, "error": "text must be a string"}), 400
    method = d.get("method", "auto")
    if method not in ("auto", "paste", "keys"):
        return jsonify({"ok": False, "error": "method must be auto, paste or keys"}), 400
    jitter = bool(d.get("jitter", True))
    _owner_monitor.ensure_started()  # no-op off Windows / once running
    started = time.monotonic()
    note = None
    delays = []

    typed = 0
    state = {"guard": None, "reason": None}

    def _keys():
        nonlocal delays, typed
        if jitter:
            delays = _plan_key_delays(text, random)
            # R4 (F7) re-checked the yield every 12 chars so a long
            # type gated only at the door cannot interleave relay
            # keystrokes with the owner's for the op's whole duration.
            # R7.2 (wishlist #27): that re-check consulted
            # _yield_if_active(), whose idle probe counts this type's
            # OWN injected keystrokes as session input — after 12 of
            # our own chars the idle read ~0 and the type aborted on
            # itself, owner or no owner. Track dwTime instead: sample
            # it after each keystroke (that stamp is ours by
            # construction) and flag owner input when a later sample
            # is strictly newer than the stamp our last keystroke
            # explains. The abort still lands on the same 12-char
            # boundaries and reports the same partial, so the response
            # contract is unchanged. Accepted blind spot: an owner
            # event inside the same tick as one of our keystrokes is
            # indistinguishable from that keystroke. Off Windows / on
            # probe failure dwTime is None and the re-check is inert,
            # like the door's.
            guard = _TypeOwnerGuard()
            state["guard"] = guard.mode
            for ch, delay in zip(text, delays):
                # R7.2 fix (Finding 3): sample FIRST, so owner input
                # during the sleep after char 12 stops char 13.
                guard.sample()
                if typed and typed % 12 == 0:
                    if guard.owner_seen:
                        state["reason"] = "owner"
                        break
                    # Finding 2: a lock with no new input moves no
                    # timestamp — check the lock itself, lock-only.
                    if _probe_locked() is True:
                        state["reason"] = "locked"
                        break
                pyautogui.write(ch)
                guard.after_write()
                time.sleep(delay)
                typed += 1
            state["guard"] = guard.mode
        else:
            pyautogui.write(text, interval=0.02)
            typed = len(text)
        return "keys"

    def _paste():
        backend = _clipboard_backend()
        if backend is None:
            raise RuntimeError("clipboard unavailable on this host")
        saved, readable = None, False
        try:
            saved = backend.get_text()
            readable = True
        except Exception:
            pass  # unreadable clipboard: paste anyway, skip the restore
        try:
            backend.set_text(text)
            pyautogui.hotkey("ctrl", "v")
            if readable:
                # Give the target app a beat to consume the paste before the
                # previous clipboard contents go back.
                time.sleep(0.1)
        finally:
            if readable and saved is not None:
                try:
                    backend.set_text(saved)
                except Exception:
                    pass
        return "paste"

    with _input("keys"):
        method_used, note = _type_dispatch(method, _keys, _paste, text)

    if method_used == "keys" and typed < len(text):
        return (
            jsonify(
                {
                    "ok": False,
                    # Deliberately NOT yield-classified (error_code is
                    # not "yield"/"locked", and the prose avoids the
                    # legacy substrings): some chars already landed, so
                    # a queued retry of the whole payload would
                    # double-type the prefix. The receipt settles
                    # failed with chars_typed; the caller re-submits
                    # text[chars_typed:].
                    "error": (
                        f"desktop locked — typing aborted at char {typed}"
                        if state["reason"] == "locked"
                        else f"owner became active — typing aborted at char {typed}"
                    ),
                    "error_code": "type_aborted",
                    "abort_reason": state["reason"],
                    "owner_guard": state["guard"],
                    "aborted": True,
                    "chars_typed": typed,
                    "chars": len(text),
                    "method_used": "keys",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "delays_planned": len(delays),
                    "planned_seconds": sum(delays[:typed]),
                }
            ),
            409,
        )
    resp = {
        "ok": True,
        "method_used": method_used,
        "owner_guard": state["guard"],
        "chars": len(text),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "delays_planned": len(delays),
        "planned_seconds": sum(delays),
    }
    if note:
        resp["note"] = note
    return jsonify(resp)
@app.post("/key")
def key():
    d = _json_body()
    name = d.get("key")
    if not isinstance(name, str) or not name.strip():
        return _bad_request("key must be a non-empty string")
    with _input("keys"):
        pyautogui.press(name)
    return {"ok": True}


@app.post("/scroll")
def scroll():
    """Scroll by clicks. Human mode (R9 #29): variable 1-3 click chunks
    with 30-120 ms pauses instead of one jump; the total is exact."""
    d = _json_body()
    try:
        clicks = int(d.get("clicks", 3))
    except (TypeError, ValueError, OverflowError):
        return _bad_request("clicks must be an integer")
    with _input("mouse"):
        if _human(d):
            for chunk, pause in motion.plan_scroll(clicks, random):
                pyautogui.scroll(chunk)
                if pause:
                    time.sleep(pause)
        else:
            pyautogui.scroll(clicks)
    return {"ok": True}


# ---- window titles (wishlist #5) ------------------------------------------

@app.get("/window_titles")
def window_titles():
    try:
        titles = pyautogui.getAllTitles()
    except Exception:
        titles = []
    return {"ok": True, "titles": list(titles) if titles else []}


def _process_snapshot(pid):
    """Read-only Windows process probe; access errors are NOT process exit.

    Creation time identifies the process across PID reuse. Wait on the same
    opened handle used to obtain that identity; never query two PID instances.
    """
    if os.name != "nt":
        raise NotImplementedError("process probing requires Windows")
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
        ctypes.POINTER(wintypes.FILETIME)
    ] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000 | 0x1000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: no such PID
            return {"running": False, "instance": None}
        raise OSError(error, "OpenProcess failed")
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            raise OSError(ctypes.get_last_error(), "GetProcessTimes failed")
        result = kernel.WaitForSingleObject(handle, 0)
        if result not in (0, 258):  # WAIT_OBJECT_0 / WAIT_TIMEOUT
            raise OSError(ctypes.get_last_error(), "process wait failed")
        created = (int(times[0].dwHighDateTime) << 32) | int(times[0].dwLowDateTime)
        return {"running": result == 258, "instance": str(created)}
    finally:
        kernel.CloseHandle(handle)


@app.get("/process/<int:pid>")
def process_state(pid):
    """Authenticated process-exit observation; no process control."""
    if not 0 < pid <= 0xFFFFFFFF:
        return jsonify({"ok": False, "error": "pid must be a positive DWORD"}), 400
    try:
        snapshot = _process_snapshot(pid)
    except NotImplementedError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 501
    except OSError:
        return jsonify({"ok": False, "error": "process probe failed"}), 502
    return jsonify({"ok": True, "pid": pid, **snapshot})


# ---- lock / idle state (wishlist #13) --------------------------------------

def _probe_dwtime():
    """The session's last-input timestamp (ms tick), or None.

    Tests monkeypatch THIS function (the seam, mirroring
    _probe_lock_state); the raw Windows call is _probe_dwtime_raw.
    """
    return _probe_dwtime_raw()


def _probe_dwtime_raw():
    """dwTime from GetLastInputInfo on Windows; None elsewhere/failure."""
    return _last_input_raw()[0]


def _tick_ms():
    """GetTickCount (the clock dwTime lives on), or None off Windows.
    Seam: tests drive it with the same virtual clock as _probe_dwtime."""
    if os.name != "nt":
        return None
    try:
        return int(ctypes.windll.kernel32.GetTickCount()) & 0xFFFFFFFF  # type: ignore[attr-defined]
    except Exception:
        return None


# ---- owner-input provenance (R7.2 fix, Finding 1) --------------------------
# Timestamps cannot say WHO produced an input (Microsoft: dwTime need not
# even increase, and injected input moves it too). Low-level keyboard and
# mouse hooks can: every event carries an INJECTED flag, and all relay
# input goes through SendInput (pyautogui), so it is flagged. The monitor
# counts non-injected (owner) events and injected (ours) events; the type
# guard compares counters, so an owner event that lands INSIDE one of our
# write() calls is still the owner's. The hook thread runs its own
# message loop; callbacks only bump counters (Windows drops slow hooks).

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01


# Low-level hook payloads, built once at import (same reason as the L1
# fix: POINTER() on a per-call class pins that class forever). c_ulong is
# Windows' DWORD; these layouts are only ever read on Windows.
class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", ctypes.c_ulong), ("scanCode", ctypes.c_ulong),
                ("flags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_size_t)]


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("flags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


_P_KBDLLHOOKSTRUCT = ctypes.POINTER(_KBDLLHOOKSTRUCT)
_P_MSLLHOOKSTRUCT = ctypes.POINTER(_MSLLHOOKSTRUCT)
_WM_QUIT = 0x0012


class _OwnerInputMonitor:
    # Review I5: a hook thread that ended (Windows silently drops slow
    # low-level hooks; the message loop can exit) is restarted on a later
    # /type, at most once per RESTART_BACKOFF. It used to stay dead for the
    # life of the process, leaving the type guard on the dwTime fallback.
    RESTART_BACKOFF = 60.0

    def __init__(self):
        self.owner = 0
        self.injected = 0
        self.running = False
        self._thread = None
        self._thread_id = None
        self._started_at = None
        self._stopped = False
        self._ready = threading.Event()
        self._start_lock = threading.Lock()

    def snapshot(self):
        """(owner_events, injected_events), or None when not running."""
        if not self.running:
            return None
        return (self.owner, self.injected)

    def ensure_started(self, wait=0.5):
        if os.name != "nt":
            return False
        with self._start_lock:
            if self._stopped:
                return False
            alive = self._thread is not None and self._thread.is_alive()
            due = (
                self._started_at is None
                or time.monotonic() - self._started_at >= self.RESTART_BACKOFF
            )
            if not alive and due:
                self._ready.clear()
                self._started_at = time.monotonic()
                self._thread = threading.Thread(
                    target=self._run, name="hands-input-hooks", daemon=True
                )
                self._thread.start()
        self._ready.wait(wait)
        return self.running

    def stop(self, timeout=2.0):
        """Unhook and end the hook thread (review I5/L3).

        Called on Stop and before the packaged app replaces this module,
        so retired hooks stop running a Python callback for every input
        event on the PC. A stopped monitor never restarts.
        """
        with self._start_lock:
            self._stopped = True
            thread, tid = self._thread, self._thread_id
        if thread is None or not thread.is_alive():
            return
        if tid:
            try:
                ctypes.windll.user32.PostThreadMessageW(tid, _WM_QUIT, 0, 0)  # type: ignore[attr-defined]
            except Exception:
                pass
        thread.join(timeout)

    def _run(self):
        user32 = None
        hooks = []
        try:
            from ctypes import wintypes

            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            self._thread_id = int(kernel32.GetCurrentThreadId())
            LRESULT = ctypes.c_ssize_t
            HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
            user32.CallNextHookEx.restype = LRESULT
            user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
            user32.UnhookWindowsHookEx.restype = wintypes.BOOL
            user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE

            def _kbd(code, wparam, lparam):
                if code >= 0:
                    flags = ctypes.cast(lparam, _P_KBDLLHOOKSTRUCT).contents.flags
                    if flags & LLKHF_INJECTED:
                        self.injected += 1
                    else:
                        self.owner += 1
                return user32.CallNextHookEx(None, code, wparam, lparam)

            def _mouse(code, wparam, lparam):
                if code >= 0:
                    flags = ctypes.cast(lparam, _P_MSLLHOOKSTRUCT).contents.flags
                    if flags & LLMHF_INJECTED:
                        self.injected += 1
                    else:
                        self.owner += 1
                return user32.CallNextHookEx(None, code, wparam, lparam)

            self._procs = (HOOKPROC(_kbd), HOOKPROC(_mouse))  # keep alive
            hmod = kernel32.GetModuleHandleW(None)
            hk = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._procs[0], hmod, 0)
            if hk:
                hooks.append(hk)
            hm = user32.SetWindowsHookExW(WH_MOUSE_LL, self._procs[1], hmod, 0)
            if hm:
                hooks.append(hm)
            if not hk or not hm:
                file_log("owner-input hooks unavailable; type guard uses dwTime")
                return
            if self._stopped:  # stop() raced the start: unhook right away
                return
            self.running = True
            self._ready.set()
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            file_log(f"owner-input hooks failed: {exc}")
        finally:
            for hook in hooks:  # review I5: hooks used to stay installed forever
                try:
                    user32.UnhookWindowsHookEx(hook)
                except Exception:
                    pass
            self.running = False
            self._ready.set()


_owner_monitor = _OwnerInputMonitor()


def _owner_input_snapshot():
    """(owner_events, injected_events) from the hooks, or None. Seam."""
    return _owner_monitor.snapshot()


# Our own keystroke may publish its dwTime AFTER the post-write probe
# (Finding 5). In the dwTime fallback, an advance stamped no later than
# this many ms after our write returned is attributed to that write.
_DW_PUBLISH_GRACE_MS = 30


class _TypeOwnerGuard:
    """Owner detection for one keystroke type (R7.2 fix).

    mode "hook": counts non-injected events since the type began —
      provenance, not timestamps (Finding 1). If the hooks see none of
      our own injected keystrokes after two writes, they are presumed
      dead (Windows silently removes slow LL hooks) and the guard drops
      to "dwtime" for the rest of the op.
    mode "dwtime": the R7.2 timestamp mechanism, kept only as the
      fallback, with three fixes: a failed post-write probe INVALIDATES
      the baseline instead of leaving it stale (Grok: a stale baseline
      read our next keystroke as the owner -> self-abort at char 12);
      an advance stamped within the publish grace of our write is ours
      (Finding 5, delayed publication); backward stamps never count.
      Residual blind window (documented, Finding 1): owner input inside
      a write or its grace is indistinguishable from it.
    mode "none": no probe at all (off Windows) — inert, like the door.
    """

    def __init__(self):
        self.owner_seen = False
        self.writes = 0
        snap = _owner_input_snapshot()
        if snap is not None:
            self.mode = "hook"
            self.base_owner, self.base_injected = snap
        else:
            self._init_dwtime()

    def _init_dwtime(self):
        self.self_dw = _probe_dwtime()
        self.write_end_tick = None
        self.mode = "dwtime" if self.self_dw is not None else "none"

    def sample(self):
        """Fresh owner sample — call BEFORE any boundary decision (F3)."""
        if self.owner_seen:
            return True
        if self.mode == "hook":
            snap = _owner_input_snapshot()
            if snap is None:  # hook thread died mid-op
                self._init_dwtime()
                return self.owner_seen
            if snap[0] != self.base_owner:
                self.owner_seen = True
            elif self.writes >= 2 and snap[1] == self.base_injected:
                self._init_dwtime()  # hooks deaf to our own input: degrade
            return self.owner_seen
        if self.mode == "dwtime" and self.self_dw is not None:
            dw = _probe_dwtime()
            if dw is not None and _dw_advanced(dw, self.self_dw):
                ours = (
                    self.write_end_tick is not None
                    and not _dw_advanced(dw, (self.write_end_tick + _DW_PUBLISH_GRACE_MS) & 0xFFFFFFFF)
                )
                if ours:
                    self.self_dw = dw  # late-published stamp of our write
                else:
                    self.owner_seen = True
        return self.owner_seen

    def after_write(self):
        self.writes += 1
        if self.mode in ("dwtime", "none"):
            dw = _probe_dwtime()
            self.write_end_tick = _tick_ms()
            if dw is None:
                self.self_dw = None  # unknown baseline never reads as owner
            else:
                if self.mode == "none":
                    self.mode = "dwtime"
                self.self_dw = dw


def _dw_advanced(dw, baseline):
    """True when last-input tick `dw` is strictly newer than `baseline`.

    dwTime is a 32-bit millisecond tick (wraps ~49.7 days); the
    modular difference reads advances under half the range as newer
    and anything else as older / wrapped noise.
    """
    return 0 < ((int(dw) - int(baseline)) & 0xFFFFFFFF) < 0x80000000


# ---- probe health (R5, F10) -------------------------------------------
# The yield trusts the lock/idle probe completely: a (None, None)
# result reads as "owner inactive", so a silently broken probe (a
# Windows change, a permissions shift, a bad cast) disables the
# owner's override with no signal anywhere. Count consecutive FULL
# failures on Windows; /state reports probe_ok so the degradation is
# a visible state (it rides to the gateway's /v1/info pc_state), and
# the third consecutive failure logs a line to the app window — the
# channel the owner actually sees. Whether the yield should fail
# CLOSED on a degraded probe is the owner's policy call; silently
# failing open was the one option that had to go.
_probe_failures = 0
_probe_degraded_logged = False


def _probe_lock_state():
    """(locked, idle_seconds) with probe-health accounting (R5, F10).

    Tests monkeypatch THIS function (the seam); the raw Windows probe
    is _probe_lock_state_raw.
    """
    global _probe_failures, _probe_degraded_logged
    result = _probe_lock_state_raw()
    if os.name == "nt":
        locked, idle_seconds = result
        if locked is None and idle_seconds is None:
            _probe_failures += 1
            if _probe_failures >= 3 and not _probe_degraded_logged:
                _probe_degraded_logged = True
                log_line(
                    "YIELD SENSOR DEGRADED: the lock/idle probe has "
                    "failed 3 times in a row - act yields cannot be "
                    "trusted until it recovers"
                )
                file_log("probe degraded: 3 consecutive lock/idle probe failures")
        else:
            _probe_failures = 0
            _probe_degraded_logged = False
    return result


def _probe_health():
    return {"probe_failures": _probe_failures, "probe_ok": _probe_failures < 3}


# ---- Win32 struct types, built ONCE at import (memory-leak fix) ----------
# ctypes.POINTER(cls) stores every pointer type it creates in a
# process-global cache that is never pruned (CPython <= 3.13), and that
# entry keeps `cls` alive. _probe_display_raw used to define its GUID
# struct inside the function and call POINTER() on it, so every GET
# /state -- i.e. every gateway /v1/info refresh, ~every 5 s in the
# packaged app -- leaked a fresh struct class plus its LP_ type (~6 KB),
# for the life of the process. Defined here, POINTER() is a cache hit.
# Pure type definitions: nothing calls into Win32 at import, so this is
# safe off Windows too.


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]


class _GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort),
                ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


_PGUID = ctypes.POINTER(_GUID)


def _make_guid(d1, d2, d3, d4):
    return _GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*d4))


# GUID_VIDEO_SUBGROUP / VIDEOIDLE (display-off timeout). Read-only inputs.
_GUID_VIDEO_SUBGROUP = _make_guid(
    0x7516B95F, 0xF776, 0x4464, [0x8C, 0x53, 0x06, 0x16, 0x7F, 0x40, 0xCC, 0x99]
)
_GUID_VIDEOIDLE = _make_guid(
    0x3C0BC021, 0xC8A8, 0x4E07, [0xA9, 0x73, 0x6B, 0x14, 0xCB, 0xCB, 0x2B, 0x7E]
)


def _last_input_raw():
    """(dwTime, now_tick) from GetLastInputInfo + GetTickCount, the ONE
    low-level last-input acquisition (R7.2 fix, Finding 6). Either value
    is None off Windows or on failure; never raises."""
    if os.name != "nt":
        return (None, None)
    dw = tick = None
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if user32.GetLastInputInfo(ctypes.byref(info)):
            dw = int(info.dwTime) & 0xFFFFFFFF
            tick = int(kernel32.GetTickCount()) & 0xFFFFFFFF
    except Exception:
        pass
    return (dw, tick)


def _probe_locked_raw():
    """True when the input desktop is Winlogon (locked); None off
    Windows / on failure. Independent of the last-input probe."""
    if os.name != "nt":
        return None
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        handle = user32.OpenInputDesktop(0, False, 0x0100)
        if not handle:
            return None
        try:
            buf = ctypes.create_unicode_buffer(256)
            needed = ctypes.c_ulong(0)
            if user32.GetUserObjectInformationW(
                handle, 2, buf, ctypes.sizeof(buf), ctypes.byref(needed)
            ):
                return buf.value.lower() == "winlogon"
        finally:
            try:
                user32.CloseDesktop(handle)
            except Exception:
                pass
    except Exception:
        pass
    return None


def _probe_lock_state_raw():
    """Probe (locked, idle_seconds) on Windows; (None, None) elsewhere.

    Never raises: any failure returns None for the affected value. The
    Windows calls live here so tests can monkeypatch _probe_lock_state.
    """
    if os.name != "nt":
        return (None, None)
    locked = _probe_locked_raw()
    dw, tick = _last_input_raw()
    idle_seconds = None
    if dw is not None and tick is not None:
        idle_seconds = ((tick - dw) & 0xFFFFFFFF) / 1000.0
    return (locked, idle_seconds)


def _probe_display_raw():
    """Probe display-sleep signals on Windows (wishlist #13); Nones elsewhere.

    screensaver: SPI_GETSCREENSAVERRUNNING. display_off_after_seconds:
    the active power scheme's display-off timeout for the current power
    source (AC/DC), so a reader can compare it to idle_seconds. Never
    raises; any failure returns None for that value.
    """
    out = {"screensaver": None, "display_off_after_seconds": None}
    if os.name != "nt":
        return out
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        running = ctypes.c_int(0)
        if user32.SystemParametersInfoW(0x0072, 0, ctypes.byref(running), 0):
            out["screensaver"] = bool(running.value)
    except Exception:
        pass
    try:
        powrprof = ctypes.windll.powrprof  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        sps = _SYSTEM_POWER_STATUS()
        on_dc = bool(kernel32.GetSystemPowerStatus(ctypes.byref(sps))) and sps.ACLineStatus == 0
        scheme = _PGUID()  # module-level pointer type: nothing new is cached per call
        if powrprof.PowerGetActiveScheme(None, ctypes.byref(scheme)) == 0:
            try:
                value = ctypes.c_ulong(0)
                read = powrprof.PowerReadDCValueIndex if on_dc else powrprof.PowerReadACValueIndex
                if read(None, scheme, ctypes.byref(_GUID_VIDEO_SUBGROUP),
                        ctypes.byref(_GUID_VIDEOIDLE), ctypes.byref(value)) == 0:
                    out["display_off_after_seconds"] = int(value.value)  # 0 = never
            finally:
                kernel32.LocalFree(scheme)
    except Exception:
        pass
    return out


def _display_state(idle_seconds):
    """Combine the raw display probe with idle time into /state fields."""
    try:
        raw = _probe_display_raw()
    except Exception:
        raw = {"screensaver": None, "display_off_after_seconds": None}
    timeout = raw.get("display_off_after_seconds")
    asleep = None
    if timeout is not None and idle_seconds is not None:
        asleep = bool(timeout) and idle_seconds >= timeout
    return {"screensaver": raw.get("screensaver"),
            "display_off_after_seconds": timeout,
            "display_asleep": asleep}


# ---- R11 resting hand (replaces the R9 idle jiggler) ----------------------
# While the agent HOLDS hands control and is idle between acts, the pointer
# floats like a hand resting on the mouse: a 1-3 px local float within
# motion.REST_RADIUS_PX of where it rests. Intensity ramps 0 -> full over
# motion.REST_RAMP_S from the end of each act (motion.rest_intensity); any
# act resets it. No toggle, no timeout. The gateway pushes the claim state
# here (/resting/claim) since only it knows about claims; the lease itself
# is untouched. Owner input ends the episode at once (OS input newer than
# the episode baseline outside our own ledger spans, or the pointer not
# where we left it) and the hand stays still until the next agent act.
# Never moves while locked, while the probe is blind, or during an act.

class _RestingHand:
    POLL = 0.1

    def __init__(self, rng=None, clock=time.monotonic):
        self.rng = rng if rng is not None else random
        self._clock = clock
        self._lock = threading.Lock()
        self.claim_until = None  # monotonic expiry of the held claim
        self.claim_seq = -1
        self.last_act_end = clock()
        self.acting = 0
        self.owner_stopped = False
        self.anchor = None
        self.offset = (0.0, 0.0)
        self.last_pos = None
        self.baseline_tick = None
        self.next_at = 0.0
        self.moves = 0
        self.kills = 0
        self._stop = threading.Event()
        self._thread = None

    # -- inputs ------------------------------------------------------------
    def act_begin(self):
        with self._lock:
            self.acting += 1
            self._end()

    def act_end(self, now):
        with self._lock:
            self.acting = max(0, self.acting - 1)
            self.last_act_end = now
            self.owner_stopped = False
            self._end()

    def set_claim(self, held, expires_in, seq, now):
        """Gateway claim push. Stale (out-of-order) pushes are ignored."""
        with self._lock:
            if seq <= self.claim_seq:
                return False
            self.claim_seq = seq
            was_held = self.claim_held(now)
            if held and expires_in > 0:
                self.claim_until = now + float(expires_in)
                if not was_held:
                    self.last_act_end = now  # fresh claim: ramp from grant
                    self.owner_stopped = False
            else:
                self.claim_until = None
                self._end()
            return True

    def claim_held(self, now):
        return self.claim_until is not None and now < self.claim_until

    def intensity(self, now):
        if self.acting or not self.claim_held(now):
            return 0.0
        return motion.rest_intensity(now - self.last_act_end)

    # -- episode -------------------------------------------------------------
    def _end(self, killed=False):
        if killed and self.anchor is not None:
            self.kills += 1
            self.owner_stopped = True
        self.anchor = None
        self.offset = (0.0, 0.0)
        self.last_pos = None
        self.baseline_tick = None

    def status(self):
        now = self._clock()
        with self._lock:
            return {
                "active": self.anchor is not None and self.claim_held(now) and not self.acting,
                "claim_held": self.claim_held(now),
                "intensity": round(self.intensity(now), 3),
                "moves": self.moves,
                "owner_kills": self.kills,
            }

    def step(self, now):
        """One decision. Returns seconds to wait before the next step."""
        with self._lock:
            if not self.claim_held(now):
                self._end()
                return 0.5
            if self.acting or self.owner_stopped:
                return self.POLL
        locked, raw_idle = _probe_lock_state()
        now_tick = _tick_ms()
        with self._lock:
            if locked is not False or raw_idle is None:
                self._end()
                return 1.0
            pos = tuple(pyautogui.position())
            if self.anchor is None:
                self.anchor = pos
                self.last_pos = pos
                self.offset = (0.0, 0.0)
                self.baseline_tick = now_tick
                self.next_at = now
                return self.POLL
            if _LEDGER.owner_input_since(self.baseline_tick, raw_idle, now_tick) or pos != self.last_pos:
                self._end(killed=True)  # the owner's hand is on the mouse
                return self.POLL
            if now < self.next_at:
                return min(self.POLL, self.next_at - now)
            offset, wait = motion.plan_rest(self.offset, self.intensity(now), self.rng)
            self.next_at = now + wait
            target = (int(round(self.anchor[0] + offset[0])), int(round(self.anchor[1] + offset[1])))
            self.offset = offset
            if target == pos:
                return self.POLL
        if not _INPUT.try_acquire():
            return self.POLL  # an act is injecting; never contend with it
        try:
            landed = self._move(target)
        finally:
            _INPUT.release("mouse")
        with self._lock:
            if landed is None:
                self._end(killed=True)
                return self.POLL
            if self.anchor is not None:
                self.last_pos = landed
                self.moves += 1
        return self.POLL

    def _move(self, target):
        """Inject one float step; None if the owner moved the pointer."""
        t0 = _tick_ms()
        # Record BEFORE injecting: a concurrent yield probe must already
        # see this span as ours.
        _LEDGER.record(t0, (t0 + 250) & 0xFFFFFFFF)
        pyautogui.moveTo(target[0], target[1], duration=0)
        px, py = pyautogui.position()
        _LEDGER.truncate_last((_tick_ms() + 50) & 0xFFFFFFFF)
        if abs(px - target[0]) > 1 or abs(py - target[1]) > 1:
            return None
        return (px, py)

    def _run(self):
        while not self._stop.is_set():
            try:
                wait = self.step(self._clock())
            except Exception as exc:  # a probe/pyautogui failure must not kill the thread
                file_log(f"resting hand step failed: {exc}")
                with self._lock:
                    self._end()
                wait = 2.0
            self._stop.wait(wait)

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hands-resting", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self._lock:
            self._end()


_resting = _RestingHand()


def start_resting_hand():
    """Start the resting-hand thread (it idles unless a claim is held)."""
    _resting.start()


def stop_resting_hand():
    _resting.stop()


def stop_background():
    """Stop this module's background threads (review L3): the resting hand
    and the owner-input hooks. The packaged app calls this before it
    replaces the module (self-heal) and on Stop; before, the old module's
    hook thread -- and its system-wide hooks -- outlived every restart."""
    stop_resting_hand()
    _owner_monitor.stop()


@app.get("/resting")
def resting_get():
    return {"ok": True, **_resting.status()}


@app.post("/resting/claim")
def resting_claim():
    """Gateway push: {held: bool, expires_in: seconds, seq: int}."""
    d = _json_body()
    held, seq = d.get("held"), d.get("seq")
    expires_in = d.get("expires_in", 0)
    if (
        not isinstance(held, bool)
        or not isinstance(seq, int) or isinstance(seq, bool)
        or not isinstance(expires_in, (int, float)) or isinstance(expires_in, bool)
        or not math.isfinite(expires_in)
    ):
        return jsonify({"ok": False, "error": "need held (bool), seq (int), expires_in (number)"}), 400
    applied = _resting.set_claim(held, float(expires_in), seq, time.monotonic())
    if held:
        start_resting_hand()
    return {"ok": True, "applied": applied, **_resting.status()}


def _probe_locked():
    """Lock-only check for the mid-type boundary (R7.2 fix, Finding 2).

    The seam tests monkeypatch. Deliberately NOT the idle half of the
    yield probe: idle counts our own keystrokes (the #27 self-abort).
    """
    return _probe_locked_raw()


@app.get("/state")
def state():
    platform = "windows" if os.name == "nt" else "other"
    try:
        locked, idle_seconds = _probe_lock_state()
    except Exception:
        locked, idle_seconds = None, None
    body = {"ok": True, "platform": platform, "locked": locked, "idle_seconds": idle_seconds}
    body["owner_idle_seconds"] = _LEDGER.owner_idle(idle_seconds, _tick_ms())
    body["resting_hand"] = _resting.status()
    body["human_motion"] = _human_motion_default()
    # Display sleep follows the OS idle clock, which injected input
    # (incl. the resting hand) does reset — so the raw idle is the right one.
    body.update(_display_state(idle_seconds))
    body.update(_probe_health())
    return body


# ---- file-drop (wishlist #2) ------------------------------------------------
# Allowlist: folders from the hands config file key "filedrop_dirs" (list
# of absolute paths) OR env HANDS_FILEDROP_DIRS (os.pathsep-separated) —
# env wins if set. Default empty -> every filedrop endpoint returns 501.
# File contents are never logged.

_FILEDROP_CHUNK_MAX = 524288
_FILEDROP_MAX_SIZE = 16 * 1024 * 1024
_FILEDROP_TTL = 600  # uploads older than 10 minutes are purged lazily
# Review L4: how much a client may hold open at once. Each upload buffers
# up to 16 MB in memory until commit/abort/expiry.
_FILEDROP_MAX_OPEN = 4
_FILEDROP_MAX_BUFFERED = 64 * 1024 * 1024  # total declared size of open uploads

_uploads: dict = {}
_uploads_lock = threading.Lock()


def _accept_drop_dir(p):
    """Absolute allowlist entries only, compared case-insensitively later.

    Relative entries are rejected (not realpath'd against the cwd).
    Exact path match is unchanged — this is not a prefix match.
    """
    text = str(p).strip()
    if not text or not os.path.isabs(text):
        return None
    return os.path.normcase(os.path.realpath(text))


def _filedrop_dirs():
    env = os.environ.get("HANDS_FILEDROP_DIRS")
    if env is not None:
        if not env.strip():
            return []
        return [d for d in (_accept_drop_dir(p) for p in env.split(os.pathsep)) if d]
    cfg = _load_config()
    dirs = cfg.get("filedrop_dirs") if isinstance(cfg, dict) else None
    if not isinstance(dirs, list):
        return []
    return [d for d in (_accept_drop_dir(p) for p in dirs) if d]


def _purge_uploads():
    now = time.time()
    with _uploads_lock:
        stale = [uid for uid, up in _uploads.items() if now - up["created_at"] > _FILEDROP_TTL]
        for uid in stale:
            del _uploads[uid]


def _filedrop_gate():
    """Shared preamble: purge stale uploads, enforce configuration."""
    _purge_uploads()
    if not _filedrop_dirs():
        return (jsonify({"ok": False, "error": "file-drop not configured on the hands"}), 501)
    return None


@app.post("/filedrop/begin")
def filedrop_begin():
    gate_resp = _filedrop_gate()
    if gate_resp is not None:
        return gate_resp
    body = _json_body()
    folder = body.get("folder")
    filename = body.get("filename")
    size = body.get("size")
    sha256 = body.get("sha256")
    if not isinstance(folder, str) or not folder:
        return jsonify({"ok": False, "error": "folder required"}), 400
    real_folder = os.path.normcase(os.path.realpath(folder))
    if real_folder not in _filedrop_dirs():
        return jsonify({"ok": False, "error": "folder not in file-drop allowlist"}), 403
    if (
        not isinstance(filename, str)
        or not filename
        or "/" in filename
        or "\\" in filename
        or ".." in filename
    ):
        return jsonify({"ok": False, "error": "filename must be a bare name"}), 400
    if isinstance(size, bool) or not isinstance(size, int) or size < 1 or size > _FILEDROP_MAX_SIZE:
        return jsonify({"ok": False, "error": "size must be an integer in 1..16MB"}), 400
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
        return jsonify({"ok": False, "error": "sha256 must be a 64-hex string"}), 400
    upload_id = uuid.uuid4().hex
    with _uploads_lock:
        if (
            len(_uploads) >= _FILEDROP_MAX_OPEN
            or sum(up["size"] for up in _uploads.values()) + size > _FILEDROP_MAX_BUFFERED
        ):
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "file-drop busy: too many uploads in progress "
                        "(finish or abort one, or wait for it to expire)",
                    }
                ),
                503,
            )
        _uploads[upload_id] = {
            "id": upload_id,
            "dir": real_folder,
            "filename": filename,
            "size": size,
            "sha256": sha256.lower(),
            "buffer": bytearray(),
            "created_at": time.time(),
        }
    return jsonify({"ok": True, "upload_id": upload_id, "chunk_max": _FILEDROP_CHUNK_MAX})


@app.post("/filedrop/chunk")
def filedrop_chunk():
    gate_resp = _filedrop_gate()
    if gate_resp is not None:
        return gate_resp
    body = _json_body()
    upload_id = body.get("upload_id")
    offset = body.get("offset")
    data_b64 = body.get("data_b64")
    with _uploads_lock:
        upload = _uploads.get(upload_id)
        if upload is None or upload.get("closed"):
            return jsonify({"ok": False, "error": "unknown upload_id"}), 404
        expected = len(upload["buffer"])
        if isinstance(offset, bool) or not isinstance(offset, int) or offset != expected:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "offset mismatch",
                        "expected_offset": expected,
                        "expected": expected,
                    }
                ),
                409,
            )
        if not isinstance(data_b64, str):
            return jsonify({"ok": False, "error": "data_b64 required"}), 400
        try:
            chunk = base64.b64decode(data_b64, validate=True)
        except (binascii.Error, ValueError):
            return jsonify({"ok": False, "error": "data_b64 is not valid base64"}), 400
        if expected + len(chunk) > upload["size"]:
            return jsonify({"ok": False, "error": "chunk exceeds declared size"}), 400
        upload["buffer"].extend(chunk)
        received = len(upload["buffer"])
    return jsonify({"ok": True, "received": received, "offset": received})


@app.post("/filedrop/commit")
def filedrop_commit():
    gate_resp = _filedrop_gate()
    if gate_resp is not None:
        return gate_resp
    body = _json_body()
    upload_id = body.get("upload_id")
    with _uploads_lock:
        upload = _uploads.get(upload_id)
        if upload is None or upload.get("closed"):
            return jsonify({"ok": False, "error": "unknown upload_id"}), 404
        # Close under the same lock as chunk append so a concurrent chunk
        # cannot extend a buffer this commit already copied.
        data = bytes(upload["buffer"])
        meta_size = upload["size"]
        meta_sha = upload["sha256"]
        meta_dir = upload["dir"]
        meta_name = upload["filename"]
        upload["closed"] = True
    if len(data) != meta_size:
        with _uploads_lock:
            _uploads.pop(upload_id, None)
        return jsonify({"ok": False, "error": "incomplete upload: size mismatch"}), 400
    digest = hashlib.sha256(data).hexdigest()
    if digest != meta_sha:
        with _uploads_lock:
            _uploads.pop(upload_id, None)
        return jsonify({"ok": False, "error": "sha256 mismatch"}), 400
    final_path = os.path.join(meta_dir, meta_name)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=meta_dir, prefix=".filedrop-", suffix=".tmp")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_path, final_path)
        tmp_path = None
    except OSError as exc:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        with _uploads_lock:
            upload = _uploads.get(upload_id)
            if upload is not None:
                upload["closed"] = False
        return jsonify({"ok": False, "error": f"write failed: {exc}"}), 500
    with _uploads_lock:
        _uploads.pop(upload_id, None)
    return jsonify({"ok": True, "path": final_path, "size": len(data), "sha256": digest})


@app.post("/filedrop/abort")
def filedrop_abort():
    gate_resp = _filedrop_gate()
    if gate_resp is not None:
        return gate_resp
    body = _json_body()
    upload_id = body.get("upload_id")
    with _uploads_lock:
        existed = _uploads.pop(upload_id, None) is not None
    if not existed:
        return jsonify({"ok": False, "error": "unknown upload_id"}), 404
    return jsonify({"ok": True})
# ------------------------------------------------- V3-P4: chords + break

@app.post("/hotkey")
def hotkey():
    """Press a true key chord, e.g. {"keys": ["ctrl", "l"]}."""
    d = _json_body()
    keys = d.get("keys")
    if (
        not isinstance(keys, list)
        or not keys
        or not all(isinstance(k, str) and k.strip() for k in keys)
    ):
        return jsonify({"ok": False, "error": "keys must be a non-empty list of strings"}), 400
    with _input("keys"):
        pyautogui.hotkey(*keys)
    return {"ok": True}


@app.post("/break")
def break_():
    """Send Ctrl+C as a true chord, 1..3 times (default 1), 0.15s apart.

    Root cause this fixes: the /key op calls pyautogui.press() with ONE
    key name, so sending the string "ctrl+c" to /key was never a real
    chord — press() treats it as a single (nonexistent) key name and
    nothing was interrupted. A long-running / polling foreground process
    therefore could not be stopped through the relay. /break (and
    /hotkey) use pyautogui.hotkey(), which presses the keys together,
    so the interrupt actually lands.
    """
    refused = _yield_if_active()
    if refused is not None:
        return refused
    d = _json_body()
    times = d.get("times", 1)
    if isinstance(times, bool) or not isinstance(times, int) or not 1 <= times <= 3:
        return jsonify({"ok": False, "error": "times must be an integer 1..3"}), 400
    with _input("keys"):
        for i in range(times):
            pyautogui.hotkey("ctrl", "c")
            if i < times - 1:
                time.sleep(0.15)
    return {"ok": True, "times": times}


# ------------------------------------------------------- V3-P4: dragging

@app.post("/drag")
def drag():
    """Press the mouse button at (x1, y1), drag to (x2, y2), release."""
    refused = _yield_if_active()
    if refused is not None:
        return refused
    d = _json_body()
    try:
        x1, y1, x2, y2 = (int(d[k]) for k in ("x1", "y1", "x2", "y2"))
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "x1, y1, x2, y2 must be integers"}), 400
    button = d.get("button", "left")
    if button not in ("left", "middle", "right"):
        return jsonify({"ok": False, "error": "button must be left, middle or right"}), 400
    try:
        duration = float(d["duration"]) if d.get("duration") is not None else None
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "duration must be a number"}), 400
    human = _human(d)
    with _input("mouse"):
        if human:
            # R9 (#24): human approach to the grab point, a hover, then a
            # human path while held; duration (if given) is the held
            # travel time, else Fitts decides. Endpoints are exact.
            _move_human(x1, y1)
            time.sleep(motion.hover_delay(random))
        else:
            pyautogui.moveTo(x1, y1)
        down = False
        try:
            # Mark first so a mouseDown/move failure still releases the button.
            # duration 0 is real; do not coerce it with `or 0.4`.
            down = True
            pyautogui.mouseDown(button=button)
            if human:
                time.sleep(motion.click_hold(random))
                _move_human(x2, y2, path_seconds=duration)
            else:
                pyautogui.moveTo(x2, y2, duration=0.4 if duration is None else duration)
        finally:
            if down:
                pyautogui.mouseUp(button=button)
    return {"ok": True}


# ------------------------------------------- V3-P4: windows + focus (#3)

def _monitor_for_bounds(bounds, rects=None):
    """Index of the monitor rect containing the window's centre, else None."""
    if rects is None:
        rects = _monitor_rects()
    cx = bounds["x"] + bounds["w"] / 2
    cy = bounds["y"] + bounds["h"] / 2
    for i, r in enumerate(rects):
        if r["x"] <= cx < r["x"] + r["w"] and r["y"] <= cy < r["y"] + r["h"]:
            return i
    return None


_PSUTIL = None  # the psutil module, False when unavailable, None = not tried yet


def _psutil():
    """psutil, imported once (review I2). A missing psutil -- no build script
    installs it -- used to be re-imported, and fail, for EVERY window on
    every enumeration; a failed import is not cached by Python."""
    global _PSUTIL
    if _PSUTIL is None:
        try:
            import psutil

            _PSUTIL = psutil
        except Exception:
            _PSUTIL = False
    return _PSUTIL or None


def _process_name(pid):
    """Process image basename for a pid, or None — never raises."""
    if not pid:
        return None
    ps = _psutil()
    if ps is not None:
        try:
            return ps.Process(int(pid)).name() or None
        except Exception:
            pass
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_wchar), ctypes.POINTER(ctypes.c_ulong),
        ]
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED
        if not handle:
            return None
        try:
            size = ctypes.c_ulong(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value) or None
            return None
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _enum_windows():
    """Raw open-window dicts (the seam tests stub).

    Each dict carries title/process/pid/bounds/monitor/visible/focused
    plus a private "_hwnd" on Windows for _activate_window(). Returns []
    off Windows. Falls back to pyautogui.getAllWindows() (process/pid
    null) when pywin32 is unavailable.
    """
    if os.name != "nt":
        return []
    rects = _monitor_rects()
    try:
        import win32gui
        import win32process
    except Exception:
        wins = []
        try:
            for w in pyautogui.getAllWindows():
                title = getattr(w, "title", "") or ""
                if not title:
                    continue
                bounds = {
                    "x": int(getattr(w, "left", 0) or 0),
                    "y": int(getattr(w, "top", 0) or 0),
                    "w": int(getattr(w, "width", 0) or 0),
                    "h": int(getattr(w, "height", 0) or 0),
                }
                handle = getattr(w, "_hWnd", None)  # pygetwindow keeps the HWND here
                wins.append({
                    "title": title, "process": None, "pid": None,
                    "bounds": bounds,
                    "monitor": _monitor_for_bounds(bounds, rects),
                    "visible": bool(getattr(w, "visible", True)),
                    "focused": bool(getattr(w, "isActive", False)),
                    "_window": w,
                    # Review E1: with the handle, focus and the held-act hwnd
                    # pin work without pywin32 (via _user32()).
                    "_hwnd": int(handle) if isinstance(handle, int) and not isinstance(handle, bool) else None,
                })
        except Exception:
            return []
        return wins
    wins = []
    try:
        foreground = win32gui.GetForegroundWindow()
    except Exception:
        foreground = None

    def _cb(hwnd, _extra):
        try:
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return True
            x, y, x2, y2 = win32gui.GetWindowRect(hwnd)
            bounds = {"x": int(x), "y": int(y), "w": int(x2 - x), "h": int(y2 - y)}
            try:
                _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                pid = None
            wins.append({
                "title": title,
                "process": _process_name(pid),
                "pid": int(pid) if pid else None,
                "bounds": bounds,
                "monitor": _monitor_for_bounds(bounds, rects),
                "visible": bool(win32gui.IsWindowVisible(hwnd)),
                "focused": hwnd == foreground,
                "_hwnd": hwnd,
            })
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        return wins
    return wins


def _shape_window(win):
    """Raw _enum_windows() dict -> the public window shape."""
    b = win.get("bounds") or {}
    return {
        "title": win.get("title") or "",
        "process": win.get("process"),
        "pid": win.get("pid"),
        "bounds": {
            "x": int(b.get("x", 0)), "y": int(b.get("y", 0)),
            "w": int(b.get("w", 0)), "h": int(b.get("h", 0)),
        },
        "monitor": win.get("monitor"),
        "visible": bool(win.get("visible", True)),
        "focused": bool(win.get("focused", False)),
    }


def _windows_supported():
    return os.name == "nt"


def _activate_window(win):
    """Bring one raw window dict to the foreground (the seam tests stub).

    On Windows: restore first if minimised, then SetForegroundWindow.
    The well-known foreground-lock workaround (AttachThreadInput) is
    deliberately out of scope here — see docs/VERIFICATION.md.
    Returns True only when activation was applied.

    Review E1: this used to import pywin32 BEFORE looking at the window,
    so on a host without pywin32 -- which is when the pygetwindow
    fallback windows exist at all -- every /focus failed ("activation
    failed"), and the fallback below was dead code. Activation now goes
    through ctypes (_user32()) and needs no pywin32.
    """
    if os.name != "nt":
        return False
    hwnd = win.get("_hwnd")
    if hwnd is None:
        fallback = win.get("_window")
        if fallback is None:
            return False
        try:
            if getattr(fallback, "isMinimized", False):
                fallback.restore()
            fallback.activate()
            return True
        except Exception:
            return False
    try:
        user32 = _user32()
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        return bool(user32.SetForegroundWindow(hwnd))
    except Exception:
        return False  # foreground lock can refuse; caller must not claim ok


@app.get("/windows")
def windows():
    raw = _enum_windows()
    resp = {"ok": True, "windows": [_shape_window(w) for w in raw]}
    if not _windows_supported() and not raw:
        resp["supported"] = False
    return jsonify(resp)


@app.post("/focus")
def focus():
    """Focus the first window whose title matches {title}.

    Substring, case-insensitive by default; exact=true requires a full
    (case-insensitive) title match. No match -> 404.
    """
    d = _json_body()
    title = d.get("title")
    if not isinstance(title, str) or not title.strip():
        return jsonify({"ok": False, "error": "title must be a non-empty string"}), 400
    exact = bool(d.get("exact", False))
    needle = title.casefold()
    refused = _yield_if_active()
    if refused is not None:
        return refused
    for win in _enum_windows():
        candidate = (win.get("title") or "").casefold()
        matched = candidate == needle if exact else needle in candidate
        if matched:
            if not _activate_window(win):
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": "activation failed",
                            "window": _shape_window(win),
                        }
                    ),
                    409,
                )
            return jsonify({"ok": True, "window": _shape_window(win)})
    return jsonify({"ok": False, "error": f"no window matching {title!r}"}), 404


# ---------------------------------------------- V3-P4: clipboard (#6)

CF_UNICODETEXT = 13


class _WindowsClipboardBackend:
    """Clipboard via ctypes user32/kernel32 (CF_UNICODETEXT)."""

    def _setup(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.GetClipboardData.restype = ctypes.c_void_p
        user32.SetClipboardData.restype = ctypes.c_void_p
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        # R7.2 fix (Finding 4): every call that takes a handle declares
        # it pointer-sized. Without argtypes ctypes passes a 64-bit
        # handle as a C int and raises ArgumentError for handles above
        # 2**31 — a failure no off-Windows fake can reproduce.
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalFree.restype = ctypes.c_void_p
        kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        return user32, kernel32

    def get_text(self):
        user32, kernel32 = self._setup()
        if not user32.OpenClipboard(None):
            raise OSError("OpenClipboard failed")
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    def set_text(self, text):
        user32, kernel32 = self._setup()
        data = ctypes.create_unicode_buffer(text)
        # R7.2 (wishlist #26): a c_wchar array has no .size attribute —
        # the byte count is ctypes.sizeof(data). The old data.size read
        # raised AttributeError on every real Windows call, so
        # /clipboard/set (and the /type paste path) could never set the
        # clipboard on the PC; it shipped because this backend never
        # runs off Windows and no test faked _setup.
        nbytes = ctypes.sizeof(data)
        if not user32.OpenClipboard(None):
            raise OSError("OpenClipboard failed")
        try:
            user32.EmptyClipboard()
            handle = kernel32.GlobalAlloc(0x0002, nbytes)  # GMEM_MOVEABLE
            if not handle:
                raise OSError("GlobalAlloc failed")
            owned = False
            try:
                ptr = kernel32.GlobalLock(handle)
                if not ptr:
                    raise OSError("GlobalLock failed")
                try:
                    ctypes.memmove(ptr, data, nbytes)
                finally:
                    kernel32.GlobalUnlock(handle)
                if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                    raise OSError("SetClipboardData failed")
                owned = True  # the clipboard owns the memory now
            finally:
                if not owned:
                    kernel32.GlobalFree(handle)
        finally:
            user32.CloseClipboard()


class _PyperclipBackend:
    def __init__(self, mod):
        self._mod = mod

    def get_text(self):
        return self._mod.paste() or ""

    def set_text(self, text):
        self._mod.copy(text)


def _clipboard_backend():
    """Clipboard backend seam: Windows ctypes, else pyperclip, else None."""
    if os.name == "nt":
        return _WindowsClipboardBackend()
    try:
        import pyperclip

        return _PyperclipBackend(pyperclip)
    except Exception:
        return None


@app.post("/clipboard/set")
def clipboard_set():
    d = _json_body()
    text = d.get("text")
    if not isinstance(text, str):
        return jsonify({"ok": False, "error": "text must be a string"}), 400
    backend = _clipboard_backend()
    if backend is None:
        return jsonify({"ok": False, "error": "clipboard unavailable on this host"}), 501
    try:
        backend.set_text(text)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"clipboard set failed: {exc}"}), 502
    return {"ok": True}


@app.get("/clipboard/get")
def clipboard_get():
    """Read the clipboard. Sensitive: same X-Secret gate as every hands
    route, and every read is logged (below) so the operator sees it in
    the app window's log area."""
    backend = _clipboard_backend()
    if backend is None:
        return jsonify({"ok": False, "error": "clipboard unavailable on this host"}), 501
    try:
        text = backend.get_text()
    except Exception as exc:
        return jsonify({"ok": False, "error": f"clipboard read failed: {exc}"}), 502
    log_line("clipboard read via relay")
    return jsonify({"ok": True, "text": text if isinstance(text, str) else ""})
# ---- elevation bridge ----------------------------------------------------
# The agent may only NAME a request from the operator's pre-configured
# allowlist (hands config file key "elevate_allowlist":
# {name: {"exe": "...", "args": "..."}} — static strings from the
# operator's config, never from the request). Nothing runs from here:
# the request is queued for the app window, and only the person at the
# PC clicking Approve there resolves it (via _resolve_elevation). There
# is deliberately no auto-approve path, no timeout-approve, and no
# config flag that approves.

ELEVATE_TTL_SECONDS = 120

elevate_queue: "queue.Queue[dict]" = queue.Queue()
_elevations: dict = {}
_elevations_lock = threading.Lock()


def _elevate_allowlist() -> dict:
    """The operator-configured elevation allowlist (name -> exe/args)."""
    try:
        allow = _cfg.get("elevate_allowlist") if isinstance(_cfg, dict) else None
    except Exception:
        return {}
    return allow if isinstance(allow, dict) else {}


def _run_elevated(entry) -> bool:
    """Windows seam: launch entry's exe/args elevated via UAC (runas).

    Returns True only when ShellExecuteW reports success (> 32).
    Off-Windows or on any error, returns False.
    """
    if os.name != "nt":
        return False
    try:
        shell32 = ctypes.windll.shell32
        # HINSTANCE is pointer-sized. The default c_int restype truncates it
        # and can record launched vs launch_failed backwards.
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        result = shell32.ShellExecuteW(
            None, "runas", entry.get("exe") or "", entry.get("args") or None, None, 1
        )
        code = 0 if result is None else int(result)
        return code > 32
    except Exception:
        return False


def _expire_if_stale(entry) -> None:
    if entry.get("state") == "pending" and (
        time.time() - float(entry.get("created_at") or 0) > ELEVATE_TTL_SECONDS
    ):
        entry["state"] = "expired"


# Review L5: _elevations used to keep every request forever (states change,
# nothing was ever deleted). Settled requests are kept for an hour so a
# late GET /elevate/<id> still answers, and the table is capped.
ELEVATE_RETAIN_SECONDS = 3600
ELEVATE_MAX = 200


def _prune_elevations_locked(now):
    for entry in _elevations.values():
        _expire_if_stale(entry)
    for eid in [
        eid for eid, entry in _elevations.items()
        if entry.get("state") != "pending"
        and now - float(entry.get("created_at") or 0) > ELEVATE_RETAIN_SECONDS
    ]:
        del _elevations[eid]
    while len(_elevations) >= ELEVATE_MAX:
        oldest = min(_elevations, key=lambda e: float(_elevations[e].get("created_at") or 0))
        evicted = _elevations.pop(oldest)
        if evicted.get("state") == "pending":
            evicted["state"] = "expired"  # never left resolvable after eviction


def expire_stale_elevations():
    """Settle every pending request that is past its TTL (review E3).

    Expiry used to happen only when the agent polled GET /elevate/<id> or
    someone clicked the card. An unpolled request therefore stayed
    "pending" forever: the app's approval card never closed, and the app
    refused to self-heal the hands while a pending request existed. The
    packaged app calls this on every UI drain.
    """
    with _elevations_lock:
        for entry in _elevations.values():
            _expire_if_stale(entry)


def _resolve_elevation(eid, approved) -> bool:
    """Resolve a pending request. Called only by the app's dialog.

    Returns True when a pending request was resolved, False for an
    unknown or already-resolved/expired id (no-op).
    """
    with _elevations_lock:
        entry = _elevations.get(eid)
        if entry is None or entry.get("state") != "pending":
            return False
        _expire_if_stale(entry)
        if entry.get("state") != "pending":
            return False
        if not approved:
            entry["state"] = "denied"
            return True
        try:
            launched = bool(_run_elevated(entry))
        except Exception:
            launched = False
        entry["state"] = "launched" if launched else "launch_failed"
        return True


@app.post("/elevate")
def elevate():
    d = _json_body()
    name = d.get("name") or ""
    allow = _elevate_allowlist()
    if not allow:
        return jsonify({"ok": False, "error": "elevation not configured"}), 501
    spec_entry = allow.get(name) if isinstance(name, str) else None  # review E6: list name was a 500
    if not isinstance(spec_entry, dict):
        return jsonify({"ok": False, "error": "unknown elevation name"}), 404
    eid = uuid.uuid4().hex
    entry = {
        "id": eid,
        "name": name,
        "exe": str(spec_entry.get("exe") or ""),
        "args": str(spec_entry.get("args") or ""),
        "state": "pending",
        "created_at": time.time(),
    }
    with _elevations_lock:
        _prune_elevations_locked(entry["created_at"])
        _elevations[eid] = entry
    elevate_queue.put(dict(entry))
    return jsonify({"ok": True, "id": eid, "state": "pending"})


@app.get("/elevate/<eid>")
def elevate_status(eid):
    with _elevations_lock:
        entry = _elevations.get(eid)
        if entry is None:
            return jsonify({"ok": False, "error": "unknown elevation id"}), 404
        _expire_if_stale(entry)
        return jsonify(
            {
                "ok": True,
                "id": entry["id"],
                "name": entry["name"],
                "state": entry["state"],
            }
        )


# ---------------------------------------------------------------- UI glue

log_queue: "queue.Queue[str]" = queue.Queue()
url_queue: "queue.Queue[str]" = queue.Queue()

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def log_line(text: str) -> None:
    log_queue.put(text)


class RelayApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.server = None
        self.tunnel_proc = None
        self.transport = None
        self.stopping = False

        root.title("HandsRelay")
        root.resizable(False, False)
        self._set_icon()

        pad = {"padx": 10, "pady": 4}
        self.status = tk.StringVar(value="Starting…")
        tk.Label(root, textvariable=self.status, font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", **pad
        )

        tk.Label(root, text="Tunnel URL:").grid(row=1, column=0, sticky="e", **pad)
        self.url_var = tk.StringVar(value="(waiting for cloudflared…)")
        tk.Entry(root, textvariable=self.url_var, width=52, state="readonly").grid(
            row=1, column=1, **pad
        )
        tk.Button(root, text="Copy", command=lambda: self._copy(self.url_var.get())).grid(
            row=1, column=2, **pad
        )

        tk.Label(root, text="Secret:").grid(row=2, column=0, sticky="e", **pad)
        self.secret_var = tk.StringVar(value=SHARED_SECRET)
        tk.Entry(root, textvariable=self.secret_var, width=52, state="readonly").grid(
            row=2, column=1, **pad
        )
        tk.Button(
            root, text="Copy", command=lambda: self._copy(self.secret_var.get())
        ).grid(row=2, column=2, **pad)

        tk.Label(root, text="Alert topic (ntfy):").grid(row=3, column=0, sticky="e", **pad)
        self.topic_var = tk.StringVar(value=ALERT_TOPIC)
        topic_entry = tk.Entry(root, textvariable=self.topic_var, width=52)
        topic_entry.grid(row=3, column=1, **pad)
        topic_entry.bind("<FocusOut>", self._save_topic)
        topic_entry.bind("<Return>", self._save_topic)
        tk.Label(root, text="phone alerts").grid(row=3, column=2, **pad)

        tk.Label(
            root,
            text="Paste the URL and the secret to Muse to start a session.\n"
            "Closing this window ends the session and destroys the secret.",
            justify="left",
            fg="#555",
        ).grid(row=4, column=0, columnspan=3, sticky="w", **pad)

        self.log = tk.Text(root, width=78, height=8, state="disabled")
        self.log.grid(row=5, column=0, columnspan=3, **pad)

        tk.Button(root, text="Stop and close", command=self.shutdown).grid(
            row=6, column=0, columnspan=3, pady=8
        )
        root.protocol("WM_DELETE_WINDOW", self.shutdown)

        if os.name == "nt":
            try:
                # ES_CONTINUOUS | ES_SYSTEM_REQUIRED: stay reachable while away
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
                log_line("keeping this PC awake while the relay runs")
            except Exception:
                pass
        log_line(f"phone alerts: subscribe to ntfy topic '{ALERT_TOPIC}' on your phone")
        log_line(f"session secret generated ({len(SHARED_SECRET)} chars)")

        threading.Thread(target=self._run_server, daemon=True).start()
        # A tunnel starts only on an explicit opt-in, and a public
        # Cloudflare tunnel needs its own separate opt-in (H5):
        # HANDS_ENABLE_TUNNEL=1 enables Tailscale Serve only.
        if (os.environ.get("HANDS_ENABLE_TUNNEL") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        ):
            threading.Thread(target=self._run_tunnel, daemon=True).start()
        else:
            log_line(
                "tunnel disabled (set HANDS_ENABLE_TUNNEL=1 to opt in; "
                "a public Cloudflare tunnel additionally needs "
                "HANDS_ENABLE_PUBLIC_TUNNEL=1)"
            )
        self._poll()

    def _set_icon(self):
        try:
            base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            icon = os.path.join(base, "muse.ico")
            if os.path.exists(icon):
                self.root.iconbitmap(icon)
        except Exception:
            pass  # icon is decoration; never let it block startup

    def _save_topic(self, _event=None):
        global ALERT_TOPIC
        topic = self.topic_var.get().strip()
        if topic and topic != ALERT_TOPIC:
            ALERT_TOPIC = topic
            cfg = _load_config()
            cfg["ntfy_topic"] = topic
            _save_config(cfg)
            log_line(f"phone alert topic set to {topic}")

    def _copy(self, text: str):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _run_server(self):
        start_resting_hand()  # idles unless a claim is held
        try:
            try:
                from waitress import create_server

                self.server = create_server(app, host="127.0.0.1", port=PORT, threads=8)
                log_line(f"relay server (Waitress) listening on 127.0.0.1:{PORT}")
                self.server.run()
            except ImportError:
                from werkzeug.serving import make_server

                self.server = make_server("127.0.0.1", PORT, app, threaded=True)
                log_line(
                    f"relay server (Werkzeug fallback) listening on 127.0.0.1:{PORT}"
                )
                self.server.serve_forever()
        except Exception as exc:  # e.g. port already in use
            log_line(f"SERVER ERROR: {exc}")

    def _run_tunnel(self):
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW
        # Preferred transport: Tailscale Serve -- tailnet only, no public URL.
        ts = shutil.which("tailscale")
        if ts:
            try:
                subprocess.run(
                    [ts, "serve", "--bg", str(PORT)],
                    capture_output=True,
                    text=True,
                    creationflags=flags,
                    timeout=30,
                )
                out = subprocess.run(
                    [ts, "status", "--json"],
                    capture_output=True,
                    text=True,
                    creationflags=flags,
                    timeout=30,
                ).stdout
                dns = json.loads(out)["Self"]["DNSName"].rstrip(".")
                self.transport = "tailscale"
                url_queue.put(f"https://{dns}")
                log_line("transport: Tailscale Serve (tailnet only, no public URL)")
                return
            except Exception as exc:
                # H5: a Tailscale failure NEVER falls back to a public
                # tunnel automatically. A transient failure used to
                # silently publish the desktop on a public
                # trycloudflare.com URL; now it stops here, loudly, and
                # a public tunnel needs its own explicit opt-in.
                log_line(
                    f"ERROR: Tailscale Serve failed ({exc}); NOT falling "
                    "back to a public tunnel — set "
                    "HANDS_ENABLE_PUBLIC_TUNNEL=1 to opt into Cloudflare "
                    "explicitly"
                )
                return
        # Public transport: Cloudflare quick tunnel, only on its own
        # explicit opt-in (HANDS_ENABLE_PUBLIC_TUNNEL=1). It is never a
        # fallback from a failed Tailscale Serve.
        if (os.environ.get("HANDS_ENABLE_PUBLIC_TUNNEL") or "").strip().lower() not in (
            "1",
            "true",
            "yes",
        ):
            log_line(
                "Cloudflare quick tunnel not enabled (set "
                "HANDS_ENABLE_PUBLIC_TUNNEL=1 to opt in explicitly); "
                "no public URL will be created"
            )
            return
        exe = shutil.which("cloudflared")
        if not exe:
            log_line(
                "cloudflared not found on PATH. Install it with: "
                "winget install cloudflare.cloudflared"
            )
            return
        try:
            self.tunnel_proc = subprocess.Popen(
                [exe, "tunnel", "--url", f"http://127.0.0.1:{PORT}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=flags,
            )
            for line in self.tunnel_proc.stdout:
                m = URL_RE.search(line)
                if m:
                    url_queue.put(m.group(0))
        except Exception as exc:
            log_line(f"TUNNEL ERROR: {exc}")

    def _poll(self):
        try:
            while True:
                self._append_log(log_queue.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                url = url_queue.get_nowait()
                self.url_var.set(url)
                self.status.set("Running — paste the URL and secret to Muse")
                self._append_log(f"tunnel is up: {url}")
        except queue.Empty:
            pass
        # Review L5: this standalone window has no approval UI, and nothing
        # drained elevate_queue, so requests piled up and could never be
        # approved. Deny them at once (never runs anything) and say why.
        try:
            while True:
                req = elevate_queue.get_nowait()
                _resolve_elevation(req.get("id"), False)
                self._append_log(
                    f"elevation request '{req.get('name')}' denied: approvals "
                    "need the packaged HandsRelay v2 app"
                )
        except queue.Empty:
            pass
        if not self.stopping:
            self.root.after(200, self._poll)

    LOG_KEEP = 600  # lines the log area keeps, like the packaged app

    def _append_log(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        # The Text widget used to grow without bound for the life of the app.
        lines = int(str(self.log.index("end-1c")).split(".")[0])
        if lines > self.LOG_KEEP:
            self.log.delete("1.0", f"{lines - self.LOG_KEEP}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def shutdown(self):
        self.stopping = True
        try:
            if self.transport == "tailscale":
                ts = shutil.which("tailscale")
                if ts:
                    subprocess.run(
                        [ts, "serve", "reset"],
                        capture_output=True,
                        timeout=15,
                    )
            if self.tunnel_proc is not None:
                self.tunnel_proc.terminate()
        except Exception:
            pass
        if os.name == "nt":
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            except Exception:
                pass
        self.root.destroy()
        os._exit(0)  # server threads are daemons; exit ends them too


def main():
    root = tk.Tk()
    RelayApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
