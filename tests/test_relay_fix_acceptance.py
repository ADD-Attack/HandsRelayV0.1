"""Proposed regression contract for the pinned R12 review.

Run in its own process:
  RELAY_REPO_ROOT=/path/to/repo python -m pytest -q test_relay_fix_acceptance.py

These assert desired fixes, not the old buggy behavior. No xfail or skips.
They use simulated desktop/network boundaries; Windows acceptance is separate.
The framework defines new watcher fields and claim-sync behavior used here.
"""
import importlib.util
from io import BytesIO
import itertools
import os
import tempfile
from pathlib import Path
import random
import sys
import threading
import types
from urllib.parse import urlsplit

import pytest
from PIL import Image, ImageGrab

ROOT = Path(os.environ.get("RELAY_REPO_ROOT", Path.cwd())).resolve()
_names = itertools.count()
TOKEN = {"X-Relay-Token": "acceptance-token"}


class FakeModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        value = lambda *a, **kw: None
        setattr(self, name, value)
        return value


class Clock:
    def __init__(self, value=10000.0):
        self.value = value

    def time(self):
        return self.value

    def monotonic(self):
        return self.value

    def monotonic_ns(self):
        return int(self.value * 1_000_000_000)

    def sleep(self, seconds):
        self.value += seconds


def load(path):
    spec = importlib.util.spec_from_file_location(
        f"acceptance_{next(_names)}", ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def gateway(monkeypatch, clock):
    module = load("services/gateway/app.py")
    module.TOKEN = "acceptance-token"
    module.HANDS_SECRET = "acceptance-secret"
    monkeypatch.setattr(module, "time", clock)
    monkeypatch.setattr(module, "_ensure_receipt_thread", lambda: None)
    # R13 protocol adapter: P02/P03 inject faults at the action POST,
    # after successful lease negotiation (covered separately in B05).
    monkeypatch.setattr(module, "_prepare_queued_execution",
                        lambda *a: {"version": 1, "boot": "fixture", "token": "fixture"})
    module._reset_coordination_state()
    return module


@pytest.fixture
def hands(monkeypatch, clock):
    for name in ("pyautogui", "tkinter", "tkinter.messagebox"):
        monkeypatch.setitem(sys.modules, name, FakeModule(name))
    module = load("hands/hands_relay_hands.py")
    module.SHARED_SECRET = "acceptance-secret"
    monkeypatch.setattr(module, "time", clock)
    monkeypatch.setattr(module, "_probe_lock_state", lambda: (False, 30.0))
    monkeypatch.setattr(module, "_tick_ms", lambda: int(clock.value * 1000))
    monkeypatch.setattr(module, "_probe_locked", lambda: False)
    monkeypatch.setattr(module._owner_monitor, "ensure_started", lambda *a, **kw: True)
    monkeypatch.setattr(module, "_owner_input_snapshot", lambda: (0, 100))
    monkeypatch.setattr(module, "start_resting_hand", lambda: None)
    module._INPUT = module.motion.InputMutex(clock=clock.monotonic, sleep=clock.sleep)
    module._resting = module._RestingHand(clock=clock.monotonic)
    return module


def held(gateway, deadline, op="click", **payload):
    receipt = dict(id="acceptance-held", op=op, state="held",
                   deadline=deadline, submitted_at=0, updated_at=0)
    gateway._store_receipt(receipt, payload or {"x": 1, "y": 2})
    return receipt


def record(gateway, response):
    return gateway._receipts[response.get_json()["id"]]


def test_p01_expired_hold_does_not_dispatch(gateway, monkeypatch, clock):
    for boundary in (clock.value, clock.value - 1):
        receipt = held(gateway, boundary)
        calls = []
        monkeypatch.setattr(gateway, "_forward_act",
                            lambda *a: (calls.append(a) or (200, {"ok": True})))
        gateway._pump_receipt(receipt, clock.value)
        assert not calls, "A receipt at or beyond its start deadline was forwarded"
        assert receipt["state"] == "refused-yielded"
        assert receipt["id"] not in gateway._pending_payloads


def test_p02_response_loss_is_unknown_and_never_retried(gateway, monkeypatch):
    calls = []

    def lost(*a, **kw):
        calls.append(kw["json"])
        raise gateway.requests.ReadTimeout("lost response after dispatch")

    monkeypatch.setattr(gateway.requests, "post", lost)
    response = gateway.app.test_client().post(
        "/v1/act/queued", headers=TOKEN, json={"op": "click", "x": 1, "y": 2})
    receipt = record(gateway, response)
    assert response.status_code == 502
    assert receipt["state"] == "unknown-outcome"
    gateway._pump_receipt(receipt, 1e12)
    assert len(calls) == 1
    assert receipt["id"] not in gateway._pending_payloads


@pytest.mark.parametrize("shape", ["empty", "list", "null", "string", "missing_ok"])
def test_p03_empty_success_body_cannot_become_applied(gateway, monkeypatch, shape):
    class Empty200:
        status_code, ok = 200, True

        def json(self):
            if shape == "empty":
                raise ValueError("empty body")
            return {"list": [], "null": None, "string": "ok", "missing_ok": {}}[shape]

    monkeypatch.setattr(gateway.requests, "post", lambda *a, **kw: Empty200())
    response = gateway.app.test_client().post(
        "/v1/act/queued", headers=TOKEN, json={"op": "click", "x": 1, "y": 2})
    receipt = record(gateway, response)
    assert response.status_code == 502
    assert response.get_json()["ok"] is False
    assert receipt["state"] == "unknown-outcome"


def test_p04_held_abort_retains_safe_partial_evidence(gateway, monkeypatch, clock):
    secret = "PRIVATE-PAYLOAD-DO-NOT-PERSIST"
    receipt = held(gateway, clock.value + 10, op="type", text=secret)
    result = {"ok": False, "error_code": "type_aborted", "abort_reason": "owner",
              "aborted": True, "chars_typed": 12, "chars": 40,
              "owner_guard": "hook", "method_used": "keys",
              "text": secret, "error": "echoed " + secret,
              "debug": {"payload": secret}}
    monkeypatch.setattr(gateway, "_forward_act", lambda *a: (409, result))
    gateway._pump_receipt(receipt, clock.value)
    assert receipt["state"] in {"failed", "aborted-owner"}  # F4 then F3
    assert isinstance(receipt.get("result"), dict), "Held failure discarded structured diagnostics"
    assert receipt["result"]["chars_typed"] == 12
    assert receipt["result"]["abort_reason"] == "owner"
    assert receipt["result"]["owner_guard"] == "hook"
    assert receipt["id"] not in gateway._pending_payloads
    assert secret not in gateway.app.json.dumps(gateway._public_receipt(receipt))
    result["chars_typed"] = 39
    assert receipt["result"]["chars_typed"] == 12, "Retained diagnostics alias a mutable response"


def test_p05_http_200_with_ok_false_is_not_applied(gateway, monkeypatch):
    monkeypatch.setattr(gateway, "_forward_act",
                        lambda *a: (200, {"ok": False, "error_code": "operation_failed"}))
    response = gateway.app.test_client().post(
        "/v1/act/queued", headers=TOKEN, json={"op": "click", "x": 1, "y": 2})
    receipt = record(gateway, response)
    assert receipt["state"] == "failed"
    assert receipt.get("applied_via") is None
    assert response.get_json()["ok"] is False


def test_p06_locked_hold_cannot_wait_without_a_target(gateway, monkeypatch):
    monkeypatch.setattr(gateway, "_forward_act",
                        lambda *a: (409, {"ok": False, "error_code": "locked"}))
    response = gateway.app.test_client().post(
        "/v1/act/queued", headers=TOKEN, json={"op": "click", "x": 1, "y": 2})
    receipt = record(gateway, response)
    assert receipt["state"] == "failed"
    assert receipt.get("result", {}).get("error_code") == "target_unknown"
    assert receipt["id"] not in gateway._pending_payloads


@pytest.mark.parametrize("change", ["owner", "foreground", "both"])
def test_p07_guards_are_rechecked_after_input_acquisition(hands, monkeypatch, change):
    current = {"hwnd": 1, "idle": 30.0}
    calls = []
    monkeypatch.setattr(hands, "_probe_lock_state", lambda: (False, current["idle"]))
    monkeypatch.setattr(hands, "_foreground_window", lambda: {"_hwnd": current["hwnd"]})
    original_acquire = hands._INPUT.acquire

    def acquired(kind, rng):
        result = original_acquire(kind, rng)
        if change in {"owner", "both"}:
            current["idle"] = 0.0
        if change in {"foreground", "both"}:
            current["hwnd"] = 2
        return result

    monkeypatch.setattr(hands._INPUT, "acquire", acquired)
    monkeypatch.setattr(hands.pyautogui, "moveTo", lambda *a, **kw: calls.append("move"))
    monkeypatch.setattr(hands.pyautogui, "click", lambda *a, **kw: calls.append("click"))
    response = hands.app.test_client().post(
        "/click", headers={"X-Secret": hands.SHARED_SECRET},
        json={"x": 1, "y": 2, "human": False, "expect_focus": {"hwnd": 1}})
    assert response.status_code == 409
    assert not calls
    # A same-thread probe would reacquire this RLock even if it leaked.
    acquired_elsewhere = []
    def check_release():
        acquired = hands._INPUT.try_acquire()
        acquired_elsewhere.append(acquired)
        if acquired:
            hands._INPUT.release("mouse")
    worker = threading.Thread(target=check_release, daemon=True)
    worker.start()
    worker.join(timeout=2)
    assert acquired_elsewhere == [True], "Refusal leaked the input mutex"


def test_p08_failed_fetch_preserves_last_success_but_marks_error(gateway, monkeypatch, clock):
    image = BytesIO()
    Image.new("RGB", (100, 100), "red").save(image, format="PNG")
    monkeypatch.setattr(gateway, "_fetch_screenshot",
                        lambda **kw: (image.getvalue(), "image/png", None))
    client = gateway.app.test_client()
    assert client.get("/v1/screen?watcher=acceptance", headers=TOKEN).status_code == 200
    first = gateway._current_watchers()[0]
    assert first.get("last_success_at") is not None
    previous = first["last_success_at"]
    clock.value += 1

    def failed(**kw):
        return None, None, (gateway.jsonify({"ok": False}), 502)

    monkeypatch.setattr(gateway, "_fetch_screenshot", failed)
    assert client.get("/v1/screen?watcher=acceptance", headers=TOKEN).status_code == 502
    current = gateway._current_watchers()[0]
    assert current["last_seen"] > first["last_seen"]  # contact can remain fresh
    assert current["last_success_at"] == previous
    assert current["last_fetch_ok"] is False


def test_p09_gateway_restart_release_reaches_surviving_hands(gateway, hands, monkeypatch, clock):
    class Response:
        def __init__(self, response):
            self.status_code = response.status_code
            self.ok = 200 <= self.status_code < 300
            self.content = response.data
            self._json = response.get_json()

        def json(self):
            return self._json

    def local_post(url, json=None, **kw):
        return Response(hands.app.test_client().post(
            urlsplit(url).path, json=json, headers={"X-Secret": hands.SHARED_SECRET}))

    def local_get(url, **kw):
        return Response(hands.app.test_client().get(
            urlsplit(url).path, headers={"X-Secret": hands.SHARED_SECRET}))

    class ImmediateThread:
        def __init__(self, target, args=(), kwargs=None, **ignored):
            self.target, self.args, self.kwargs = target, args, kwargs or {}

        def start(self):
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(gateway.requests, "post", local_post)
    monkeypatch.setattr(gateway.requests, "get", local_get)
    monkeypatch.setattr(gateway._claim_push_session, "post", local_post)
    monkeypatch.setattr(gateway._claim_push_session, "get", local_get)
    monkeypatch.setattr(gateway.threading, "Thread", ImmediateThread)
    authority_dir = tempfile.mkdtemp()
    hands._claim_sync = hands.claim_sync.ClaimSync(os.path.join(authority_dir, "hands"))
    gateway.bind_claim_authority(authority_dir)
    gateway._claim_push_seq = 6
    gateway._claim = {"holder": "old", "purpose": "test", "expires_at": clock.value + 60}
    gateway._push_claim_locked(gateway._claim, clock.value)
    assert hands._resting.claim_held(hands._resting._clock())
    gateway._claim_authority.release()
    restarted = load("services/gateway/app.py")
    restarted.HANDS_SECRET = hands.SHARED_SECRET
    restarted.time = clock
    monkeypatch.setattr(restarted._claim_push_session, "post", local_post)
    monkeypatch.setattr(restarted._claim_push_session, "get", local_get)
    restarted.bind_claim_authority(authority_dir)
    restarted._claim = None
    restarted._push_claim_locked(None, clock.value)
    assert not hands._resting.claim_held(hands._resting._clock())


def test_p10_countdown_refresh_still_decreases(monkeypatch, hands):
    module = load("hands_relay_v2_app.py")
    claim = {"holder": "test", "purpose": "countdown", "expires_at": 1000}
    before = module.build_dashboard_model(None, claim, [], None, {}, 100)["claim"]
    after = module.build_dashboard_model(None, claim, [], None, {}, 105)["claim"]
    assert before["expires_in_s"] == 900
    assert after["expires_in_s"] == 895
    assert module.POLL_MS <= 5000


@pytest.mark.parametrize("owner_event", [True, False])
def test_p11_owner_keyboard_stops_remaining_rest_glide(hands, monkeypatch, clock, owner_event):
    position, moves, counts = [500, 500], [], {"owner": 0, "injected": 100}
    monkeypatch.setattr(hands, "_owner_input_snapshot",
                        lambda: (counts["owner"], counts["injected"]))
    monkeypatch.setattr(hands.pyautogui, "position", lambda: tuple(position))

    def move(x, y, **kw):
        moves.append((x, y))
        position[:] = [x, y]
        counts["injected"] += 1

    def sleep(seconds):
        clock.value += seconds
        if owner_event:
            counts["owner"] += 1  # a real keyboard event, no pointer movement

    monkeypatch.setattr(hands.pyautogui, "moveTo", move)
    monkeypatch.setattr(clock, "sleep", sleep)
    rest = hands._RestingHand(rng=random.Random(3), clock=clock.monotonic)
    rest.claim_until = clock.value + 60
    landed = rest._move((503, 500))
    if owner_event:
        assert landed is None
        assert len(moves) == 1
    else:
        assert landed == (503, 500)
        assert len(moves) > 1, "An always-disabled resting hand cannot pass"


@pytest.mark.parametrize("owner_event", [True, False])
def test_p12_nonjitter_keys_cannot_ignore_owner_takeover(hands, monkeypatch, clock, owner_event):
    counts, emitted = {"owner": 0, "injected": 100}, []
    monkeypatch.setattr(hands, "_owner_input_snapshot",
                        lambda: (counts["owner"], counts["injected"]))

    def write(text, **kw):
        emitted.extend(text)
        counts["injected"] += 1
        if owner_event:
            counts["owner"] += 1

    monkeypatch.setattr(hands.pyautogui, "write", write)
    response = hands.app.test_client().post(
        "/type", headers={"X-Secret": hands.SHARED_SECRET},
        json={"text": "x" * 130, "method": "keys", "jitter": False})
    body = response.get_json()
    if not owner_event:
        assert response.status_code == 200
        assert len(emitted) == 130
        assert body["ok"] is True
        return
    assert response.status_code == 409
    assert len(emitted) == 1, "Owner event during the first unit must stop the next unit"
    assert body["owner_guard"] in {"hook", "dwtime"}
    assert body["chars_typed"] == len(emitted)


def test_p13_secondary_capture_uses_virtual_origin(hands, monkeypatch):
    virtual = Image.new("RGB", (300, 100), "red")
    virtual.paste("green", (0, 0, 100, 100))
    virtual.paste("blue", (200, 0, 300, 100))
    primary = virtual.crop((100, 0, 200, 100))
    rects = [{"x": -100, "y": 0, "w": 100, "h": 100, "primary": False},
             {"x": 0, "y": 0, "w": 100, "h": 100, "primary": True},
             {"x": 100, "y": 0, "w": 100, "h": 100, "primary": False}]
    monkeypatch.setattr(hands, "_monitor_rects", lambda: rects)

    def screenshot(region=None, allScreens=False, **kw):
        im = virtual.copy() if allScreens else primary.copy()
        if region is not None:
            x, y, w, h = region
            im = im.crop((x, y, x + w, y + h))  # upstream-equivalent crop
        return im

    monkeypatch.setattr(hands.pyautogui, "screenshot", screenshot)
    monkeypatch.setattr(ImageGrab, "grab",
                        lambda bbox=None, all_screens=False, **kw:
                        virtual.copy() if all_screens else primary.copy())
    assert hands._grab(rects[0]).getpixel((50, 50)) == (0, 128, 0)
    assert hands._grab(rects[2]).getpixel((50, 50)) == (0, 0, 255)
