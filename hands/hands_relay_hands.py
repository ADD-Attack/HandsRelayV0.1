"""
HandsRelay -- let Muse drive your Windows mouse/keyboard, with your consent.

One small window. No terminals.

WHAT IT DOES
  - Starts the relay server on 127.0.0.1:5000 (localhost only -- nothing on
    your network can reach it directly).
  - Opens a Cloudflare quick tunnel only on opt-in (HANDS_ENABLE_TUNNEL=1);
    off by default — a public tunnel is a fallback, never the design.
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
    tailnet and there is NO public URL at all. Otherwise it falls back
    to a Cloudflare quick tunnel. Responses carry X-Robots-Tag: noindex
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
from flask import Flask, request, jsonify, send_file, has_request_context

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


def _load_claim_sync():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claim_sync.py")
    spec = importlib.util.spec_from_file_location("hands_relay_claim_sync", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


claim_sync = _load_claim_sync()

# ---------------------------------------------------------------- server

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1_000_000  # requests are tiny JSON

# ---- failed-guess lockout ------------------------------------------------
_fail_lock = threading.Lock()
MAX_FAILS = 10        # bad secrets allowed inside the window...
FAIL_WINDOW = 600     # ...of 10 minutes, then auth pauses for...
LOCKOUT_SECONDS = 300  # ...5 minutes (restart the app to clear early)


# ---- phone alerts (ntfy) + persistent security log -----------------------
_BASE_DIR = os.path.dirname(
    os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)
)
_claim_sync = claim_sync.ClaimSync(os.environ.get("HANDS_SYNC_DIR") or os.path.join(_BASE_DIR, "claim-sync"))
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
    # Hooks win over a timestamp span: a physical event during our own
    # injection is still the owner's. A quiet hook plus a span that
    # explains the idle tick is not owner activity. Idle with no such
    # span remains the signal used when hooks have not observed it.
    now_tick = _tick_ms()
    if _owner_hook_recent(threshold):
        idle_seconds = 0.0
    else:
        last = _LEDGER.last_input_tick(idle_seconds, now_tick)
        if (owner_coverage() == "hook" and last is not None
                and _LEDGER._is_self(last)):
            idle_seconds = max(float(idle_seconds or 0.0), threshold)
        else:
            idle_seconds = _LEDGER.owner_idle(idle_seconds, now_tick)
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


def _foreground_window():
    """The focused window dict from _enum_windows(), or None."""
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
    want_instance = expect.get("process_instance")
    if isinstance(want_instance, str) and want_instance:
        pid = win.get("pid")
        try:
            snap = _process_snapshot(pid) if type(pid) is int and pid > 0 else None
        except Exception:
            return False
        if not isinstance(snap, dict) or snap.get("running") is not True or snap.get("instance") != want_instance:
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
    body = request.get_json(force=True, silent=True) or {}
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
        if now < _lockout_by_peer.get(peer, 0.0):
            return "locked", False
        if not secret:
            return "bad", False
        times = _fail_by_peer.setdefault(peer, [])
        times[:] = [t for t in times if now - t < FAIL_WINDOW]
        times.append(now)
        tripped = len(times) >= MAX_FAILS
        if tripped:
            _lockout_by_peer[peer] = now + LOCKOUT_SECONDS
            times.clear()
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


# R13 B04: receiver-local, boot-bound, single-start execution leases.
_EXECUTION_VERSION = 1
_EXECUTION_BOOT = uuid.uuid4().hex
_execution_lock = threading.RLock()
_execution_leases = {}
_EXECUTION_MAX = 512


def _operation_digest(op, payload):
    if not isinstance(payload, dict) or not isinstance(op, str):
        raise ValueError("invalid operation")
    clean = {k: v for k, v in payload.items() if k != "_execution_lease"}
    encoded = json.dumps([op, clean], sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _execution_mint(op, payload, lifetime):
    if (type(lifetime) not in (int, float) or not math.isfinite(lifetime)
            or not 1 <= lifetime <= 120):
        raise ValueError("invalid_lifetime")
    path = "/clipboard/set" if op == "clipboard_set" else "/" + str(op)
    if path not in _ACT_PATHS:
        raise ValueError("invalid_operation")
    digest = _operation_digest(op, payload)
    now = time.monotonic()
    with _execution_lock:
        for token, row in list(_execution_leases.items()):
            if row["expires"] <= now:
                del _execution_leases[token]
        if len(_execution_leases) >= _EXECUTION_MAX:
            raise ValueError("lease_capacity")
        token = secrets_mod.token_urlsafe(32)
        _execution_leases[token] = {"digest": digest, "expires": now + lifetime,
                                    "started": False}
    return {"version": _EXECUTION_VERSION, "boot": _EXECUTION_BOOT, "token": token}


def _execution_check(metadata, op, payload, *, start=False):
    """Called under input access immediately before first effect (B07).

    Receiver time alone decides expiry. No public digest, renewal, or replay.
    Return a controlled refusal code, or None. Starting consumes atomically.
    """
    if (not isinstance(metadata, dict) or type(metadata.get("version")) is not int
            or metadata.get("version") != _EXECUTION_VERSION):
        return "lease_invalid"
    if metadata.get("boot") != _EXECUTION_BOOT:
        return "lease_wrong_boot"
    token = metadata.get("token")
    if not isinstance(token, str):
        return "lease_invalid"
    try:
        digest = _operation_digest(op, payload)
    except (ValueError, TypeError):
        return "lease_invalid"
    with _execution_lock:
        row = _execution_leases.get(token)
        if row is None:
            return "lease_unknown"
        if row["digest"] != digest:
            return "lease_operation_changed"
        if row["started"]:
            return "lease_consumed"
        if time.monotonic() >= row["expires"]:
            return "deadline_expired"
        if start:
            row["started"] = True
    return None


@app.get("/execution/capabilities")
def execution_capabilities():
    return jsonify({"ok": True, "execution_lease_version": _EXECUTION_VERSION,
                    "boot": _EXECUTION_BOOT, "max_hold_seconds": 120,
                    "foreground_hwnd": (_foreground_window() or {}).get("_hwnd")})


@app.post("/execution/lease")
def execution_lease():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error_code": "lease_invalid"}), 400
    try:
        metadata = _execution_mint(body.get("op"), body.get("payload"),
                                   body.get("hold_seconds"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error_code": "lease_invalid"}), 400
    return jsonify({"ok": True, "lease": metadata})


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


class _CaptureOutOfRange(Exception):
    """A monitor index is not in this topology snapshot."""


class _CaptureOutOfSurface(Exception):
    """The requested rect is outside the virtual surface. Never pad it."""


class _CaptureTopologyChanged(Exception):
    """Enumeration changed while the bitmap was being taken."""


class _CaptureExtentMismatch(Exception):
    """The bitmap size does not equal the topology union. No rescale."""


class _CaptureBackendUnavailable(Exception):
    """Neither ImageGrab nor pyautogui could return an all-screens bitmap."""


def _topology_snapshot():
    """One normalized monitor list. Order is not a display identity."""
    snapshot = []
    for rect in _monitor_rects():
        snapshot.append(
            {
                "x": int(rect["x"]),
                "y": int(rect["y"]),
                "w": int(rect["w"]),
                "h": int(rect["h"]),
                "primary": bool(rect.get("primary")),
            }
        )
    return snapshot


def _union(topology):
    """Global origin and extent of the virtual surface for this snapshot."""
    x0 = min(rect["x"] for rect in topology)
    y0 = min(rect["y"] for rect in topology)
    x1 = max(rect["x"] + rect["w"] for rect in topology)
    y1 = max(rect["y"] + rect["h"] for rect in topology)
    return x0, y0, x1 - x0, y1 - y0


def _grab_all_screens():
    """All-screens bitmap. Cropping is never left to the backend.

    ImageGrab(all_screens=True) is the selected Windows surface. pyautogui's
    allScreens flag is only the fallback when ImageGrab cannot run, and a
    region tuple is never forwarded: global coordinates are not surface
    coordinates when the virtual origin is not (0, 0).
    """
    try:
        from PIL import ImageGrab

        image = ImageGrab.grab(all_screens=True)
        if image is not None:
            return image
    except Exception:
        image = None
    try:
        return pyautogui.screenshot(allScreens=True)
    except Exception as exc:
        raise _CaptureBackendUnavailable("virtual surface capture failed") from exc


def capture_virtual_surface(topology=None):
    """Pixels plus the global origin and extent of those pixels.

    Coordinate space is the virtual desktop. Pixel (0, 0) is global
    (vx, vy), the minimum monitor origin in this same topology snapshot.
    A requested global rect (x, y, w, h) is the surface crop
    (x-vx, y-vy, w, h). DPI mode for a packaged hands process is
    Per-Monitor-V2 so these pixels match input coordinates. This module
    does not change process DPI awareness and does not rescale: a bitmap
    whose size differs from the topology union is refused.
    """
    snapshot = list(topology) if topology is not None else _topology_snapshot()
    image = _grab_all_screens()
    _vx, _vy, width, height = _union(snapshot)
    return {
        "image": image,
        "x": _vx,
        "y": _vy,
        "w": image.size[0],
        "h": image.size[1],
        "topology": snapshot,
        "expected_w": width,
        "expected_h": height,
    }


def _grab(rect):
    """Capture rect ({"x","y","w","h"}) or, with None, the legacy frame.

    None keeps pyautogui.screenshot() with no region, the historical
    default extent. A rect is cropped from the virtual surface of one
    topology snapshot. The snapshot is read again after the grab; a
    change refuses the bitmap instead of pairing it with another layout.
    """
    if rect is None:
        return pyautogui.screenshot()
    before = _topology_snapshot()
    if not before:
        raise _CaptureOutOfSurface("no monitors")
    surface = capture_virtual_surface(before)
    if _topology_snapshot() != before:
        raise _CaptureTopologyChanged("monitor topology changed during capture")
    image = surface["image"]
    if image.size != (surface["expected_w"], surface["expected_h"]):
        raise _CaptureExtentMismatch(
            "capture extent does not match the monitor topology: "
            f"bitmap {image.size[0]}x{image.size[1]}, "
            f"topology {surface['expected_w']}x{surface['expected_h']}"
        )
    vx, vy = surface["x"], surface["y"]
    x, y = int(rect["x"]), int(rect["y"])
    w, h = int(rect["w"]), int(rect["h"])
    left, top = x - vx, y - vy
    right, bottom = left + w, top + h
    if w <= 0 or h <= 0 or left < 0 or top < 0 or right > image.size[0] or bottom > image.size[1]:
        raise _CaptureOutOfSurface("region is outside the virtual surface")
    return image.crop((left, top, right, bottom))


def _capture_indexed_monitor(monitor_idx):
    """Resolve a monitor index and grab it from the same topology snapshot."""
    last = None
    for _attempt in (1, 2):
        topology = _topology_snapshot()
        if monitor_idx < 0 or monitor_idx >= len(topology):
            raise _CaptureOutOfRange(
                f"monitor {monitor_idx} out of range: "
                f"{len(topology)} monitor(s) available"
            )
        rect = dict(topology[monitor_idx])
        try:
            image = _grab(rect)
        except _CaptureTopologyChanged as exc:
            last = exc
            continue
        if _topology_snapshot() != topology:
            last = _CaptureTopologyChanged("monitor topology changed during capture")
            continue
        return image, rect, topology
    raise last or _CaptureTopologyChanged("monitor topology changed during capture")


def _capture_region(region_rect):
    """Grab one global rect, retrying once if the topology changes."""
    last = None
    for _attempt in (1, 2):
        topology = _topology_snapshot()
        try:
            image = _grab(region_rect)
        except _CaptureTopologyChanged as exc:
            last = exc
            continue
        if _topology_snapshot() != topology:
            last = _CaptureTopologyChanged("monitor topology changed during capture")
            continue
        return image, dict(region_rect), topology
    raise last or _CaptureTopologyChanged("monitor topology changed during capture")


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
    try:
        if monitor_idx is not None:
            img, rect, topology = _capture_indexed_monitor(monitor_idx)
        elif region_rect is not None:
            img, rect, topology = _capture_region(region_rect)
        else:
            img, rect, topology = _grab(None), None, None
    except _CaptureOutOfRange as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except _CaptureOutOfSurface as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except (_CaptureTopologyChanged, _CaptureExtentMismatch, _CaptureBackendUnavailable) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
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
    if topology is not None and rect is not None:
        vx, vy, sw, sh = _union(topology)
        resp.headers["X-Screen-Origin"] = f"{vx},{vy}"
        resp.headers["X-Screen-Surface"] = f"{sw},{sh}"
        resp.headers["X-Screen-Rect"] = f"{rect['x']},{rect['y']},{rect['w']},{rect['h']}"
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


def _tick_ms():
    """The GetTickCount clock that GetLastInputInfo's dwTime lives on;
    a monotonic stand-in off Windows (tests, Linux)."""
    if os.name == "nt":
        try:
            return int(ctypes.windll.kernel32.GetTickCount()) & 0xFFFFFFFF  # type: ignore[attr-defined]
        except Exception:
            pass
    return int(time.monotonic() * 1000) & 0xFFFFFFFF


# One input stream at a time, with hand-travel between devices (#30 rule
# 4). Every injecting op holds it; the resting hand only ever try-acquires.
_INPUT = motion.InputMutex(clock=lambda: time.monotonic(), sleep=lambda s: time.sleep(s))
# Resting-hand injections, so the yield probe can tell them from the owner (#28).
_LEDGER = motion.SelfInputLedger()


class _InputRefused(Exception):
    def __init__(self, response):
        self.response = response


class _PasteUnavailable(Exception):
    """Paste cannot start. A key fallback is still allowed."""


class _PasteUncertain(Exception):
    """The paste chord may have reached the desktop. Do not also type it."""


class _PasteAborted(Exception):
    """Clipboard work stopped before the chord. No key fallback."""

    def __init__(self, body, status=409):
        self.body = body
        self.status = status


@app.errorhandler(_InputRefused)
def _input_refused(error):
    return error.response


_guard_local = threading.local()


class _input:
    """Serialize, then revalidate under input access; failed enter releases."""

    def __init__(self, kind):
        self.kind = kind
        self.started = False

    def check(self, start=False):
        if not has_request_context():
            return
        if self.started:
            refused = _focus_guard()
            reason = "foreground_changed" if refused is not None else None
            # Typing owns its partial-count lock checkpoints; B10 unifies
            # those without losing the structured type-abort response.
            if request.path != "/type" and _probe_locked() is True:
                reason = "locked"
            if reason:
                raise _InputRefused((jsonify({"ok": False, "aborted": True,
                    "error_code": "input_aborted", "abort_reason": reason}), 409))
        else:
            refused = _yield_if_active()
            if refused is None:
                refused = _focus_guard()
            if refused is not None:
                raise _InputRefused(refused)
        body = request.get_json(silent=True) or {}
        if not self.started and "_execution_lease" in body:
            op = "clipboard_set" if request.path == "/clipboard/set" else request.path.lstrip("/")
            code = _execution_check(body["_execution_lease"], op, body, start=start)
            if code:
                raise _InputRefused((jsonify({"ok": False, "error_code": code}), 409))

    def before_effect(self):
        self.check(start=not self.started)
        if not self.started:
            _resting.act_begin()
            self.started = True

    def __enter__(self):
        _INPUT.acquire(self.kind, random)
        try:
            self.check()
            _guard_local.current = self
            return self
        except BaseException:
            _INPUT.release(self.kind)
            raise

    def __exit__(self, *exc):
        try:
            if self.started:
                _resting.act_end(time.monotonic())
        finally:
            _guard_local.current = None
            _INPUT.release(self.kind)
        return False


def _before_effect():
    guard = getattr(_guard_local, "current", None)
    if guard is not None:
        guard.before_effect()


def _inject(function, *args, **kwargs):
    _before_effect()
    started = _tick_ms()
    try:
        return function(*args, **kwargs)
    finally:
        ended = _tick_ms()
        if started is not None and ended is not None:
            _LEDGER.record(started, ended)


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


def _peek_unit(guard):
    """Owner, lock, then focus. None means the next unit may be emitted."""
    guard.sample()
    if guard.owner_seen:
        return "owner", None
    if _probe_locked() is True:
        return "locked", None
    if has_request_context():
        focus = _focus_guard()
        if focus is not None:
            return "foreground_changed", focus
    return None, None


def _refuse_unit(kind, focus, started):
    """A blocked first unit can yield. Anything later is a partial abort."""
    if not started:
        if focus is not None:
            return focus
        return _pre_effect_response("locked" if kind == "locked" else "owner")
    return (jsonify({
        "ok": False,
        "aborted": True,
        "error": "input aborted after a side effect",
        "error_code": "input_aborted",
        "abort_reason": kind,
    }), 409)


def _guarded_inject(guard, started, function, *args, **kwargs):
    kind, focus = _peek_unit(guard)
    if kind:
        raise _InputRefused(_refuse_unit(kind, focus, started[0]))
    result = _inject(function, *args, **kwargs)
    guard.after_write()
    started[0] = True
    return result


def _run_path(steps, guard=None, started=None):
    guard = guard or _TypeOwnerGuard()
    if started is None:
        started = [False]
    for sx, sy, delay in steps:
        _guarded_inject(guard, started, pyautogui.moveTo, sx, sy, duration=0)
        time.sleep(delay)
    return guard, started


def _move_human(x, y, rng=None, width=None, path_seconds=None, guard=None, started=None):
    """Move the cursor to (x, y) along a planned human-like path."""
    rng = rng if rng is not None else random
    x0, y0 = pyautogui.position()
    return _run_path(
        _plan_path(x0, y0, x, y, rng, width=width, path_seconds=path_seconds),
        guard=guard, started=started)


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


@app.post("/move")
def move():
    d = request.get_json(force=True)
    x, y = int(d["x"]), int(d["y"])
    with _input("mouse"):
        if _human(d):
            w, h = _box(d)
            if d.get("attention"):
                time.sleep(motion.attention_delay(random, d.get("text_chars")))
            _move_human(x, y, width=min(w, h) if w else None)
        else:
            _guarded_inject(_TypeOwnerGuard(), [False], pyautogui.moveTo, x, y, duration=0.2)
    return {"ok": True}


@app.post("/click")
def click():
    """Click at (x, y). Human mode (default, R9 #24/#29/#30):

    optional {w, h}: the target box centred on (x, y) — the landing
    point is Gaussian inside it and its size sets the Fitts speed;
    optional attention (default true) + text_chars: the pre-move
    hesitation. Timing is fitted to the +0.5 s per-click budget.
    """
    d = request.get_json(force=True)
    x, y = int(d["x"]), int(d["y"])
    button = d.get("button", "left")
    if not _human(d):
        with _input("mouse"):
            guard, started = _TypeOwnerGuard(), [False]
            _guarded_inject(guard, started, pyautogui.moveTo, x, y, duration=0.2)
            begun = {"v": False}

            def _click(begun=begun):
                begun["v"] = True
                return pyautogui.click(x=x, y=y, button=button)

            try:
                _guarded_inject(guard, started, _click)
            except Exception:
                if begun["v"]:
                    try:
                        pyautogui.mouseUp(button=button)
                    except Exception:
                        pass
                raise
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
        guard, started = _TypeOwnerGuard(), [False]
        _move_human(tx, ty, rng, path_seconds=travel, guard=guard, started=started)
        time.sleep(hover)
        kind, focus = _peek_unit(guard)
        if kind:
            raise _InputRefused(_refuse_unit(kind, focus, started[0]))
        _before_effect()
        try:
            _inject(pyautogui.mouseDown, x=tx, y=ty, button=button)
            guard.after_write()
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


def _release_chord(keys):
    """Required key-up cleanup. Never treated as a new input unit."""
    for key in keys:
        try:
            pyautogui.keyUp(key)
        except Exception:
            pass


def _owner_attribution_limit():
    """What this process cannot honestly attribute to the owner."""
    coverage = owner_coverage()
    if coverage == "degraded":
        return "unreliable"
    if coverage == "hook":
        return "non-injected-events-only"
    return "same-tick-and-grace-blind"


def _pre_effect_response(kind):
    if kind == "locked":
        error, code = "desktop locked — control yielded", "locked"
    else:
        error, code = "user active — control yielded", "yield"
    return jsonify({"ok": False, "error": error, "error_code": code}), 409


def _type_dispatch(method, keys_fn, paste_fn, text):
    """Pick the type path. Keys fallback only if paste never started."""
    if method == "keys":
        return keys_fn(), None
    wants_paste = method == "paste" or (
        method == "auto" and len(text) > 120 and _clipboard_backend() is not None
    )
    if wants_paste:
        try:
            return paste_fn(), None
        except _PasteUnavailable as exc:
            return keys_fn(), f"paste unavailable ({exc}); used keys"
    return keys_fn(), None


@app.post("/type")
def type_text():
    """Type text one character at a time, or paste when that path is safe.

    {text, method?: "auto"|"paste"|"keys", jitter?: bool}

    Jitter changes delays only. Both key modes use the same per-character
    loop: sample owner, lock, and focus before each character; an owner
    event during the current character stops the next one. A fixed 0.02 s
    delay is the non-jitter plan.

    Paste samples before clipboard mutation and again before Ctrl+V.
    Owner activity before the chord does not paste. The saved clipboard
    is restored only when it still holds the value this call placed.
    A chord that may have been sent never falls back to typing, and the
    response does not invent chars_typed for paste consumption.
    """
    refused = _yield_if_active()
    if refused is not None:
        return refused
    d = request.get_json(force=True, silent=True) or {}
    text = d.get("text")
    if not isinstance(text, str):
        return jsonify({"ok": False, "error": "text must be a string"}), 400
    method = d.get("method", "auto")
    if method not in ("auto", "paste", "keys"):
        return jsonify({"ok": False, "error": "method must be auto, paste or keys"}), 400
    jitter = bool(d.get("jitter", True))
    _owner_monitor.ensure_started()
    started = time.monotonic()
    note = None
    delays = []
    typed = 0
    state = {"guard": None, "reason": None, "pre_effect": None}

    def _unit_block(guard):
        """(kind, focus_response) when the next unit must not be emitted."""
        guard.sample()
        if guard.owner_seen:
            return "owner", None
        if _probe_locked() is True:
            return "locked", None
        focus = _focus_guard()
        if focus is not None:
            return "foreground_changed", focus
        return None, None

    def _remember_block(kind, focus):
        if typed == 0:
            state["pre_effect"] = focus if focus is not None else _pre_effect_response(
                "locked" if kind == "locked" else "owner")
        else:
            state["reason"] = kind

    def _keys():
        nonlocal delays, typed
        delays = _plan_key_delays(text, random) if jitter else [0.02] * len(text)
        guard = _TypeOwnerGuard()
        state["guard"] = guard.mode
        for ch, delay in zip(text, delays):
            kind, focus = _unit_block(guard)
            if kind:
                _remember_block(kind, focus)
                break
            try:
                _inject(pyautogui.write, ch)
            except _InputRefused as exc:
                if typed == 0:
                    state["pre_effect"] = exc.response
                else:
                    body = exc.response[0].get_json() if isinstance(exc.response, tuple) else exc.response.get_json()
                    state["reason"] = (body or {}).get("error_code") or "aborted"
                break
            except Exception:
                _release_chord(("shift", "ctrl", "alt"))
                raise
            guard.after_write()
            time.sleep(delay)
            typed += 1
        state["guard"] = guard.mode
        return "keys"

    def _paste():
        backend = _clipboard_backend()
        if backend is None:
            raise _PasteUnavailable("clipboard unavailable on this host")
        saved, readable = None, False
        try:
            saved = backend.get_text()
            readable = True
        except Exception:
            pass
        guard = _TypeOwnerGuard()
        state["guard"] = guard.mode
        kind, focus = _unit_block(guard)
        if kind:
            raise _InputRefused(focus if focus is not None else _pre_effect_response(
                "locked" if kind == "locked" else "owner"))
        _before_effect()
        mutated = False
        placed = None
        chord_attempted = False
        try:
            try:
                backend.set_text(text)
            except _InputRefused:
                raise
            except Exception as exc:
                if getattr(backend, "changed", False):
                    mutated = True
                    placed = text
                    raise _PasteUncertain(f"clipboard mutation outcome unknown ({exc})")
                raise _PasteUnavailable(str(exc))
            mutated = True
            placed = text
            kind, _focus = _unit_block(guard)
            if kind:
                raise _PasteAborted({
                    "ok": False,
                    "error": f"paste stopped before the chord ({kind})",
                    "error_code": "paste_aborted",
                    "abort_reason": kind,
                    "injected": False,
                    "method_used": "paste",
                })

            def _chord():
                nonlocal chord_attempted
                chord_attempted = True
                return pyautogui.hotkey("ctrl", "v")

            try:
                _inject(_chord)
            except _InputRefused as exc:
                if chord_attempted:
                    raise _PasteUncertain("paste chord may have been sent during refusal") from exc
                body = exc.response[0].get_json() if isinstance(exc.response, tuple) else exc.response.get_json()
                raise _PasteAborted({
                    "ok": False,
                    "error": "paste stopped before the chord",
                    "error_code": "paste_aborted",
                    "abort_reason": (body or {}).get("error_code") or "aborted",
                    "injected": False,
                    "method_used": "paste",
                })
            if readable:
                time.sleep(0.1)
        except (_PasteUncertain, _PasteAborted, _PasteUnavailable, _InputRefused):
            raise
        except Exception as exc:
            if chord_attempted or mutated:
                raise _PasteUncertain(f"paste chord may have been sent ({exc})")
            raise _PasteUnavailable(str(exc))
        finally:
            if chord_attempted:
                _release_chord(("ctrl", "v"))
            if mutated and readable and saved is not None:
                try:
                    current = backend.get_text()
                except Exception:
                    current = None
                if current == placed:
                    try:
                        backend.set_text(saved)
                    except Exception:
                        pass
        return "paste"

    try:
        with _input("keys"):
            method_used, note = _type_dispatch(method, _keys, _paste, text)
    except _PasteUncertain as exc:
        return jsonify({
            "ok": False,
            "error": str(exc),
            "error_code": "paste_uncertain",
            "method_used": "paste",
            "injected": "uncertain",
            "chars": len(text),
        }), 502
    except _PasteAborted as exc:
        return jsonify(exc.body), exc.status

    if state["pre_effect"] is not None and typed == 0:
        return state["pre_effect"]
    if method_used == "keys" and typed < len(text):
        reason = state["reason"] or "owner"
        if reason == "locked":
            error = f"desktop locked — typing aborted at char {typed}"
        elif reason == "foreground_changed":
            error = f"foreground changed — typing aborted at char {typed}"
        else:
            error = f"owner became active — typing aborted at char {typed}"
        body = {
            "ok": False,
            "error": error,
            "error_code": "type_aborted",
            "abort_reason": reason,
            "owner_guard": state["guard"],
            "aborted": True,
            "chars_typed": typed,
            "chars": len(text),
            "method_used": "keys",
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "delays_planned": len(delays),
            "planned_seconds": sum(delays[:typed]),
        }
        limit = {"hook": "non-injected-events-only",
                 "dwtime": "same-tick-and-grace-blind"}.get(state["guard"])
        if limit:
            body["owner_attribution_limit"] = limit
        return jsonify(body), 409
    resp = {
        "ok": True,
        "method_used": method_used,
        "owner_guard": state["guard"],
        "chars": len(text),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "delays_planned": len(delays),
        "planned_seconds": sum(delays),
    }
    if state["guard"] == "hook":
        resp["owner_attribution_limit"] = "non-injected-events-only"
    elif state["guard"] == "dwtime":
        resp["owner_attribution_limit"] = "same-tick-and-grace-blind"
    if note:
        resp["note"] = note
    return jsonify(resp)


@app.post("/key")
def key():
    d = request.get_json(force=True)
    with _input("keys"):
        name = d["key"]
        begun = {"v": False}

        def _press(begun=begun, name=name):
            begun["v"] = True
            return pyautogui.press(name)

        try:
            _guarded_inject(_TypeOwnerGuard(), [False], _press)
        except Exception:
            if begun["v"]:
                _release_chord((name,))
            raise
    return {"ok": True}


@app.post("/scroll")
def scroll():
    """Scroll by clicks. Human mode (R9 #29): variable 1-3 click chunks
    with 30-120 ms pauses instead of one jump; the total is exact."""
    d = request.get_json(force=True)
    clicks = int(d.get("clicks", 3))
    with _input("mouse"):
        guard, started = _TypeOwnerGuard(), [False]
        if _human(d):
            for chunk, pause in motion.plan_scroll(clicks, random):
                _guarded_inject(guard, started, pyautogui.scroll, chunk)
                if pause:
                    time.sleep(pause)
        else:
            _guarded_inject(guard, started, pyautogui.scroll, clicks)
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


class _OwnerInputMonitor:
    def __init__(self):
        self.owner = 0
        self.injected = 0
        self.running = False
        self._thread = None
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
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="hands-input-hooks", daemon=True
                )
                self._thread.start()
        self._ready.wait(wait)
        return self.running

    def _run(self):
        try:
            from ctypes import wintypes

            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            LRESULT = ctypes.c_ssize_t
            HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
            user32.CallNextHookEx.restype = LRESULT
            user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE

            class KBD(ctypes.Structure):
                _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                            ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                            ("dwExtraInfo", ctypes.c_size_t)]

            class MSL(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long),
                            ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                            ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

            def _kbd(code, wparam, lparam):
                if code >= 0:
                    flags = ctypes.cast(lparam, ctypes.POINTER(KBD)).contents.flags
                    if flags & LLKHF_INJECTED:
                        self.injected += 1
                    else:
                        self.owner += 1
                return user32.CallNextHookEx(None, code, wparam, lparam)

            def _mouse(code, wparam, lparam):
                if code >= 0:
                    flags = ctypes.cast(lparam, ctypes.POINTER(MSL)).contents.flags
                    if flags & LLMHF_INJECTED:
                        self.injected += 1
                    else:
                        self.owner += 1
                return user32.CallNextHookEx(None, code, wparam, lparam)

            self._procs = (HOOKPROC(_kbd), HOOKPROC(_mouse))  # keep alive
            hmod = kernel32.GetModuleHandleW(None)
            hk = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._procs[0], hmod, 0)
            hm = user32.SetWindowsHookExW(WH_MOUSE_LL, self._procs[1], hmod, 0)
            if not hk or not hm:
                file_log("owner-input hooks unavailable; type guard uses dwTime")
                self._ready.set()
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
            self.running = False
            self._ready.set()


_owner_monitor = _OwnerInputMonitor()


def _owner_input_snapshot():
    """(owner_events, injected_events) from the hooks, or None. Seam."""
    return _owner_monitor.snapshot()


# Persistent across actions. A fresh per-action snapshot would forget
# physical input that happened before the action started.
_owner_observed = {"count": None, "at": None}
_owner_degraded = {"reason": None}


def _observe_owner_counter():
    snap = _owner_input_snapshot()
    if not isinstance(snap, tuple) or not snap:
        return None
    try:
        owner = int(snap[0])
    except (TypeError, ValueError):
        return None
    if _owner_observed["count"] is not None and owner != _owner_observed["count"]:
        _owner_observed["at"] = time.monotonic()
    _owner_observed["count"] = owner
    return owner


def _owner_hook_recent(threshold):
    """True when a physical hook event is still inside the yield window."""
    _observe_owner_counter()
    at = _owner_observed["at"]
    try:
        window = float(threshold)
    except (TypeError, ValueError):
        return False
    return at is not None and (time.monotonic() - at) < window


def owner_coverage():
    """hook, unavailable, or degraded. Degraded is never reported as reliable."""
    if _owner_degraded["reason"]:
        return "degraded"
    if isinstance(_owner_input_snapshot(), tuple):
        return "hook"
    return "unavailable"


def mark_owner_coverage_degraded(reason):
    _owner_degraded["reason"] = str(reason or "degraded")


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

        class _LastInputInfo(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]

        info = _LastInputInfo()
        info.cbSize = ctypes.sizeof(_LastInputInfo)
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

        class _Guid(ctypes.Structure):
            _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort),
                        ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]

        def _guid(d1, d2, d3, d4):
            return _Guid(d1, d2, d3, (ctypes.c_ubyte * 8)(*d4))

        # GUID_VIDEO_SUBGROUP / VIDEOIDLE
        sub = _guid(0x7516B95F, 0xF776, 0x4464, [0x8C, 0x53, 0x06, 0x16, 0x7F, 0x40, 0xCC, 0x99])
        idle = _guid(0x3C0BC021, 0xC8A8, 0x4E07, [0xA9, 0x73, 0x6B, 0x14, 0xCB, 0xCB, 0x2B, 0x7E])

        class _Sps(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                        ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                        ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]

        sps = _Sps()
        on_dc = bool(kernel32.GetSystemPowerStatus(ctypes.byref(sps))) and sps.ACLineStatus == 0
        scheme = ctypes.POINTER(_Guid)()
        if powrprof.PowerGetActiveScheme(None, ctypes.byref(scheme)) == 0:
            try:
                value = ctypes.c_ulong(0)
                read = powrprof.PowerReadDCValueIndex if on_dc else powrprof.PowerReadACValueIndex
                if read(None, scheme, ctypes.byref(sub), ctypes.byref(idle), ctypes.byref(value)) == 0:
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
# R12 (#36): each float step is executed as an interpolated glide
# (motion.plan_rest_path) instead of one instant moveTo — the pointer
# travels the 1-3 px along an eased micro-path; the contract above is
# unchanged, including the mid-glide owner-grab abort in _move.

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
        if owner_coverage() == "degraded":
            with self._lock:
                self._end()
            return 1.0
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
        """Glide one float step (R12 #36); None if the owner took over.

        R11 executed the step as a single moveTo(duration=0) and the
        pointer teleported 1-3 px. Now the travel follows
        motion.plan_rest_path: interpolated sub-steps (<= 1 px apart)
        along the human-like planner's curve, spread over the glide
        duration. Before every sub-step the pointer must still be where
        the glide left it; if it is not, the owner's hand is on the
        mouse and the glide aborts at once — step() ends the episode
        exactly as it did for a fought instant move.
        """
        t0 = _tick_ms()
        # Record BEFORE injecting: a concurrent yield probe must already
        # see this span as ours. The provisional end covers the longest
        # glide (REST_GLIDE_MAX_S); truncate_last closes the span at its
        # true end, and the finally below guarantees that close even if
        # a sub-step raises — otherwise a fault mid-glide would leave
        # the full 1500 ms provisional standing, a window in which real
        # owner input could be misread as self-input (review F3).
        _LEDGER.record(t0, (t0 + 1500) & 0xFFFFFFFF)
        fought = False
        expect = None
        try:
            start = tuple(pyautogui.position())
            steps = motion.plan_rest_path(start[0], start[1], target[0], target[1], self.rng)
            expect = start
            initial = _owner_input_snapshot()
            initial_owner = initial[0] if isinstance(initial, tuple) else None
            for sx, sy, delay in steps:
                px, py = pyautogui.position()
                current = _owner_input_snapshot()
                owner_changed = (initial_owner is not None and isinstance(current, tuple)
                                 and current[0] != initial_owner)
                permit_lost = self.claim_until is not None and not self.claim_held(self._clock())
                if (permit_lost
                        or owner_changed or _probe_locked() is True
                        or owner_coverage() == "degraded"
                        or abs(px - expect[0]) > 1 or abs(py - expect[1]) > 1):
                    fought = True
                    break
                _inject(pyautogui.moveTo, sx, sy, duration=0)
                expect = (sx, sy)
                if delay > 0:
                    time.sleep(delay)
        finally:
            _LEDGER.truncate_last((_tick_ms() + 50) & 0xFFFFFFFF)
        if fought:
            return None
        px, py = pyautogui.position()
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


@app.get("/resting")
def resting_get():
    return {"ok": True, **_resting.status()}


@app.get("/resting/challenge")
def resting_challenge():
    """Single-use permit challenge, timed on this receiver's monotonic clock."""
    return jsonify(_claim_sync.issue(time.monotonic()))


def _restart_hands_boot():
    """Test and lifecycle seam: a new boot drops every grant."""
    _claim_sync.rotate_boot()
    with _resting._lock:
        _resting.claim_until = None
        _resting.claim_seq = -1
        _resting._end()


@app.post("/resting/claim")
def resting_claim():
    """Gateway push.

    Legacy: {held, expires_in, seq}. Authoritative: those fields plus
    pairing, epoch, boot, and a single-use challenge. The legacy body
    remains because existing direct callers assert it still applies.
    """
    d = request.get_json(force=True, silent=True) or {}
    if any(key in d for key in ("epoch", "pairing", "boot", "challenge")):
        result = _claim_sync.evaluate(d, time.monotonic(), _resting)
        if result.get("invalid"):
            return jsonify({"ok": False, "error": "claim sync message is invalid"}), 400
        if result.get("applied") and d.get("held") is True:
            start_resting_hand()
        body = {
            "ok": True,
            "applied": bool(result.get("applied")),
            "boot": d.get("boot"),
            "epoch": d.get("epoch"),
            "seq": d.get("seq"),
            "claim_id": d.get("claim_id"),
            **_resting.status(),
        }
        if _claim_sync.sync_error:
            body["sync_error"] = _claim_sync.sync_error
        return body
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
    body["owner_coverage"] = owner_coverage()
    body["owner_attribution_limit"] = _owner_attribution_limit()
    body["claim_sync"] = {
        "boot": _claim_sync.boot,
        "epoch": _claim_sync.accepted_epoch,
        "ok": _claim_sync.sync_error is None,
        "error": _claim_sync.sync_error,
    }
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
    body = request.get_json(force=True, silent=True) or {}
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
    body = request.get_json(force=True, silent=True) or {}
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
    body = request.get_json(force=True, silent=True) or {}
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
    body = request.get_json(force=True, silent=True) or {}
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
    d = request.get_json(force=True, silent=True) or {}
    keys = d.get("keys")
    if (
        not isinstance(keys, list)
        or not keys
        or not all(isinstance(k, str) and k.strip() for k in keys)
    ):
        return jsonify({"ok": False, "error": "keys must be a non-empty list of strings"}), 400
    with _input("keys"):
        begun = {"v": False}

        def _chord(begun=begun):
            begun["v"] = True
            return pyautogui.hotkey(*keys)

        try:
            _guarded_inject(_TypeOwnerGuard(), [False], _chord)
        except Exception:
            if begun["v"]:
                _release_chord(keys)
            raise
        _release_chord(keys)
    return {"ok": True}


@app.post("/break")
def break_():
    """Send Ctrl+C as a true chord, 1..3 times (default 1), 0.15s apart.

    Root cause this fixes: the /key op calls _inject(pyautogui.press, ) with ONE
    key name, so sending the string "ctrl+c" to /key was never a real
    chord — press() treats it as a single (nonexistent) key name and
    nothing was interrupted. A long-running / polling foreground process
    therefore could not be stopped through the relay. /break (and
    /hotkey) use _inject(pyautogui.hotkey, ), which presses the keys together,
    so the interrupt actually lands.
    """
    refused = _yield_if_active()
    if refused is not None:
        return refused
    d = request.get_json(force=True, silent=True) or {}
    times = d.get("times", 1)
    if isinstance(times, bool) or not isinstance(times, int) or not 1 <= times <= 3:
        return jsonify({"ok": False, "error": "times must be an integer 1..3"}), 400
    with _input("keys"):
        guard, started = _TypeOwnerGuard(), [False]
        for i in range(times):
            begun = {"v": False}

            def _chord(begun=begun):
                begun["v"] = True
                return pyautogui.hotkey("ctrl", "c")

            try:
                _guarded_inject(guard, started, _chord)
            except Exception:
                if begun["v"]:
                    _release_chord(("ctrl", "c"))
                raise
            _release_chord(("ctrl", "c"))
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
    d = request.get_json(force=True, silent=True) or {}
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
        guard, started = _TypeOwnerGuard(), [False]
        if human:
            # R9 (#24): human approach to the grab point, a hover, then a
            # human path while held; duration (if given) is the held
            # travel time, else Fitts decides. Endpoints are exact.
            _move_human(x1, y1, guard=guard, started=started)
            time.sleep(motion.hover_delay(random))
        else:
            _guarded_inject(guard, started, pyautogui.moveTo, x1, y1)
        down = False
        try:
            kind, focus = _peek_unit(guard)
            if kind:
                raise _InputRefused(_refuse_unit(kind, focus, started[0]))
            # Mark first so a mouseDown/move failure still releases the button.
            # duration 0 is real; do not coerce it with `or 0.4`.
            down = True
            _inject(pyautogui.mouseDown, button=button)
            guard.after_write()
            if human:
                time.sleep(motion.click_hold(random))
                _move_human(x2, y2, path_seconds=duration, guard=guard, started=started)
            else:
                _guarded_inject(
                    guard, started, pyautogui.moveTo, x2, y2,
                    duration=0.4 if duration is None else duration)
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


def _process_name(pid):
    """Process image basename for a pid, or None — never raises."""
    if not pid:
        return None
    try:
        import psutil

        return psutil.Process(int(pid)).name() or None
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
                wins.append({
                    "title": title, "process": None, "pid": None,
                    "bounds": bounds,
                    "monitor": _monitor_for_bounds(bounds, rects),
                    "visible": bool(getattr(w, "visible", True)),
                    "focused": bool(getattr(w, "isActive", False)),
                    "_window": w,
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
    """
    if os.name != "nt":
        return False
    try:
        import win32con
        import win32gui

        hwnd = win.get("_hwnd")
        if hwnd is None:
            fallback = win.get("_window")
            if fallback is not None:
                try:
                    if getattr(fallback, "isMinimized", False):
                        fallback.restore()
                    fallback.activate()
                    return True
                except Exception:
                    return False
            return False
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        return True
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
    d = request.get_json(force=True, silent=True) or {}
    title = d.get("title")
    if not isinstance(title, str) or not title.strip():
        return jsonify({"ok": False, "error": "title must be a non-empty string"}), 400
    exact = bool(d.get("exact", False))
    needle = title.casefold()
    refused = _yield_if_active()
    if refused is not None:
        return refused
    with _input("keys"):
        for win in _enum_windows():
            candidate = (win.get("title") or "").casefold()
            matched = candidate == needle if exact else needle in candidate
            if matched:
                current = next((item for item in _enum_windows()
                                if item.get("_hwnd") == win.get("_hwnd")
                                and (item.get("title") or "").casefold() == candidate), None)
                if current is None:
                    return jsonify({"ok": False, "error_code": "target_unavailable",
                                    "error": "focus target disappeared"}), 409
                if not _guarded_inject(_TypeOwnerGuard(), [False], _activate_window, current):
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
        self.changed = False
        if not user32.OpenClipboard(None):
            raise OSError("OpenClipboard failed")
        try:
            self.changed = True  # EmptyClipboard is a mutation
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
        self.changed = True
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
    d = request.get_json(force=True, silent=True) or {}
    text = d.get("text")
    if not isinstance(text, str):
        return jsonify({"ok": False, "error": "text must be a string"}), 400
    backend = _clipboard_backend()
    if backend is None:
        return jsonify({"ok": False, "error": "clipboard unavailable on this host"}), 501
    with _input("keys"):
        _before_effect()
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
    d = request.get_json(force=True, silent=True) or {}
    name = d.get("name") or ""
    allow = _elevate_allowlist()
    if not allow:
        return jsonify({"ok": False, "error": "elevation not configured"}), 501
    spec_entry = allow.get(name)
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
        # A public tunnel is a fallback, never the default (ROADMAP):
        # it starts only on an explicit opt-in.
        if (os.environ.get("HANDS_ENABLE_TUNNEL") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        ):
            threading.Thread(target=self._run_tunnel, daemon=True).start()
        else:
            log_line("public tunnel disabled (set HANDS_ENABLE_TUNNEL=1 to opt in)")
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
                log_line(f"Tailscale Serve failed ({exc}); falling back to Cloudflare")
        # Fallback transport: Cloudflare quick tunnel (public URL).
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
        if not self.stopping:
            self.root.after(200, self._poll)

    def _append_log(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
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
