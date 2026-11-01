"""
HandsRelay -- let Muse drive your Windows mouse/keyboard, with your consent.

One small window. No terminals.

WHAT IT DOES
  - Starts the relay server on 127.0.0.1:5000 (localhost only -- nothing on
    your network can reach it directly).
  - Opens a Cloudflare quick tunnel and shows the URL in the window.
  - Uses a strong per-machine secret: generated on first run and stored
    at %USERPROFILE%\\.HandsRelayV0.1\\hands-secret.txt -- the same file
    the packaged v2 app owns -- so restarts, the gateway, and this app
    keep agreeing on it. Delete that file to rotate the secret.
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

TAKEOVER INTERLOCK (the person at the PC always wins)
  When physical input arrives that the agent did not send -- the mouse
  moving when no agent action moved it, or a local key / mouse-button
  press -- the hands enter YIELDED: move/click/type/key/scroll answer
  HTTP 409 {"ok": false, "error": "user active — control yielded"}
  (screenshots still work). Control returns automatically after
  MUSE_TAKEOVER_QUIET_SECONDS (default 10) with no local input.
  Detection on Windows uses low-level input hooks, which mark input
  injected by software (the agent's pyautogui calls) so it never trips
  the interlock; a cursor-deviation poll backs it up. If the hooks
  cannot be installed, a GetLastInputInfo poll with a short suppression
  window around agent actions is used instead (see README). Set
  MUSE_TAKEOVER=0 to disable the interlock entirely (not recommended).

WHAT MUSE CAN DO WITH IT (unchanged from cursor_relay.py)
  GET  /health              liveness only, no secret needed
  GET  /screenshot          PNG of your screen
  POST /move    {x, y}      move the cursor
  POST /click   {x, y, button}  click
  POST /type    {text}      type text
  POST /key     {key}       press a key ("enter", "tab", "esc", ...)
  POST /hotkey  {keys}      press a chord, e.g. ["win", "shift", "left"]
                            (keys held in order, released in reverse)
  POST /scroll  {clicks}    scroll
  GET  /status              who has control: {"control": "agent"|"yielded", ...}
  Every call except /health needs the secret, as header
  X-Secret: <the app's secret> or, equivalently, as a query parameter
  ?secret=<the app's secret> (v1 parity: some proxy/tunnel paths strip
  custom headers; a URL parameter survives them. Prefer the header when
  both work -- URLs are more likely to be logged.)
"""

import contextlib
import ctypes
import datetime
import hmac
import io
import json
import os
import queue
import re
import secrets as secrets_mod
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request

import pyautogui
from flask import Flask, request, jsonify, send_file

PORT = 5000

def _load_or_create_secret() -> str:
    """Per-machine hands secret, shared with the packaged v2 app.

    Stored at ~/.HandsRelayV0.1/hands-secret.txt (created on first run,
    0600 where the OS honors it; the value is never logged). Falls back
    to a per-launch secret if the file cannot be read or written.
    """
    path = os.path.join(
        os.path.expanduser("~"), ".HandsRelayV0.1", "hands-secret.txt"
    )
    try:
        with open(path, "r", encoding="utf-8") as fh:
            saved = fh.read().strip()
        if len(saved) >= 16:
            return saved
    except OSError:
        pass
    value = secrets_mod.token_urlsafe(32)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(value + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # best effort (on Windows this only toggles read-only)
    except OSError:
        pass
    return value


# From the OS CSPRNG (via the per-machine file above). When the packaged
# v2 app hosts this module it overwrites this global with its own loaded
# value -- the same file, so the two always agree.
SHARED_SECRET = _load_or_create_secret()

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.15

# ---------------------------------------------------------------- takeover

YIELD_ERROR = "user active — control yielded"


class Takeover:
    """Who has control: the agent, or the person at the keyboard.

    Local input (anything the agent did not send) marks the time of the
    last local activity; while that is less than ``quiet_seconds`` ago,
    control is YIELDED and act calls are refused. Two detectors feed it:

      * cursor deviation (every platform): a poll compares the cursor
        with where it was last seen; a move while no agent action is in
        flight is local input. Agent actions run inside
        ``agent_action()``, which re-baselines the cursor when they end,
        so agent-commanded moves never count.
      * Windows low-level hooks: keyboard and mouse events WITHOUT the
        "injected" flag are local input. pyautogui sends through
        SendInput, which Windows marks injected, so the agent's own keys
        and clicks never trip it. If the hooks cannot be installed, a
        GetLastInputInfo poll is used, ignoring input during agent
        actions plus ``suppress_seconds`` afterwards -- the tradeoff:
        local keystrokes inside that window go unnoticed.
    """

    def __init__(
        self,
        get_position,
        quiet_seconds=10.0,
        clock=time.monotonic,
        tolerance=2,
        suppress_seconds=0.3,
        on_change=None,
    ):
        self._get_position = get_position
        self.quiet_seconds = float(quiet_seconds)
        self._clock = clock
        self._tolerance = tolerance
        self._suppress_seconds = suppress_seconds
        self._on_change = on_change
        self._lock = threading.RLock()
        self._last_local = None  # clock() of the last local input
        self._last_reason = ""
        self._agent_depth = 0
        self._agent_until = 0.0
        self._last_seen = None
        self._reported = "agent"
        self._started = False
        self._last_input_tick = None
        self.detector = "cursor-poll"

    # ---- inputs -----------------------------------------------------
    def local_input(self, reason):
        """Record physical input. Cheap and lock-free: called from hooks."""
        self._last_reason = reason
        self._last_local = self._clock()

    @contextlib.contextmanager
    def agent_action(self):
        with self._lock:
            self._agent_depth += 1
        try:
            yield
        finally:
            with self._lock:
                self._agent_depth -= 1
                self._agent_until = self._clock() + self._suppress_seconds
                try:
                    self._last_seen = tuple(self._get_position())
                except Exception:
                    pass

    def agent_moving(self):
        """True while an agent action runs or just ended (lock-free read)."""
        return self._agent_depth > 0 or self._clock() < self._agent_until

    def check_cursor(self):
        """One cursor-deviation poll step."""
        with self._lock:
            try:
                pos = tuple(self._get_position())
            except Exception:
                return
            last, self._last_seen = self._last_seen, pos
            if self._agent_depth > 0 or last is None:
                return
            moved = max(abs(pos[0] - last[0]), abs(pos[1] - last[1])) > self._tolerance
        if moved:
            self.local_input("mouse moved")

    def check_last_input(self, tick):
        """Fallback detector: GetLastInputInfo tick changed."""
        with self._lock:
            prev, self._last_input_tick = self._last_input_tick, tick
            if prev is None or tick == prev:
                return
            if self._agent_depth > 0 or self._clock() < self._agent_until:
                return
        self.local_input("keyboard/mouse input")

    # ---- state ------------------------------------------------------
    def retry_after(self):
        last = self._last_local
        if last is None:
            return 0.0
        return max(0.0, self.quiet_seconds - (self._clock() - last))

    def is_yielded(self):
        yielded = self.retry_after() > 0
        self._report("yielded" if yielded else "agent")
        return yielded

    def state(self):
        yielded = self.is_yielded()
        return {
            "control": "yielded" if yielded else "agent",
            "retry_after": round(self.retry_after(), 1),
            "reason": self._last_reason if yielded else "",
            "quiet_seconds": self.quiet_seconds,
            "detector": self.detector,
        }

    def _report(self, state):
        with self._lock:
            if state == self._reported:
                return
            self._reported = state
        if self._on_change is not None:
            try:
                self._on_change(state, self._last_reason)
            except Exception:
                pass

    # ---- background watchers -----------------------------------------
    def start(self):
        with self._lock:
            if self._started:
                return
            self._started = True
        if os.name == "nt":
            threading.Thread(target=self._hook_thread, name="takeover-hooks", daemon=True).start()
        threading.Thread(target=self._poll_thread, name="takeover-poll", daemon=True).start()

    def _poll_thread(self):
        while True:
            self.check_cursor()
            if os.name == "nt" and self.detector == "cursor-poll+lastinput":
                tick = _last_input_tick()
                if tick is not None:
                    self.check_last_input(tick)
            self.is_yielded()  # reports state transitions (log + window)
            time.sleep(0.1)

    def _hook_thread(self):
        try:
            _run_input_hooks(self)
        except Exception as exc:
            self.detector = "cursor-poll+lastinput"
            log_line(f"takeover: input hooks unavailable ({exc}); using fallback detector")


def _last_input_tick():
    """Windows GetLastInputInfo tick, or None."""
    try:
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return info.dwTime
    except Exception:
        pass
    return None


def _run_input_hooks(takeover):
    """Install WH_KEYBOARD_LL + WH_MOUSE_LL and pump messages (Windows only).

    Callbacks only look at the "injected" flag and stamp a time -- they
    must return fast, or Windows drops the hook.
    """
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
    LLKHF_INJECTED = 0x10
    LLMHF_INJECTED, LLMHF_LOWER_IL_INJECTED = 0x01, 0x02
    WM_MOUSEMOVE = 0x0200
    LRESULT = ctypes.c_ssize_t
    ULONG_PTR = ctypes.c_size_t

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("pt", wintypes.POINT),
            ("mouseData", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = LRESULT
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    def on_key(n_code, w_param, l_param):
        if n_code == 0:
            info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if not info.flags & LLKHF_INJECTED:
                takeover.local_input("key press")
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def on_mouse(n_code, w_param, l_param):
        if n_code == 0:
            info = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            if not info.flags & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED):
                if w_param != WM_MOUSEMOVE:
                    takeover.local_input("mouse button")
                elif not takeover.agent_moving():
                    # pyautogui moves with SetCursorPos, which may not carry
                    # the injected flag; moves during an agent action (plus
                    # the short suppression window) are the agent's own.
                    takeover.local_input("mouse moved")
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    # Keep references: ctypes callbacks must outlive the hooks.
    takeover._hook_procs = (HOOKPROC(on_key), HOOKPROC(on_mouse))
    module = kernel32.GetModuleHandleW(None)
    hooks = [
        user32.SetWindowsHookExW(WH_KEYBOARD_LL, takeover._hook_procs[0], module, 0),
        user32.SetWindowsHookExW(WH_MOUSE_LL, takeover._hook_procs[1], module, 0),
    ]
    if not all(hooks):
        raise OSError(f"SetWindowsHookExW failed (error {ctypes.get_last_error()})")
    takeover.detector = "input-hooks+cursor-poll"
    log_line("takeover interlock: watching for local keyboard/mouse input")
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def _on_takeover_change(state, reason):
    if state == "yielded":
        log_line(f"CONTROL YIELDED to the person at the PC ({reason}) -- agent actions paused")
        file_log(f"control yielded ({reason})")
        status_queue.put("YIELDED — you have control; agent paused")
    else:
        log_line("control returned to the agent (quiet period over)")
        file_log("control returned to agent")
        status_queue.put("Agent has control — move the mouse or press a key to take over")


TAKEOVER_ENABLED = os.environ.get("MUSE_TAKEOVER", "1") != "0"
TAKEOVER = Takeover(
    get_position=lambda: pyautogui.position(),
    quiet_seconds=float(os.environ.get("MUSE_TAKEOVER_QUIET_SECONDS", "10")),
    on_change=_on_takeover_change,
)
ACT_PATHS = {"/move", "/click", "/type", "/key", "/scroll", "/hotkey"}


def _yielded_response(extra=None):
    body = {"ok": False, "error": YIELD_ERROR, "control": "yielded"}
    body.update(extra or {})
    resp = jsonify(body)
    resp.status_code = 409
    resp.headers["Retry-After"] = str(max(1, int(TAKEOVER.retry_after() + 0.999)))
    return resp

# ---------------------------------------------------------------- server

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1_000_000  # requests are tiny JSON

# ---- failed-guess lockout ------------------------------------------------
_fail_lock = threading.Lock()
_fail_times: list = []
_lockout_until = 0.0
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


def _auth_locked_out() -> bool:
    return time.monotonic() < _lockout_until


def _record_failure() -> None:
    global _lockout_until
    now = time.monotonic()
    file_log("bad secret attempt")
    with _fail_lock:
        _fail_times[:] = [t for t in _fail_times if now - t < FAIL_WINDOW]
        _fail_times.append(now)
        if len(_fail_times) >= MAX_FAILS:
            _lockout_until = now + LOCKOUT_SECONDS
            _fail_times.clear()
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


@app.before_request
def gate():
    if request.path == "/health":
        return  # liveness only: never gated, never counted as a failure
    if _auth_locked_out():
        return jsonify({"ok": False, "error": "locked out"}), 429
    provided = request.headers.get("X-Secret") or request.args.get("secret") or ""
    if not hmac.compare_digest(provided, SHARED_SECRET):
        _record_failure()
        log_line(f"rejected request to {request.path} (bad secret)")
        return jsonify({"ok": False, "error": "bad secret"}), 401
    if TAKEOVER_ENABLED:
        TAKEOVER.start()  # idempotent; the windows also start it at launch
        if request.path in ACT_PATHS and TAKEOVER.is_yielded():
            return _yielded_response()


@app.after_request
def noindex(resp):
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/screenshot")
def screenshot():
    img = pyautogui.screenshot()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    resp = send_file(buf, mimetype="image/png")
    resp.headers["X-Screen-Width"] = str(img.size[0])
    resp.headers["X-Screen-Height"] = str(img.size[1])
    return resp


@app.get("/status")
def status():
    state = TAKEOVER.state() if TAKEOVER_ENABLED else {"control": "agent", "detector": "disabled"}
    return {"ok": True, **state}


@app.post("/move")
def move():
    d = request.get_json(force=True)
    with TAKEOVER.agent_action():
        pyautogui.moveTo(int(d["x"]), int(d["y"]), duration=0.2)
    return {"ok": True}


@app.post("/click")
def click():
    d = request.get_json(force=True)
    with TAKEOVER.agent_action():
        pyautogui.click(x=int(d["x"]), y=int(d["y"]), button=d.get("button", "left"))
    return {"ok": True}


@app.post("/type")
def type_text():
    d = request.get_json(force=True)
    text = str(d["text"])
    with TAKEOVER.agent_action():
        # One character at a time (what pyautogui.write does anyway), so a
        # takeover mid-string stops typing instead of finishing into
        # whatever window the person has moved to.
        for i, ch in enumerate(text):
            if TAKEOVER_ENABLED and TAKEOVER.is_yielded():
                return _yielded_response({"typed": i, "total": len(text)})
            pyautogui.write(ch)
            time.sleep(0.02)
    return {"ok": True}


@app.post("/key")
def key():
    d = request.get_json(force=True)
    with TAKEOVER.agent_action():
        pyautogui.press(d["key"])
    return {"ok": True}


MAX_HOTKEY_KEYS = 6


@app.post("/hotkey")
def hotkey():
    """A key combination: {"keys": ["win", "shift", "left"]}.

    Names are validated against pyautogui's key set; unknown names are a
    400 and nothing is pressed. Single-key /key is unchanged.
    """
    d = request.get_json(force=True, silent=True) or {}
    keys = d.get("keys")
    if (
        not isinstance(keys, list)
        or not keys
        or len(keys) > MAX_HOTKEY_KEYS
        or not all(isinstance(k, str) and k for k in keys)
    ):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": f"keys must be a list of 1-{MAX_HOTKEY_KEYS} key names",
                }
            ),
            400,
        )
    keys = [k.lower() for k in keys]
    valid = set(pyautogui.KEYBOARD_KEYS)
    unknown = [k for k in keys if k not in valid]
    if unknown:
        return jsonify({"ok": False, "error": "unknown key name(s)", "unknown": unknown}), 400
    with TAKEOVER.agent_action():
        pyautogui.hotkey(*keys)
    return {"ok": True, "keys": keys}


@app.post("/scroll")
def scroll():
    d = request.get_json(force=True)
    with TAKEOVER.agent_action():
        pyautogui.scroll(int(d.get("clicks", 3)))
    return {"ok": True}


# ---------------------------------------------------------------- UI glue

log_queue: "queue.Queue[str]" = queue.Queue()
url_queue: "queue.Queue[str]" = queue.Queue()
status_queue: "queue.Queue[str]" = queue.Queue()

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
        self.server_failed = False

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
        log_line(f"hands secret ready ({len(SHARED_SECRET)} chars, per-machine file)")

        if TAKEOVER_ENABLED:
            TAKEOVER.start()
        else:
            log_line("WARNING: takeover interlock disabled (MUSE_TAKEOVER=0)")
        threading.Thread(target=self._run_server, daemon=True).start()
        threading.Thread(target=self._run_tunnel, daemon=True).start()
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
            self.server_failed = True
            log_line(f"SERVER ERROR: {exc}")
            status_queue.put("Server FAILED to start -- see log")

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
                if self.server_failed:
                    self.status.set("Tunnel up, but the server FAILED -- see log")
                else:
                    self.status.set("Running — paste the URL and secret to Muse")
                self._append_log(f"tunnel is up: {url}")
        except queue.Empty:
            pass
        try:
            while True:
                self.status.set(status_queue.get_nowait())
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
