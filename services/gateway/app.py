"""HandsRelay v2 — gateway service.

The agent-facing API. Owns authentication (per-deployment token, constant-time
compare, failed-guess lockout) and routes control requests to the Windows
hands process and parse requests to the parser service.

The hands link: the gateway holds the hands base URL (HANDS_URL) and the
hands' current session secret (HANDS_SECRET — the packaged hands app
generates a fresh secret every launch and shows it in its window; copy it
into the gateway's environment). Agent calls are translated:

  GET  /v1/screen            -> GET  {HANDS_URL}/screenshot  (PNG bytes back)
  GET  /v1/screen/hash       -> screenshot hash (see screen_hash below)
  GET  /v1/monitors          -> GET  {HANDS_URL}/monitors
  GET  /v1/windows           -> GET  {HANDS_URL}/windows
  GET  /v1/clipboard         -> GET  {HANDS_URL}/clipboard/get
  POST /v1/act {"op": ...}   -> POST {HANDS_URL}/<op>        (move/click/
                                                              type/key/scroll/
                                                              hotkey/break/
                                                              drag/focus;
                                                              clipboard_set ->
                                                              /clipboard/set)
"""

import base64
import binascii
import hashlib
import hmac
import math
import os
import queue as _queue_mod
import threading
import time
import uuid
from collections import OrderedDict
from io import BytesIO

import requests
from flask import Flask, Response, jsonify, request

TOKEN = os.environ.get("RELAY_GATEWAY_TOKEN", "")
HANDS_URL = os.environ.get("HANDS_URL", "http://host.docker.internal:5000").rstrip("/")
HANDS_SECRET = os.environ.get("HANDS_SECRET", "")
PARSER_URL = os.environ.get("PARSER_URL", "http://parser:5011").rstrip("/")

HANDS_OPS = {
    "move", "click", "type", "key", "scroll",
    # V3-P4 (wishlist #3, #4, #6, #19):
    "hotkey", "break", "drag", "focus", "clipboard_set",
}
# Op -> hands path, for ops whose hands route is not "/<op>".
HANDS_OP_PATHS = {"clipboard_set": "clipboard/set"}
HANDS_TIMEOUT = 30

# Screen hashing (wishlist #14): the bottom strip of the frame is the
# taskbar — clock, tray icons, notification churn — and must not change
# the hash. 80px matches the watcher convention the live walkthrough
# loop already uses on the client side.
DEFAULT_MASK_BOTTOM = 80
# Stable mode samples two frames this far apart and keeps only the
# pixels that agree, so a blinking caret or spinner phase cannot flip
# the hash (wishlist #14 addendum, 2026-10-19).
STABLE_SAMPLE_GAP = 0.65

MAX_FAILS = 10
FAIL_WINDOW = 600
LOCKOUT_SECONDS = 300

_fail_lock = threading.Lock()

_probe_lock = threading.Lock()
_probe_cache = {"hands": None, "parser": None, "pc_state": None, "at": 0.0}

# ---------------------------------------------------------------------------
# Hands claim coordination (wishlist #15).
#
# Advisory only: sessions read the claim before driving the desktop, but
# /v1/act is deliberately NOT gated on it — the claim coordinates polite
# agents, it is not an access control (the gateway token is that).
# State is in-memory and lock-guarded, with LAZY expiry: an expired claim
# is treated as absent the next time anything looks, via _current_claim.
# ---------------------------------------------------------------------------
CLAIM_TTL_DEFAULT = 900
CLAIM_TTL_MIN = 30
CLAIM_TTL_MAX = 3600
RESERVATION_GRACE = 30  # seconds the queue head alone may claim after a free-up
# Review L2: the queue is bounded and self-cleaning. A queued holder keeps
# its slot by re-POSTing {"queue": true} (which refreshes the entry);
# entries not refreshed within QUEUE_ENTRY_TTL are dropped, and a head
# whose reservation lapses unused forfeits its slot.
CLAIM_QUEUE_MAX = 32
QUEUE_ENTRY_TTL = 3600
HOLDER_MAX_CHARS = 128
PURPOSE_MAX_CHARS = 500

_claim_lock = threading.RLock()
_claim = None  # {"holder", "purpose", "since", "expires_at"} or None
_claim_queue = []  # FIFO of {"holder", "purpose", "enqueued_at", "refreshed_at"}
_reserved_for = None  # queue head holder with an exclusive claim window
_reserved_until = 0.0


# R11: the hands' resting-hand float runs only while a claim is held, and
# only the gateway knows claims — so every grant/refresh/release is pushed
# to the hands (/resting/claim) with the remaining lease and a sequence
# number (pushes run on a thread; the hands drops out-of-order ones). This
# is a notification only: lease duration and renewal are unchanged.
_claim_push_seq = 0
_claim_push_session = requests.Session()


def _post_claim_to_hands(payload):
    try:
        _claim_push_session.post(
            f"{HANDS_URL}/resting/claim", json=payload, headers=_hands_headers(), timeout=3
        )
    except requests.RequestException:
        pass  # the hands is down or older: no resting hand, nothing else changes


def _push_claim_locked(claim, now):
    """Queue a claim-state push to the hands; call with _claim_lock held."""
    global _claim_push_seq
    _claim_push_seq += 1
    payload = {
        "held": claim is not None,
        "expires_in": max(0.0, float(claim["expires_at"]) - now) if claim else 0.0,
        "seq": _claim_push_seq,
    }
    threading.Thread(target=_post_claim_to_hands, args=(payload,), daemon=True).start()


# ---------------------------------------------------------------------------
# R11 watchers (owner, 2026-10-20). A watcher is an agent in eyes-only mode:
# it polls the screen and never takes the claim, so without this it was
# invisible. POST /v1/watch (or ?watcher=<name> on /v1/screen,
# /v1/screen/hash, /v1/screen/wait) marks it watching; it lapses after its
# timeout without a refresh. Timeout: 30 s default, agent-settable up to
# 60 s per watcher (timeout_seconds) or gateway-wide (/v1/watch/config).
# Independent of the claim lease.
# ---------------------------------------------------------------------------
WATCHER_TIMEOUT_DEFAULT = 30
WATCHER_TIMEOUT_MIN = 30
WATCHER_TIMEOUT_MAX = 60

_watch_lock = threading.Lock()
_watchers = {}  # holder -> {"holder", "since", "last_seen", "timeout_seconds"}
WATCHERS_MAX = 64  # review L6: distinct watcher names cannot grow the table without bound


def _initial_watcher_timeout():
    return _clamp_number(
        os.environ.get("WATCHER_TIMEOUT_SECONDS", WATCHER_TIMEOUT_DEFAULT),
        WATCHER_TIMEOUT_DEFAULT,
        WATCHER_TIMEOUT_MIN,
        WATCHER_TIMEOUT_MAX,
    )


_watcher_timeout = None  # set lazily from _initial_watcher_timeout()


def _watcher_timeout_setting():
    global _watcher_timeout
    with _watch_lock:
        if _watcher_timeout is None:
            _watcher_timeout = _initial_watcher_timeout()
        return _watcher_timeout


def _touch_watcher(holder, timeout_seconds=None, now=None):
    holder = str(holder or "").strip()[:80]
    if not holder:
        return None
    now = time.time() if now is None else now
    default = _watcher_timeout_setting()
    with _watch_lock:
        # Review L6: expire lapsed watchers on WRITE too, not only when
        # someone reads /v1/info or /v1/watch, and cap distinct names.
        _expire_watchers_locked(now)
        if holder not in _watchers and len(_watchers) >= WATCHERS_MAX:
            oldest = min(_watchers.values(), key=lambda e: e["last_seen"])
            del _watchers[oldest["holder"]]
        entry = _watchers.get(holder)
        if entry is None or now >= entry["last_seen"] + entry["timeout_seconds"]:
            entry = {"holder": holder, "since": now, "timeout_seconds": default}
            _watchers[holder] = entry
        if timeout_seconds is not None:
            entry["timeout_seconds"] = _clamp_number(
                timeout_seconds, default, WATCHER_TIMEOUT_MIN, WATCHER_TIMEOUT_MAX
            )
        entry["last_seen"] = now
        return dict(entry)


def _expire_watchers_locked(now):
    for holder in [h for h, e in _watchers.items() if now >= e["last_seen"] + e["timeout_seconds"]]:
        del _watchers[holder]


def _current_watchers(now=None):
    """Live watchers (lazy expiry), oldest first."""
    now = time.time() if now is None else now
    with _watch_lock:
        _expire_watchers_locked(now)
        return sorted((dict(e) for e in _watchers.values()), key=lambda e: e["since"])


def _active_reservation_locked(now):
    """Reserved holder if the grace window is still open, else None.

    Review L2: a reservation that lapses UNUSED also drops that holder
    from the queue (it keeps no priority it did not use). Before, the
    lapsed head stayed first in line and was re-reserved on every
    release, so one abandoned session blocked every handoff for
    RESERVATION_GRACE seconds, indefinitely.
    """
    global _reserved_for, _reserved_until
    if _reserved_for is None:
        return None
    if now < _reserved_until:
        return _reserved_for
    lapsed = _reserved_for
    _reserved_for = None
    _reserved_until = 0.0
    _dequeue_locked(lapsed)
    return None


def _prune_queue_locked(now):
    """Drop queue entries not refreshed within QUEUE_ENTRY_TTL (review L2)."""
    fresh = [
        e for e in _claim_queue
        if now - float(e.get("refreshed_at", e.get("enqueued_at", now))) < QUEUE_ENTRY_TTL
    ]
    if len(fresh) != len(_claim_queue):
        _claim_queue[:] = fresh


def _reserve_head_locked(now):
    """Give the queue head an exclusive claim window, if none is active."""
    global _reserved_for, _reserved_until
    if _claim_queue and _reserved_for is None:
        _reserved_for = _claim_queue[0]["holder"]
        _reserved_until = now + RESERVATION_GRACE


def _current_claim_locked(now):
    global _claim
    _prune_queue_locked(now)
    if _claim is not None and now >= float(_claim.get("expires_at", 0.0)):
        # Lazy expiry: the claim is gone, and the queue head (if any)
        # earns a reservation from this moment of discovery.
        _claim = None
        _reserve_head_locked(now)
    return _claim


def _current_claim(now=None):
    """Copy of the live claim, or None if free / expired."""
    if now is None:
        now = time.time()
    with _claim_lock:
        current = _current_claim_locked(now)
        return dict(current) if current is not None else None


def _claim_snapshot(now=None):
    """(claim copy or None, queue copies, reserved holder or None)."""
    if now is None:
        now = time.time()
    with _claim_lock:
        current = _current_claim_locked(now)
        reserved = _active_reservation_locked(now)
        return (
            dict(current) if current is not None else None,
            [dict(entry) for entry in _claim_queue],
            reserved,
        )


def _enqueue_locked(holder, purpose, now):
    """Append to the FIFO queue (deduped by holder); returns the 1-based
    position, or None when the queue is full. Re-queueing an existing
    holder keeps its position and refreshes the entry (review L2)."""
    for idx, entry in enumerate(_claim_queue):
        if entry["holder"] == holder:
            entry["refreshed_at"] = now
            return idx + 1
    if len(_claim_queue) >= CLAIM_QUEUE_MAX:
        return None
    _claim_queue.append(
        {"holder": holder, "purpose": purpose, "enqueued_at": now, "refreshed_at": now}
    )
    return len(_claim_queue)


def _queue_full_response(claim, reserved=None):
    body = {
        "ok": False,
        "error": f"claim queue is full ({CLAIM_QUEUE_MAX} waiting)",
        "queue_full": True,
        "claim": claim,
    }
    if reserved is not None:
        body["reserved_for"] = reserved
    return jsonify(body), 409


def _dequeue_locked(holder):
    for idx, entry in enumerate(_claim_queue):
        if entry["holder"] == holder:
            del _claim_queue[idx]
            return True
    return False


def _reset_coordination_state():
    """Clear claim + receipt state. Test hook / internal use only."""
    global _claim, _reserved_for, _reserved_until
    with _claim_lock:
        _claim = None
        _claim_queue.clear()
        _reserved_for = None
        _reserved_until = 0.0
    with _receipts_lock:
        _receipts.clear()
        _pending_payloads.clear()
    global _watcher_timeout
    with _watch_lock:
        _watchers.clear()
        _watcher_timeout = None
    with _frame_hash_cache_lock:
        _frame_hash_cache.clear()


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 16MB file-drop is ~22MB base64; hands still caps the file at 16MB

# ---------------------------------------------------------------------------
# Owner-visible events (review E4). Gateway auth failures used to go only
# to app.logger, which the windowed packaged app never shows, and nothing
# alerted -- although the gateway is the one service reachable from
# outside the PC. The packaged app drains log_queue into its window (as it
# does for the hands) and points ALERT_HOOK at the hands' ntfy alert; a
# standalone gateway can alert by itself via RELAY_NTFY_TOPIC.
# ---------------------------------------------------------------------------
LOG_QUEUE_MAX = 1000
log_queue = _queue_mod.Queue(maxsize=LOG_QUEUE_MAX)
ALERT_HOOK = None  # callable(title, message), set by the packaged app
NTFY_TOPIC = os.environ.get("RELAY_NTFY_TOPIC", "").strip()


def log_event(message):
    """Record an owner-relevant event: app logger + the window's queue."""
    app.logger.warning(message)
    try:
        log_queue.put_nowait(message)
    except _queue_mod.Full:
        pass  # nobody is draining (standalone): never block a request on it


def _post_ntfy(title, message):
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Tags": "warning"},
            timeout=10,
        )
    except requests.RequestException:
        pass


def _alert(title, message):
    """Best-effort phone alert; never raises, never blocks the request."""
    hook = ALERT_HOOK
    if callable(hook):
        try:
            hook(title, message)
        except Exception:
            pass
    elif NTFY_TOPIC:
        threading.Thread(target=_post_ntfy, args=(title, message), daemon=True).start()


def _json_body():
    """The request's JSON object, or {} for a missing/invalid/non-object body.

    Review E6: the old `get_json(force=True, silent=True) or {}` idiom let
    a JSON array or string through, and the next `.get` raised a 500.
    """
    body = request.get_json(force=True, silent=True)
    return body if isinstance(body, dict) else {}


def _short(value, limit=64):
    """A bounded printable form of a client-supplied value (review E6)."""
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


_fail_by_peer = {}
_lockout_by_peer = {}
PEER_SWEEP_AT = 256  # review L6: sweep stale per-peer entries past this many peers


def _peer_key():
    try:
        return request.remote_addr or "-"
    except RuntimeError:
        return "-"


def _sweep_peers_locked(now):
    """Drop per-peer auth state that can no longer matter (review L6)."""
    for peer in [p for p, until in _lockout_by_peer.items() if now >= until]:
        del _lockout_by_peer[peer]
    for peer in [p for p, ts in _fail_by_peer.items() if not ts or now - ts[-1] >= FAIL_WINDOW]:
        del _fail_by_peer[peer]


def _gate_decision(provided):
    """Decide and record in one critical section -> (decision, tripped).

    A request that already presents the valid token is never locked out,
    even when this peer is over the failure budget. Empty TOKEN fails closed.
    tripped is True for the one failure that starts a lockout.
    """
    now = time.monotonic()
    peer = _peer_key()
    with _fail_lock:
        if TOKEN and hmac.compare_digest(provided, TOKEN):
            return "ok", False
        until = _lockout_by_peer.get(peer)
        if until is not None:
            if now < until:
                return "locked", False
            del _lockout_by_peer[peer]  # expired: forget it (review L6)
        times = [t for t in _fail_by_peer.get(peer, ()) if now - t < FAIL_WINDOW]
        times.append(now)
        tripped = len(times) >= MAX_FAILS
        if tripped:
            _lockout_by_peer[peer] = now + LOCKOUT_SECONDS
            _fail_by_peer.pop(peer, None)
        else:
            _fail_by_peer[peer] = times
        if len(_fail_by_peer) + len(_lockout_by_peer) > PEER_SWEEP_AT:
            _sweep_peers_locked(now)
        return "bad", tripped


@app.before_request
def gate():
    if request.path == "/health":
        return
    provided = request.headers.get("X-Relay-Token") or ""
    decision, tripped = _gate_decision(provided)
    if decision == "locked":
        return jsonify({"ok": False, "error": "locked out"}), 429
    if decision != "ok":
        peer = _peer_key()
        log_event(f"rejected request to {request.path} from {peer} (bad gateway token)")
        if tripped:
            log_event(
                f"LOCKOUT: {MAX_FAILS} bad gateway tokens from {peer} inside "
                f"{FAIL_WINDOW // 60} minutes - authentication paused for "
                f"{LOCKOUT_SECONDS // 60} minutes"
            )
            _alert(
                "HandsRelay gateway LOCKOUT",
                f"{MAX_FAILS} bad gateway-token guesses from {peer}. "
                f"Authentication is paused for {LOCKOUT_SECONDS // 60} minutes.",
            )
        return jsonify({"ok": False, "error": "bad token"}), 401


@app.after_request
def noindex(resp):
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@app.after_request
def integrity(resp):
    """R9 (#22): make a torn or emptied body detectable as torn.

    Every buffered response carries an explicit Content-Length and
    X-Body-SHA256 (hex digest of the exact body bytes). A client that
    receives a 2xx whose bytes do not hash to the header — including
    an empty body where the header names a non-empty one — knows the
    read was damaged in transit (proxy/tunnel), not "empty but valid".
    The gateway itself never emits an empty 2xx JSON body: every JSON
    route returns at least {"ok": ...}. Streamed responses are left
    alone (their length is not known up front).
    """
    if resp.direct_passthrough or resp.is_streamed:
        return resp
    data = resp.get_data()
    resp.headers["Content-Length"] = str(len(data))
    resp.headers["X-Body-SHA256"] = hashlib.sha256(data).hexdigest()
    return resp


def _hands_headers():
    return {"X-Secret": HANDS_SECRET}


def _probe(url, headers=None):
    try:
        r = requests.get(url, headers=headers or {}, timeout=2)
        return r.status_code == 200
    except requests.RequestException:
        return False


def _fetch_pc_state():
    """Hands GET /state (wishlist #13), or None on any failure."""
    try:
        r = requests.get(f"{HANDS_URL}/state", headers=_hands_headers(), timeout=2)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        return None
    return data


def _reachability():
    """Cached (5s) reachability of the two backends, for /v1/info.

    Returns (hands_ok, parser_ok, pc_state); pc_state is the hands
    /state payload, or None when the hands is unreachable or /state
    errors.
    """
    now = time.monotonic()
    with _probe_lock:
        if now - _probe_cache["at"] < 5 and _probe_cache["hands"] is not None:
            return _probe_cache["hands"], _probe_cache["parser"], _probe_cache.get("pc_state")
    hands = _probe(f"{HANDS_URL}/health", _hands_headers())
    parser = _probe(f"{PARSER_URL}/health")
    pc_state = _fetch_pc_state() if hands else None
    with _probe_lock:
        _probe_cache.update({"hands": hands, "parser": parser, "pc_state": pc_state, "at": now})
    return hands, parser, pc_state


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "gateway"})


@app.get("/v1/info")
def info():
    hands_ok, parser_ok, pc_state = _reachability()
    return jsonify(
        {
            "ok": True,
            "hands_url": HANDS_URL,
            "hands_secret_configured": bool(HANDS_SECRET),
            "hands_reachable": hands_ok,
            "parser_url": PARSER_URL,
            "parser_reachable": parser_ok,
            "hands_claim": _current_claim(),
            "pc_state": pc_state,
            "watchers": _current_watchers(),
            "watcher_timeout_seconds": _watcher_timeout_setting(),
        }
    )


@app.get("/v1/watch")
def watch_status():
    return jsonify(
        {"ok": True, "watchers": _current_watchers(), "timeout_seconds": _watcher_timeout_setting()}
    )


@app.post("/v1/watch")
def watch_touch():
    """{holder, timeout_seconds?}: start or refresh watcher (eyes-only) mode."""
    body = _json_body()
    entry = _touch_watcher(body.get("holder"), body.get("timeout_seconds"))
    if entry is None:
        return jsonify({"ok": False, "error": "holder is required"}), 400
    return jsonify({"ok": True, "watcher": entry})


@app.post("/v1/watch/release")
def watch_release():
    body = _json_body()
    holder = str(body.get("holder") or "").strip()
    if not holder:
        return jsonify({"ok": False, "error": "holder is required"}), 400
    with _watch_lock:
        removed = _watchers.pop(holder, None) is not None
    return jsonify({"ok": True, "released": removed})


@app.post("/v1/watch/config")
def watch_config():
    """{timeout_seconds}: gateway-wide watcher timeout (30-60 s)."""
    global _watcher_timeout
    body = _json_body()
    value = body.get("timeout_seconds")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return jsonify({"ok": False, "error": "timeout_seconds must be a number"}), 400
    value = _clamp_number(value, WATCHER_TIMEOUT_DEFAULT, WATCHER_TIMEOUT_MIN, WATCHER_TIMEOUT_MAX)
    _watcher_timeout_setting()
    with _watch_lock:
        _watcher_timeout = value
    return jsonify({"ok": True, "timeout_seconds": value})


@app.get("/v1/hands/claim")
def hands_claim_status():
    claim, queue, reserved = _claim_snapshot()
    return jsonify({"ok": True, "claim": claim, "queue": queue, "reserved_for": reserved})


@app.post("/v1/hands/claim")
def hands_claim():
    global _claim, _reserved_for, _reserved_until
    body = _json_body()
    holder = str(body.get("holder") or "").strip()[:HOLDER_MAX_CHARS]
    if not holder:
        return jsonify({"ok": False, "error": "holder is required"}), 400
    purpose = str(body.get("purpose") or "")[:PURPOSE_MAX_CHARS]
    ttl = _clamp_number(
        body.get("ttl_seconds", CLAIM_TTL_DEFAULT),
        CLAIM_TTL_DEFAULT,
        CLAIM_TTL_MIN,
        CLAIM_TTL_MAX,
    )
    want_queue = bool(body.get("queue"))
    now = time.time()
    with _claim_lock:
        current = _current_claim_locked(now)
        if current is not None and current["holder"] == holder:
            # Same holder re-claiming refreshes the lease.
            current["purpose"] = purpose or current["purpose"]
            current["expires_at"] = now + ttl
            _push_claim_locked(current, now)
            return jsonify({"ok": True, "granted": True, "claim": dict(current)})
        if current is not None:
            if want_queue:
                position = _enqueue_locked(holder, purpose, now)
                if position is None:
                    return _queue_full_response(dict(current))
                return jsonify(
                    {
                        "ok": True,
                        "granted": False,
                        "queued": True,
                        "position": position,
                        "claim": dict(current),
                    }
                )
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": f"hands already claimed by {current['holder']!r}",
                        "claim": dict(current),
                    }
                ),
                409,
            )
        # Desktop is free. An active reservation (queue head's grace
        # window after a release/expiry) blocks everyone else.
        reserved = _active_reservation_locked(now)
        if reserved is not None and reserved != holder:
            if want_queue:
                position = _enqueue_locked(holder, purpose, now)
                if position is None:
                    return _queue_full_response(None, reserved)
                return jsonify(
                    {
                        "ok": True,
                        "granted": False,
                        "queued": True,
                        "position": position,
                        "claim": None,
                        "reserved_for": reserved,
                    }
                )
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": f"hands reserved for {reserved!r} (queue head grace window)",
                        "claim": None,
                        "reserved_for": reserved,
                    }
                ),
                409,
            )
        if reserved == holder:
            _reserved_for = None
            _reserved_until = 0.0
            _dequeue_locked(holder)
        elif _claim_queue and _claim_queue[0]["holder"] == holder:
            _dequeue_locked(holder)
        elif _claim_queue:
            # A queue outlived its reservation window: the claim is a
            # free-for-all again, but a queued caller keeps no priority
            # it did not use — drop its stale entry if it jumps in.
            _dequeue_locked(holder)
        _claim = {
            "holder": holder,
            "purpose": purpose,
            "since": now,
            "expires_at": now + ttl,
        }
        _push_claim_locked(_claim, now)
        return jsonify({"ok": True, "granted": True, "claim": dict(_claim)})


@app.post("/v1/hands/release")
def hands_release():
    global _claim, _reserved_for, _reserved_until
    body = _json_body()
    holder = str(body.get("holder") or "").strip()[:HOLDER_MAX_CHARS]
    if not holder:
        return jsonify({"ok": False, "error": "holder is required"}), 400
    now = time.time()
    with _claim_lock:
        if body.get("cancel_queue"):
            removed = _dequeue_locked(holder)
            if removed and _reserved_for == holder:
                _reserved_for = None
                _reserved_until = 0.0
                if _current_claim_locked(now) is None:
                    _reserve_head_locked(now)
            claim, queue, reserved = _claim_snapshot(now)
            return jsonify(
                {
                    "ok": True,
                    "cancelled_queue": removed,
                    "claim": claim,
                    "queue": queue,
                    "reserved_for": reserved,
                }
            )
        current = _current_claim_locked(now)
        if current is None:
            # Already expired (or never held). Expiry may have just reserved
            # the queue head; report that same snapshot, including reserved_for.
            claim, queue, reserved = _claim_snapshot(now)
            return jsonify(
                {
                    "ok": True,
                    "released": False,
                    "claim": claim,
                    "queue": queue,
                    "reserved_for": reserved,
                }
            )
        if current["holder"] != holder:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "only the current holder can release the claim",
                        "claim": dict(current),
                    }
                ),
                403,
            )
        _claim = None
        _push_claim_locked(None, now)
        _reserve_head_locked(now)
        return jsonify(
            {
                "ok": True,
                "released": True,
                "claim": None,
                "reserved_for": _reserved_for,
            }
        )


@app.post("/v1/parse")
def parse():
    """Forward a screenshot (JSON {"image_b64": ...}) to the parser service."""
    try:
        r = requests.post(f"{PARSER_URL}/parse", json=_json_body(), timeout=120)
    except requests.RequestException as exc:
        return jsonify({"ok": False, "error": f"parser unreachable: {exc}"}), 502
    try:
        return jsonify(r.json()), r.status_code
    except ValueError:
        # R4 (F12): a non-JSON parser reply (e.g. a waitress 500 page
        # during a crash loop) must not escape as Flask's HTML 500 —
        # drivers of this API parse JSON or nothing.
        return jsonify({"ok": False, "error": f"parser returned {r.status_code}"}), r.status_code


def _region_ok(region):
    """Gateway-side check of a region string: 'x,y,w,h', ints, w,h > 0."""
    try:
        parts = [int(p) for p in str(region).split(",")]
    except ValueError:
        return False
    return len(parts) == 4 and parts[2] > 0 and parts[3] > 0


def _screen_fetch_kwargs():
    """(kwargs, error) for a screen fetch, from the request's query args.

    monitor/region are only included when actually present, so a
    no-param call still invokes _fetch_screenshot() with zero arguments
    (existing stubs rely on that). Region format is validated here so a
    bad region is a 400 without the hands being called at all.
    """
    kwargs = {}
    region = request.args.get("region")
    if region is not None:
        if not _region_ok(region):
            return None, (
                jsonify(
                    {"ok": False, "error": "region must be 'x,y,w,h' (ints, w and h > 0)"}
                ),
                400,
            )
        kwargs["region"] = region
    monitor = request.args.get("monitor")
    if monitor is not None:
        try:
            kwargs["monitor"] = int(monitor)
        except ValueError:
            return None, (
                jsonify({"ok": False, "error": "monitor must be an integer"}),
                400,
            )
    return kwargs, None


@app.post("/v1/find")
def find():
    """Forward a find-text request to the parser service's /find."""
    body = _json_body()
    payload = {
        "image_b64": body.get("image_b64") or "",
        "text": body.get("text") or "",
        "case_sensitive": bool(body.get("case_sensitive")),
    }
    try:
        r = requests.post(f"{PARSER_URL}/find", json=payload, timeout=120)
    except requests.RequestException as exc:
        return jsonify({"ok": False, "error": f"parser unreachable: {exc}"}), 502
    try:
        return jsonify(r.json()), r.status_code
    except ValueError:
        # R4 (F12): same non-JSON guard as /v1/parse.
        return jsonify({"ok": False, "error": f"parser returned {r.status_code}"}), r.status_code


def _hands_forward(method, path, payload=None):
    """Forward one call to the hands, act()-style error passthrough.

    503 when no HANDS_SECRET is configured, 502 when the hands are
    unreachable or reject the gateway's secret.
    """
    if not HANDS_SECRET:
        return jsonify({"ok": False, "error": "HANDS_SECRET not configured on gateway"}), 503
    try:
        if method == "GET":
            r = requests.get(
                f"{HANDS_URL}{path}", headers=_hands_headers(), timeout=HANDS_TIMEOUT
            )
        else:
            r = requests.post(
                f"{HANDS_URL}{path}",
                json=payload,
                headers=_hands_headers(),
                timeout=HANDS_TIMEOUT,
            )
    except requests.RequestException as exc:
        return jsonify({"ok": False, "error": f"hands unreachable: {exc}"}), 502
    if r.status_code == 401:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "hands rejected the gateway's secret — HANDS_SECRET does not match the hands app's current session secret",
                }
            ),
            502,
        )
    try:
        return jsonify(r.json()), r.status_code
    except ValueError:
        return jsonify({"ok": r.ok, "status": r.status_code}), r.status_code


@app.post("/v1/elevate")
def elevate():
    """Request an elevated launch on the host.

    Body: {"name": "<allowlisted name>"}. The gateway supplies only a
    name; what runs is fixed by the operator's hands config, and the
    launch happens only if the person at the PC approves it there.
    """
    body = _json_body()
    return _hands_forward("POST", "/elevate", {"name": body.get("name") or ""})


@app.get("/v1/elevate/<eid>")
def elevate_status(eid):
    """Poll the state of an elevation request by id."""
    return _hands_forward("GET", f"/elevate/{eid}")


def _fetch_screenshot(monitor=None, region=None):
    """One screenshot from the hands.

    With monitor=N appends ?monitor=N; with region="x,y,w,h" (validated
    here, in the same format the hands expect) appends ?region=... .
    With neither, the hands URL is exactly the historical one.

    Returns (png_bytes, content_type, None) on success, or
    (None, None, error_response) with the same failures /v1/screen has
    always surfaced.
    """
    if region is not None and not _region_ok(region):
        return None, None, (
            jsonify({"ok": False, "error": "region must be 'x,y,w,h' (ints, w and h > 0)"}),
            400,
        )
    url = f"{HANDS_URL}/screenshot"
    params = []
    if monitor is not None:
        params.append(f"monitor={int(monitor)}")
    if region is not None:
        params.append(f"region={region}")
    if params:
        url += "?" + "&".join(params)
    if not HANDS_SECRET:
        return None, None, (
            jsonify({"ok": False, "error": "HANDS_SECRET not configured on gateway"}),
            503,
        )
    try:
        r = requests.get(url, headers=_hands_headers(), timeout=HANDS_TIMEOUT)
    except requests.RequestException as exc:
        return None, None, (jsonify({"ok": False, "error": f"hands unreachable: {exc}"}), 502)
    if r.status_code == 401:
        return None, None, (
            jsonify(
                {
                    "ok": False,
                    "error": "hands rejected the gateway's secret — HANDS_SECRET does not match the hands app's current session secret",
                }
            ),
            502,
        )
    if r.status_code != 200:
        return None, None, (
            jsonify({"ok": False, "error": f"hands returned {r.status_code}"}),
            502,
        )
    return r.content, r.headers.get("Content-Type", "image/png"), None


class _HashUnavailable(Exception):
    """Pillow is not installed on the gateway, so frames cannot be decoded."""


class _UndecodableFrame(Exception):
    """The hands returned bytes that are not a decodable image (review E7)."""


def _undecodable_response():
    return (
        jsonify({"ok": False, "error": "hands returned a frame that could not be decoded"}),
        502,
    )


def _open_rgb(png_bytes):
    """PNG bytes -> RGB image. _HashUnavailable without Pillow;
    _UndecodableFrame for truncated/corrupt/non-image bytes, which used to
    escape the routes as an HTML 500."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise _HashUnavailable() from exc
    try:
        return Image.open(BytesIO(png_bytes)).convert("RGB")
    except Exception as exc:  # UnidentifiedImageError, truncated data, bombs
        raise _UndecodableFrame(str(exc)) from exc


# Review I4: an unchanged screen produces byte-identical PNGs, so the masked
# hash of a recently seen frame is reused instead of decoding it again (a
# /v1/screen/wait long-poll re-hashes every 200 ms; /v1/screen hashes every
# frame for its ETag). Bounded LRU keyed by the PNG's own SHA-256 + mask.
_FRAME_HASH_CACHE_MAX = 8
_frame_hash_cache = OrderedDict()
_frame_hash_cache_lock = threading.Lock()


def _decode_masked(png_bytes, mask_bottom):
    """PNG bytes -> RGB image with the bottom mask_bottom rows removed."""
    img = _open_rgb(png_bytes)
    mask = max(0, min(int(mask_bottom), img.height - 1))
    if mask:
        img = img.crop((0, 0, img.width, img.height - mask))
    return img


def _frame_hash(png_a, png_b, mask_bottom, size_match_out=None):
    """SHA-256 of a screenshot's masked pixels.

    With png_b given (stable mode), the two frames are composited first
    with a per-pixel minimum (ImageChops.darker). The minimum is
    symmetric, so the result does not depend on which sample caught a
    blink's bright phase — and a bright transient on a darker ground
    (a terminal caret) drops out entirely, leaving the untouched
    frame. Only persistent content moves the hash.
    Returns (hex_digest, width, height) of the masked frame.
    size_match_out, when a list, receives True/False: False means the two
    samples differed in size and were NOT composited. Callers that asked
    for stable=1 must report stable false in that case. The 3-tuple return
    is unchanged so existing unpackers keep working.
    """
    key = None
    if png_b is None and isinstance(png_a, (bytes, bytearray)):
        key = (hashlib.sha256(png_a).digest(), int(mask_bottom))
        with _frame_hash_cache_lock:
            hit = _frame_hash_cache.get(key)
            if hit is not None:
                _frame_hash_cache.move_to_end(key)
        if hit is not None:
            if size_match_out is not None:
                size_match_out.append(True)
            return hit
    img_a = _decode_masked(png_a, mask_bottom)
    sizes_match = True
    if png_b is not None:
        from PIL import ImageChops

        img_b = _decode_masked(png_b, mask_bottom)
        if img_b.size == img_a.size:
            img_a = ImageChops.darker(img_a, img_b)
        else:
            sizes_match = False
    if size_match_out is not None:
        size_match_out.append(sizes_match)
    result = (hashlib.sha256(img_a.tobytes()).hexdigest(), img_a.width, img_a.height)
    if key is not None:
        with _frame_hash_cache_lock:
            _frame_hash_cache[key] = result
            _frame_hash_cache.move_to_end(key)
            while len(_frame_hash_cache) > _FRAME_HASH_CACHE_MAX:
                _frame_hash_cache.popitem(last=False)
    return result


def _region_hash(png_bytes, region):
    """SHA-256 of one region's pixels (wishlist #5).

    region is [x, y, w, h]; it is clamped to the frame. Returns
    (hex_digest, width, height) of the cropped region.
    """
    img = _open_rgb(png_bytes)
    x, y, w, h = (int(v) for v in region)
    if w <= 0 or h <= 0:
        raise ValueError("region width/height must be positive")
    x0 = max(0, min(x, img.width))
    y0 = max(0, min(y, img.height))
    x1 = max(x0, min(x + w, img.width))
    y1 = max(y0, min(y + h, img.height))
    cropped = img.crop((x0, y0, x1, y1))
    return hashlib.sha256(cropped.tobytes()).hexdigest(), cropped.width, cropped.height


@app.get("/v1/monitors")
def monitors():
    """Monitor list from the hands, proxied (rectangles + count)."""
    return _proxy_hands_get("/monitors")


@app.get("/v1/screen")
def screen():
    """Current screenshot from the hands, proxied as PNG bytes.

    ?monitor=N / ?region=x,y,w,h select a capture variant on the hands;
    the ETag below then describes that variant. Carries an ETag (the
    masked-frame hash, as in /v1/screen/hash) when
    the gateway can decode images; a matching If-None-Match gets a 304
    so pollers can skip re-downloading unchanged frames.
    """
    if request.args.get("watcher"):
        _touch_watcher(request.args.get("watcher"))  # R11 watcher mode
    kwargs, err = _screen_fetch_kwargs()
    if err is not None:
        return err
    png, content_type, err = _fetch_screenshot(**kwargs)
    if err is not None:
        return err
    resp = Response(png, content_type=content_type)
    try:
        digest, _, _ = _frame_hash(png, None, DEFAULT_MASK_BOTTOM)
    except _HashUnavailable:
        return resp
    except _UndecodableFrame as exc:
        # Review E7: serve the bytes as received, just without an ETag,
        # rather than failing the whole route with an HTML 500.
        app.logger.warning("screen frame not decodable for ETag: %s", exc)
        return resp
    etag = f'"{digest}"'
    resp.headers["ETag"] = etag
    inm = (request.headers.get("If-None-Match") or "").strip()
    if inm and inm.strip('"') == digest:
        resp.status_code = 304
        resp.set_data(b"")
    return resp


@app.get("/v1/screen/hash")
def screen_hash():
    """Hash of the current screenshot, without moving the PNG.

    A watcher polls this (tiny JSON) instead of the full frame and only
    fetches /v1/screen when the hash changes. The bottom mask_bottom px
    (default 80, the taskbar) are excluded. With ?stable=1 the gateway
    samples two frames ~0.65s apart and hashes only the pixels that
    agree, so a blinking caret cannot flip the result. ?monitor=N /
    ?region=x,y,w,h select the capture variant being hashed.
    """
    if request.args.get("watcher"):
        _touch_watcher(request.args.get("watcher"))  # R11 watcher mode
    stable = (request.args.get("stable") or "").lower() in ("1", "true", "yes")
    try:
        mask_bottom = int(request.args.get("mask_bottom", DEFAULT_MASK_BOTTOM))
    except ValueError:
        return jsonify({"ok": False, "error": "mask_bottom must be an integer"}), 400
    mask_bottom = max(0, min(mask_bottom, 2000))

    kwargs, err = _screen_fetch_kwargs()
    if err is not None:
        return err
    png_a, _, err = _fetch_screenshot(**kwargs)
    if err is not None:
        return err
    png_b = None
    if stable:
        time.sleep(STABLE_SAMPLE_GAP)
        png_b, _, err = _fetch_screenshot(**kwargs)
        if err is not None:
            return err
    size_match = []
    try:
        digest, width, height = _frame_hash(png_a, png_b, mask_bottom, size_match)
    except _HashUnavailable:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "image decoding unavailable on gateway (pillow not installed)",
                }
            ),
            501,
        )
    except _UndecodableFrame:
        return _undecodable_response()
    return jsonify(
        {
            "ok": True,
            "hash": digest,
            "stable": bool(stable and (size_match[0] if size_match else True)),
            "mask_bottom": mask_bottom,
            "width": width,
            "height": height,
        }
    )


@app.get("/v1/screen/wait")
def screen_wait():
    """Long-poll sibling of /v1/screen/hash.

    Holds until the masked frame hash differs from `since` (also accepted
    as `hash`) or timeout elapses (default 25s, capped at 25). Returns the
    current hash JSON plus timed_out. Reuses _frame_hash / _fetch_screenshot.
    Single-sample (not the two-frame stable composite) so a 25s hold is not
    a chain of 0.65s gaps.
    """
    if request.args.get("watcher"):
        _touch_watcher(request.args.get("watcher"))  # R11 watcher mode
    since = request.args.get("since", request.args.get("hash", ""))
    if not isinstance(since, str):
        since = ""
    try:
        timeout = float(request.args.get("timeout", "25"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "timeout must be a number"}), 400
    if timeout != timeout or timeout in (float("inf"), float("-inf")):
        return jsonify({"ok": False, "error": "timeout must be a number"}), 400
    timeout = min(max(timeout, 0.0), 25.0)
    try:
        mask_bottom = int(request.args.get("mask_bottom", DEFAULT_MASK_BOTTOM))
    except ValueError:
        return jsonify({"ok": False, "error": "mask_bottom must be an integer"}), 400
    mask_bottom = max(0, min(mask_bottom, 2000))
    kwargs, err = _screen_fetch_kwargs()
    if err is not None:
        return err
    deadline = time.monotonic() + timeout
    timed_out = True
    digest = width = height = None
    while True:
        png, _, err = _fetch_screenshot(**kwargs)
        if err is not None:
            return err
        try:
            digest, width, height = _frame_hash(png, None, mask_bottom)
        except _HashUnavailable:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "image decoding unavailable on gateway (pillow not installed)",
                    }
                ),
                501,
            )
        except _UndecodableFrame:
            return _undecodable_response()
        if digest != since:
            timed_out = False
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.2, remaining))
    return jsonify(
        {
            "ok": True,
            "hash": digest,
            "timed_out": timed_out,
            "stable": False,
            "mask_bottom": mask_bottom,
            "width": width,
            "height": height,
        }
    )


# ---------------------------------------------------------------------------
# Queued act (wishlist #18): yielded-input accounting.
#
# The hands answer 409 when the desktop's owner is actively using it and
# an act yields. /v1/act surfaces that as-is; /v1/act/queued instead
# files a receipt and retries until a hold deadline, so the caller can
# account for the input later instead of guessing whether it landed.
#
# Privacy: a receipt records the op and the payload's key NAMES only —
# never payload values (typed text may be sensitive). The payload itself
# lives in a separate transient map, only while the receipt is held, and
# is dropped the moment the receipt reaches a terminal state; it never
# appears in any receipt response.
# ---------------------------------------------------------------------------
RECEIPT_MAX = 200
RECEIPT_RETRY_INTERVAL = 1.0
HOLD_DEFAULT = 30
HOLD_MIN = 1
HOLD_MAX = 120

_receipts_lock = threading.RLock()
_receipts = {}  # id -> receipt dict, insertion-ordered (oldest first)
_pending_payloads = {}  # receipt id -> payload, held receipts only
_receipt_thread = None
# Review L3: set when this module instance is retired (the packaged app's
# self-heal reloads the gateway in-process); the retry loop then exits
# instead of running -- and pinning the old module -- forever.
_receipt_stop = threading.Event()
PARAM_KEYS_MAX = 32  # review E6: receipts keep a bounded list of key names


def _is_yield_conflict(status, result):
    """Classify a hands 409 as a WP0b yield (hold and retry) or not.

    R4 (F11): the hands' structured error_code is authoritative when
    present ("yield" = owner active, "locked" = desktop locked; both
    mean hold and retry). The substring match on the human prose is
    only a fallback for an older hands that predates the code —
    rewording an error message must not silently reclassify holds as
    failures or failures as holds.
    """
    if status != 409 or not isinstance(result, dict):
        return False
    code = result.get("error_code")
    if code is not None:
        return code in ("yield", "locked")
    error = str(result.get("error") or "").lower()
    return "yield" in error or "user active" in error


def _store_receipt(receipt, payload=None):
    with _receipts_lock:
        _receipts[receipt["id"]] = receipt
        if payload is not None:
            _pending_payloads[receipt["id"]] = payload
        while len(_receipts) > RECEIPT_MAX:
            # R4 (F4): evict TERMINAL receipts before held ones —
            # settled history is expendable, and a burst of direct
            # applies must not push out a genuinely held receipt. The
            # just-stored receipt is never its own victim.
            victim = None
            for rid, rec in _receipts.items():
                if rid != receipt["id"] and rec.get("state") != "held":
                    victim = rid
                    break
            if victim is None:
                victim = next(iter(_receipts))
            evicted = _receipts.pop(victim)
            # Mark it BEFORE dropping it: the retry loop may already hold
            # a reference to this dict, and _pump_receipt only forwards
            # receipts whose state is still "held".
            if evicted["state"] == "held":
                evicted["state"] = "expired"
            _pending_payloads.pop(victim, None)


def _public_receipt(receipt):
    return {k: v for k, v in receipt.items() if not k.startswith("_")}


def _pump_receipt(receipt, now):
    """One retry step for a held receipt. Returns the receipt.

    Synchronous and thread-free so tests can drive it directly: attempts
    the forward once; a non-409 answer settles the receipt (applied, or
    failed when the hands turned out unreachable), a 409 past the
    deadline settles it as refused-yielded, otherwise it stays held.
    A receipt whose hold deadline has already passed is settled
    refused-yielded WITHOUT forwarding (review E2).
    """
    with _receipts_lock:
        if receipt.get("state") != "held":
            return receipt
        if receipt["id"] not in _pending_payloads:
            # The payload is gone (evicted or settled elsewhere). NEVER
            # forward an op with an empty body in its place: an empty
            # break fires a real Ctrl+C at the desktop, an empty scroll
            # scrolls by the hands' defaults. Settle honestly instead.
            receipt["state"] = "expired"
            receipt["updated_at"] = now
            return receipt
        if now >= receipt["deadline"]:
            # Review E2: never START a forward after the hold window. The
            # retry loop is serial (it must preserve submission order), so
            # a slow act -- a long type runs for minutes -- can delay the
            # next receipt's turn past its deadline; forwarding it then
            # would land input long after the caller's hold had lapsed.
            # Its last attempt was a yield, so it settles refused-yielded.
            receipt["state"] = "refused-yielded"
            receipt["updated_at"] = now
            _pending_payloads.pop(receipt["id"], None)
            return receipt
        payload = dict(_pending_payloads[receipt["id"]])
        op = receipt["op"]
    try:
        status, result = _forward_act(op, payload)
    except Exception as exc:  # a stubbed / broken forward must not kill the pump
        status, result = None, {"ok": False, "error": f"forward failed: {exc}"}
        failed = True
        yielded = False
    else:
        yielded = _is_yield_conflict(status, result)
        # 2xx applied. Yield-409 stays held (deadline still -> refused-yielded).
        # Everything else, including 400/500/503/502 and a non-yield 409, fails.
        failed = not (isinstance(status, int) and 200 <= status < 300)
    with _receipts_lock:
        receipt["updated_at"] = now
        if status is not None:
            receipt["hands_status"] = status
        if yielded:
            if now >= receipt["deadline"]:
                receipt["state"] = "refused-yielded"
                _pending_payloads.pop(receipt["id"], None)
            return receipt
        if failed or status is None:
            receipt["state"] = "failed"
            receipt["result"] = None
            _pending_payloads.pop(receipt["id"], None)
            return receipt
        receipt["state"] = "applied"
        receipt["applied_via"] = "after-hold"
        receipt["result"] = result
        _pending_payloads.pop(receipt["id"], None)
        return receipt


def _receipt_tick(clock=time.time):
    """One pass over the held receipts, oldest first (submission order).

    The clock is read again for EACH receipt, so a receipt whose turn
    comes after a slow forward is judged against the real time then.
    """
    with _receipts_lock:
        held = [r for r in _receipts.values() if r.get("state") == "held"]
    for receipt in held:
        if _receipt_stop.is_set():
            return
        try:
            _pump_receipt(receipt, clock())
        except Exception:
            continue


def _receipt_loop():
    while not _receipt_stop.wait(RECEIPT_RETRY_INTERVAL):
        _receipt_tick()


def _ensure_receipt_thread():
    global _receipt_thread
    with _receipts_lock:
        if _receipt_stop.is_set():
            return  # retired module: never start a loop that pins it
        if _receipt_thread is not None and _receipt_thread.is_alive():
            return
        _receipt_thread = threading.Thread(
            target=_receipt_loop, name="queued-act-retry", daemon=True
        )
        _receipt_thread.start()


def stop_background(timeout=2.0):
    """Stop this module's background threads (review L3).

    Called by the packaged app before it replaces this module (self-heal)
    and on Stop. A retired module never restarts its retry loop.
    """
    _receipt_stop.set()
    thread = _receipt_thread
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout)


def _forward_act(op, payload):
    """Forward one act to the hands. Returns (status_code, json_dict).

    This is exactly the forwarding /v1/act has always done, factored out
    so /v1/act/queued (and its retry pump) share the one code path.
    """
    if not HANDS_SECRET:
        return 503, {"ok": False, "error": "HANDS_SECRET not configured on gateway"}
    # Review E6: a list/dict op used to raise TypeError (unhashable) -> 500.
    if not isinstance(op, str) or op not in HANDS_OPS:
        return 400, {"ok": False, "error": f"unknown op: {_short(op)!r}"}
    path = HANDS_OP_PATHS.get(op, op)
    try:
        r = requests.post(
            f"{HANDS_URL}/{path}",
            json=payload,
            headers=_hands_headers(),
            timeout=_act_timeout(op, payload),
        )
    except requests.RequestException as exc:
        return 502, {"ok": False, "error": f"hands unreachable: {exc}"}
    if r.status_code == 401:
        return 502, {
            "ok": False,
            "error": "hands rejected the gateway's secret — HANDS_SECRET does not match the hands app's current session secret",
        }
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"ok": r.ok, "status": r.status_code}
def _act_timeout(op, payload):
    """Per-op timeout for the hands call (wishlist #9).

    A long "type" on the keystroke path runs at worst ~6 chars/s, so a
    flat 30s would kill legitimate long types at the gateway while the
    hands kept typing. Size the timeout to the payload instead:
    15s base + 1s per 6 chars, clamped to [HANDS_TIMEOUT, 600].
    Every other op keeps the flat HANDS_TIMEOUT.
    """
    if op == "type":
        text = (payload or {}).get("text") or ""
        try:
            n = len(text)
        except TypeError:
            n = 0
        return max(HANDS_TIMEOUT, min(600, 15 + n / 6))
    return HANDS_TIMEOUT


def _proxy_hands_get(path):
    """GET a JSON route on the hands, with _fetch_screenshot's errors:
    503 when no hands secret is configured, 502 when the hands is
    unreachable or rejects the gateway's secret."""
    if not HANDS_SECRET:
        return jsonify({"ok": False, "error": "HANDS_SECRET not configured on gateway"}), 503
    try:
        r = requests.get(
            f"{HANDS_URL}{path}", headers=_hands_headers(), timeout=HANDS_TIMEOUT
        )
    except requests.RequestException as exc:
        return jsonify({"ok": False, "error": f"hands unreachable: {exc}"}), 502
    if r.status_code == 401:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "hands rejected the gateway's secret — HANDS_SECRET does not match the hands app's current session secret",
                }
            ),
            502,
        )
    try:
        return jsonify(r.json()), r.status_code
    except ValueError:
        return jsonify({"ok": r.ok, "status": r.status_code}), r.status_code


@app.get("/v1/windows")
def windows():
    """Open windows on the hands host (wishlist #3), proxied."""
    return _proxy_hands_get("/windows")


@app.get("/v1/clipboard")
def clipboard():
    """Current clipboard text from the hands (wishlist #6), proxied.

    Clipboard reads are sensitive: this route sits behind the gateway
    token like every /v1 route, and the hands logs every read.
    """
    return _proxy_hands_get("/clipboard/get")


@app.post("/v1/act")
def act():
    """One desktop action, forwarded to the hands.

    Body: {"op": "move"|"click"|"type"|"key"|"scroll"|"hotkey"|"break"|
           "drag"|"focus"|"clipboard_set", ...op params}
    Op params match the hands API exactly: move/click {x, y}, type {text},
    key {key}, scroll {clicks}, hotkey {keys}, break {times},
    drag {x1, y1, x2, y2}, focus {title}, clipboard_set {text} (forwarded
    to the hands' /clipboard/set).
    """
    body = _json_body()
    op = body.get("op") or ""
    payload = {k: v for k, v in body.items() if k != "op"}
    status, result = _forward_act(op, payload)
    return jsonify(result), status


def _param_keys(payload, exclude=()):
    """Sorted payload key NAMES for a receipt, bounded in count and length
    (review E6: a receipt must not retain an arbitrarily large body)."""
    names = sorted(str(k)[:64] for k in payload.keys() if k not in exclude)
    return names[:PARAM_KEYS_MAX]


@app.post("/v1/act/queued")
def act_queued():
    """Like /v1/act, but a yield-409 is held and retried (wishlist #18).

    Same body as /v1/act plus optional hold_seconds (default 30, clamp
    1..120). A 409 whose error says the act yielded (the owner is
    actively using the desktop) files a receipt, starts the retry
    engine, and answers 202; poll GET /v1/act/queued/<id> for the
    outcome (applied / refused-yielded / failed). Any non-yield result
    is returned as /v1/act would return it, plus the receipt id: every
    submission files a receipt (R4, F4), so the outcome stays
    reconcilable via GET /v1/act/queued/<id> even after a lost
    response — a driver never has to guess whether an act landed.
    """
    body = _json_body()
    op = body.get("op") or ""
    hold = _clamp_number(body.get("hold_seconds", HOLD_DEFAULT), HOLD_DEFAULT, HOLD_MIN, HOLD_MAX)
    payload = {k: v for k, v in body.items() if k not in ("op", "hold_seconds")}
    status, result = _forward_act(op, payload)
    if not _is_yield_conflict(status, result):
        # R4 (F4): settle a receipt for the direct result too — no
        # payload is retained (nothing is held), and the id rides in
        # the response body so the caller can reconcile later.
        now = time.time()
        applied = isinstance(status, int) and 200 <= status < 300
        receipt = {
            "id": uuid.uuid4().hex,
            "op": _short(op),
            "param_keys": _param_keys(payload),
            "state": "applied" if applied else "failed",
            # R9 (#21): which path settled it — "direct" here,
            # "after-hold" when the retry pump applies a held act.
            "applied_via": "direct" if applied else None,
            "submitted_at": now,
            "updated_at": now,
            "deadline": now,
            "hold_seconds": hold,
            "hands_status": status,
            "result": result,
        }
        _store_receipt(receipt)
        body = (
            dict(result)
            if isinstance(result, dict)
            else {"ok": False, "error": str(result)}
        )
        body["id"] = receipt["id"]
        return jsonify(body), status
    now = time.time()
    # R9 (#20): pin the held act to the window it was aimed at. The
    # hands reports the foreground hwnd in its yield 409; retries then
    # carry expect_focus, so the hands' focus guard refuses (non-yield
    # 409 -> receipt failed) if the owner left a different window in
    # front. A caller's own expect_focus wins; focus itself is exempt
    # (its job is to change the foreground).
    pinned = None
    if (
        "expect_focus" not in payload
        and op != "focus"
        and isinstance(result, dict)
        and isinstance(result.get("foreground_hwnd"), int)
        and not isinstance(result.get("foreground_hwnd"), bool)
    ):
        pinned = result["foreground_hwnd"]
        payload["expect_focus"] = {"hwnd": pinned}
    receipt = {
        "id": uuid.uuid4().hex,
        "op": op,
        "param_keys": _param_keys(payload, exclude=("expect_focus",) if pinned is not None else ()),
        "pinned_foreground": pinned is not None,
        "state": "held",
        "submitted_at": now,
        "updated_at": now,
        "deadline": now + hold,
        "hold_seconds": hold,
        "hands_status": status,
        "result": None,
    }
    _store_receipt(receipt, payload)
    _ensure_receipt_thread()
    return (
        jsonify({"ok": True, "queued": True, "id": receipt["id"], "state": "held"}),
        202,
    )


@app.get("/v1/act/queued")
def act_queued_list():
    with _receipts_lock:
        receipts = [
            {
                "id": r["id"],
                "op": r["op"],
                "state": r["state"],
                "submitted_at": r["submitted_at"],
            }
            for r in reversed(list(_receipts.values()))
        ]
    return jsonify({"ok": True, "receipts": receipts})


@app.get("/v1/act/queued/<receipt_id>")
def act_queued_get(receipt_id):
    with _receipts_lock:
        receipt = _receipts.get(receipt_id)
        if receipt is None:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "unknown or expired receipt id",
                        "state": "expired",
                    }
                ),
                404,
            )
        body = _public_receipt(receipt)
    body["ok"] = True
    return jsonify(body)


# ---- file-drop (wishlist #2) -------------------------------------------------
# Gateway one-shot -> hands begin/chunk/commit. Raw chunks are 256KB
# (hands chunk_max is 512KB). File contents are never logged.

_FILEDROP_RAW_CHUNK = 256 * 1024


def _hands_post(path, payload):
    """POST JSON to the hands; returns (status_code, body_dict)."""
    try:
        r = requests.post(
            f"{HANDS_URL}{path}", json=payload, headers=_hands_headers(), timeout=HANDS_TIMEOUT
        )
    except requests.RequestException as exc:
        return 502, {"ok": False, "error": f"hands unreachable: {exc}"}
    try:
        body = r.json()
    except ValueError:
        body = {"ok": False, "error": f"hands returned {r.status_code}"}
    if not isinstance(body, dict):
        body = {"ok": False, "error": "hands returned a non-object response"}
    if r.status_code == 401:
        # Same mapping as the other hands forwarders: a hands secret mismatch
        # must not look like a bad gateway token.
        return (
            502,
            {
                "ok": False,
                "error": "hands rejected the gateway's secret — HANDS_SECRET does not match the hands app's current session secret",
            },
        )
    return r.status_code, body


def _filedrop_via_hands(folder, filename, data, sha256_hex, post=None):
    """Run begin/chunk/commit against the hands.

    `post` is a callable seam taking (path, payload) -> (status, dict);
    tests inject a fake hands through it (or by replacing _hands_post).
    Returns (body_dict, status_code); any hands error is surfaced as-is.
    """
    if post is None:
        post = _hands_post
    status, body = post(
        "/filedrop/begin",
        {"folder": folder, "filename": filename, "size": len(data), "sha256": sha256_hex},
    )
    if status != 200 or not body.get("ok"):
        return body, status
    upload_id = body.get("upload_id")
    if not upload_id:
        return {"ok": False, "error": "hands begin returned no upload_id"}, 502
    try:
        chunk_max = int(body.get("chunk_max") or _FILEDROP_RAW_CHUNK)
    except (TypeError, ValueError):
        chunk_max = _FILEDROP_RAW_CHUNK
    chunk_size = min(_FILEDROP_RAW_CHUNK, chunk_max) if chunk_max > 0 else _FILEDROP_RAW_CHUNK
    offset = 0
    while offset < len(data):
        chunk = data[offset : offset + chunk_size]
        status, body = post(
            "/filedrop/chunk",
            {
                "upload_id": upload_id,
                "offset": offset,
                "data_b64": base64.b64encode(chunk).decode("ascii"),
            },
        )
        if status != 200 or not body.get("ok"):
            try:
                post("/filedrop/abort", {"upload_id": upload_id})
            except Exception:
                pass
            return body, status
        offset += len(chunk)
    status, body = post("/filedrop/commit", {"upload_id": upload_id})
    if status != 200 or not isinstance(body, dict) or not body.get("ok"):
        try:
            post("/filedrop/abort", {"upload_id": upload_id})
        except Exception:
            pass
    return body, status


@app.post("/v1/filedrop")
def filedrop():
    """One-shot file drop: {"folder", "filename", "data_b64", "sha256"?}.

    The gateway computes sha256 itself when not supplied, and verifies
    it against a supplied one before sending anything to the hands.
    """
    if not HANDS_SECRET:
        return jsonify({"ok": False, "error": "HANDS_SECRET not configured on gateway"}), 503
    body = _json_body()
    folder = body.get("folder")
    filename = body.get("filename")
    data_b64 = body.get("data_b64")
    if not isinstance(folder, str) or not folder:
        return jsonify({"ok": False, "error": "folder required"}), 400
    if not isinstance(filename, str) or not filename:
        return jsonify({"ok": False, "error": "filename required"}), 400
    if not isinstance(data_b64, str) or not data_b64:
        return jsonify({"ok": False, "error": "data_b64 required"}), 400
    try:
        data = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError):
        return jsonify({"ok": False, "error": "data_b64 is not valid base64"}), 400
    digest = hashlib.sha256(data).hexdigest()
    supplied = body.get("sha256")
    if supplied is not None:
        if not isinstance(supplied, str) or supplied.lower() != digest:
            return jsonify({"ok": False, "error": "sha256 mismatch"}), 400
    # Hands filedrop_begin rejects size > 16MB. Enforce the same here so a
    # raised MAX_CONTENT_LENGTH cannot sneak a larger object through.
    if len(data) > 16 * 1024 * 1024:
        return jsonify({"ok": False, "error": "file exceeds 16MB"}), 400
    result, status = _filedrop_via_hands(folder, filename, data, digest)
    return jsonify(result), status


# ---- wait-for (wishlist #5) ---------------------------------------------------

class _WaitFetchError(Exception):
    """Carries a ready-made (response, status) error from a fetcher."""

    def __init__(self, response):
        super().__init__("wait fetch failed")
        self.response = response


class _WaitUnsupported(Exception):
    """The hands does not support the polled capability."""


def _sample_screen_hash(fetch=None, stable=False):
    if fetch is None:
        fetch = _fetch_screenshot
    png_a, _, err = fetch()
    if err is not None:
        raise _WaitFetchError(err)
    png_b = None
    if stable:
        time.sleep(STABLE_SAMPLE_GAP)
        png_b, _, err = fetch()
        if err is not None:
            raise _WaitFetchError(err)
    try:
        digest, _, _ = _frame_hash(png_a, png_b, DEFAULT_MASK_BOTTOM)
    except _UndecodableFrame:
        raise _WaitFetchError(_undecodable_response())  # review E7: JSON 502, not HTML 500
    return digest


def _sample_region_hash(fetch=None, region=None):
    if fetch is None:
        fetch = _fetch_screenshot
    png, _, err = fetch()
    if err is not None:
        raise _WaitFetchError(err)
    try:
        digest, _, _ = _region_hash(png, region)
    except _UndecodableFrame:
        raise _WaitFetchError(_undecodable_response())  # review E7
    return digest


def _check_screen_hash_change(state, fetch=None, stable=False):
    return _sample_screen_hash(fetch=fetch, stable=stable) != state["initial_hash"]


def _check_screen_region_change(state, fetch=None):
    return _sample_region_hash(fetch=fetch, region=state["region"]) != state["initial_hash"]


def _fetch_window_titles():
    """Hands GET /window_titles -> (titles_or_None, status_code)."""
    try:
        r = requests.get(
            f"{HANDS_URL}/window_titles", headers=_hands_headers(), timeout=5
        )
    except requests.RequestException:
        return None, 502
    if r.status_code != 200:
        return None, r.status_code
    try:
        data = r.json()
    except ValueError:
        return None, r.status_code
    titles = data.get("titles") if isinstance(data, dict) else None
    return (list(titles) if isinstance(titles, list) else []), 200


def _check_window_title(contains, fetch=None):
    if fetch is None:
        fetch = _fetch_window_titles
    titles, status = fetch()
    if status in (404, 501):
        raise _WaitUnsupported()
    if status != 200:
        # _WaitFetchError carries a ready (response, status) tuple.
        # wait_for already returns exc.response. 401 is rewritten to 502 so a
        # hands secret mismatch does not look like a bad gateway token.
        out = 502 if status == 401 else status
        raise _WaitFetchError(
            (
                jsonify(
                    {
                        "ok": False,
                        "error": f"window title fetch failed ({status})",
                        "hands_status": status,
                    }
                ),
                out,
            )
        )
    if not titles:
        return False
    needle = str(contains).lower()
    return any(needle in str(t).lower() for t in titles)


def _fetch_parser_health():
    try:
        r = requests.get(f"{PARSER_URL}/health", timeout=2)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _check_parser_model_loaded(fetch=None):
    if fetch is None:
        fetch = _fetch_parser_health
    data = fetch()
    return bool(data and data.get("model_loaded"))


def _clamp_number(value, default, lo, hi):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(num, hi))


def _check_process_exited(pid, state):
    """Pin the first observed process instance; a reused PID is not that process."""
    try:
        response = requests.get(
            f"{HANDS_URL}/process/{pid}", headers=_hands_headers(), timeout=5
        )
        if response.status_code != 200:
            status = 501 if response.status_code == 501 else 502
            raise _WaitFetchError(
                (jsonify({"ok": False, "error": "process probe unavailable",
                          "hands_status": response.status_code}), status)
            )
        body = response.json()
        if (not isinstance(body, dict) or body.get("ok") is not True
                or body.get("pid") != pid or type(body.get("running")) is not bool):
            raise ValueError("invalid process snapshot")
        if not body["running"]:
            return True
        instance = body.get("instance")
        if not isinstance(instance, str) or not instance:
            raise ValueError("missing process identity")
        original = state.setdefault("process_instance", instance)
        return instance != original
    except (requests.RequestException, ValueError):
        raise _WaitFetchError(
            (jsonify({"ok": False, "error": "process probe failed"}), 502)
        )


_WAIT_TYPES = {"screen_hash_change", "screen_region_change", "window_title",
               "parser_model_loaded", "process_exit"}


@app.post("/v1/wait")
def wait_for():
    """Poll a condition until it holds or the deadline passes.

    Body: {"condition": {...}, "timeout_seconds": 30, "poll_seconds": 1}
    """
    body = _json_body()
    condition = body.get("condition")
    if not isinstance(condition, dict):
        return jsonify({"ok": False, "error": "condition required"}), 400
    ctype = condition.get("type")
    if ctype not in _WAIT_TYPES:
        return jsonify({"ok": False, "error": f"unknown condition type: {ctype!r}"}), 400
    timeout = _clamp_number(body.get("timeout_seconds", 30), 30, 1, 300)
    poll = _clamp_number(body.get("poll_seconds", 1), 1, 0.2, 10)

    state = {}
    stable = False
    if ctype == "screen_hash_change":
        stable = bool(condition.get("stable"))
        try:
            state["initial_hash"] = _sample_screen_hash(stable=stable)
        except _WaitFetchError as exc:
            return exc.response
        except _HashUnavailable:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "image decoding unavailable on gateway (pillow not installed)",
                    }
                ),
                501,
            )
    elif ctype == "screen_region_change":
        region = condition.get("region")
        if (
            not isinstance(region, (list, tuple))
            or len(region) != 4
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in region)
            # Review E7: JSON allows NaN/Infinity, and int(inf) raised an
            # uncaught OverflowError (500) in the region hash.
            or any(not math.isfinite(v) for v in region)
        ):
            return jsonify({"ok": False, "error": "region must be [x, y, w, h]"}), 400
        state["region"] = list(region)
        try:
            state["initial_hash"] = _sample_region_hash(region=state["region"])
        except _WaitFetchError as exc:
            return exc.response
        except _HashUnavailable:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "image decoding unavailable on gateway (pillow not installed)",
                    }
                ),
                501,
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
    elif ctype == "window_title":
        if not isinstance(condition.get("contains"), str) or not condition.get("contains"):
            return jsonify({"ok": False, "error": "contains required"}), 400
    elif ctype == "process_exit":
        pid = condition.get("pid")
        if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
            return jsonify({"ok": False, "error": "pid must be a positive DWORD"}), 400

    start = time.monotonic()
    while True:
        try:
            if ctype == "screen_hash_change":
                held = _check_screen_hash_change(state, stable=stable)
            elif ctype == "screen_region_change":
                held = _check_screen_region_change(state)
            elif ctype == "window_title":
                held = _check_window_title(condition.get("contains"))
            elif ctype == "process_exit":
                held = _check_process_exited(condition["pid"], state)
            else:  # parser_model_loaded
                held = _check_parser_model_loaded()
        except _WaitFetchError as exc:
            return exc.response
        except _WaitUnsupported:
            return jsonify({"ok": False, "error": "hands does not support window titles"}), 501
        except _HashUnavailable:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "image decoding unavailable on gateway (pillow not installed)",
                    }
                ),
                501,
            )
        waited = time.monotonic() - start
        if held:
            return jsonify(
                {"ok": True, "held": True, "waited_seconds": waited, "condition": condition}
            )
        if waited >= timeout:
            return jsonify(
                {
                    "ok": True,
                    "held": False,
                    "timeout": True,
                    "waited_seconds": waited,
                    "condition": condition,
                }
            )
        time.sleep(min(poll, max(0.0, timeout - waited)))


if __name__ == "__main__":
    from waitress import serve

    # R4 (F9): 8 threads starved act traffic whenever long-poll
    # endpoints (wait_for up to 300 s, screen/wait 25 s, a long type
    # forward up to 600 s) overlapped — every route, /v1/info
    # included, queued behind them. 32 costs nothing at this scale.
    # Review I1: the bind address is configurable. The default stays
    # 0.0.0.0 (all interfaces) so an agent that reaches the gateway by the
    # PC's tailnet IP keeps working; set RELAY_GATEWAY_HOST=127.0.0.1 when
    # the gateway is only reached through Tailscale Serve or a local proxy.
    host = os.environ.get("RELAY_GATEWAY_HOST", "0.0.0.0").strip() or "0.0.0.0"
    print(f"HandsRelay gateway listening on {host}:5010")
    serve(app, host=host, port=5010, threads=32)
