"""R11 — resting hand (replaces the jiggler), watcher mode + timeout,
GUI layout. Planner tests are pure; the hands/gateway/app pieces run with
pyautogui, the probe and the clock stubbed.
"""
import importlib.util
import math
import os
import random
import re
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("RELAY_GATEWAY_TOKEN", "r11-token")
os.environ.setdefault("HANDS_SECRET", "r11-secret")
TOKEN = {"X-Relay-Token": os.environ["RELAY_GATEWAY_TOKEN"]}


def _load(rel, tag):
    spec = importlib.util.spec_from_file_location(tag, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def motion():
    return _load("hands/motion.py", "r11_motion")


@pytest.fixture(scope="module")
def hands():
    return _load("hands/hands_relay_hands.py", "r11_hands")


@pytest.fixture(scope="module")
def gw():
    return _load("services/gateway/app.py", "r11_gateway")


@pytest.fixture(scope="module")
def appmod():
    return _load("hands_relay_v2_app.py", "r11_app")


# ============================================================ ramp profile

def test_ramp_zero_at_act_end_full_at_1_5s(motion):
    assert motion.REST_RAMP_S == 1.5
    assert motion.rest_intensity(0.0) == 0.0
    assert motion.rest_intensity(-1.0) == 0.0
    assert motion.rest_intensity(0.75) == pytest.approx(0.5)
    assert motion.rest_intensity(1.5) == 1.0
    assert motion.rest_intensity(600.0) == 1.0  # holds at full; no decay, no timeout
    vals = [motion.rest_intensity(t / 100) for t in range(0, 151)]
    assert vals == sorted(vals)  # monotonic ramp, no frozen gap
    assert vals[1] > 0.0  # starts immediately


def test_float_is_local_and_small(motion):
    rng = random.Random(4)
    off = (0.0, 0.0)
    steps, far = [], 0.0
    for _ in range(5000):
        new, wait = motion.plan_rest(off, 1.0, rng)
        steps.append(math.hypot(new[0] - off[0], new[1] - off[1]))
        off = new
        far = max(far, math.hypot(*off))
        assert 0.25 <= wait <= 0.9
    assert far <= motion.REST_RADIUS_PX + 1e-9  # never drifts away from rest
    assert max(steps) <= 2 * motion.REST_RADIUS_PX  # no 10-40 px sweeps
    assert len({round(s, 3) for s in steps}) > 1000  # organic, never a fixed pattern


def test_float_scales_with_intensity(motion):
    def amp(k):
        rng = random.Random(9)
        return sum(math.hypot(*motion.plan_rest((0.0, 0.0), k, rng)[0]) for _ in range(500)) / 500

    assert amp(0.0) == 0.0
    assert amp(0.1) < amp(1.0) / 5
    assert 1.0 <= amp(1.0) <= 3.0


# ============================================================ resting hand

class _Pag:
    def __init__(self, tick, pos=(500, 500)):
        self.tick = tick
        self.pos = pos
        self.moves = []

    def position(self):
        return self.pos

    def moveTo(self, x, y, duration=None):
        self.moves.append((x, y))
        self.pos = (int(round(x)), int(round(y)))
        self.tick["last_input"] = self.tick["t"]  # the OS counts injected input


@pytest.fixture
def rig(hands, monkeypatch):
    # OS idle clock: idle = now - last input (ours or the owner's).
    tick = {"t": 1_000_000, "last_input": 1_000_000 - 2_000}
    pag = _Pag(tick)
    monkeypatch.setattr(hands.pyautogui, "position", pag.position)
    monkeypatch.setattr(hands.pyautogui, "moveTo", pag.moveTo)
    monkeypatch.setattr(hands, "_probe_lock_state",
                        lambda: (False, (tick["t"] - tick["last_input"]) / 1000.0))
    monkeypatch.setattr(hands, "_tick_ms", lambda: tick["t"])
    monkeypatch.setattr(hands, "_LEDGER", hands.motion.SelfInputLedger())
    monkeypatch.setattr(hands, "_INPUT", hands.motion.InputMutex(sleep=lambda s: None))
    return pag, tick


def _rest(hands, now=100.0):
    clock = {"t": now}
    r = hands._RestingHand(rng=random.Random(3), clock=lambda: clock["t"])
    return r, clock


def _run(r, clock, tick, until, dt=0.1):
    while clock["t"] < until:
        r.step(clock["t"])
        clock["t"] += dt
        tick["t"] += int(dt * 1000)


def test_resting_hand_only_while_claim_held(hands, rig):
    pag, tick = rig
    r, clock = _rest(hands)
    _run(r, clock, tick, 110.0)
    assert pag.moves == [] and not r.status()["active"] and not r.status()["claim_held"]
    assert r.set_claim(True, 900, 1, clock["t"])
    _run(r, clock, tick, 120.0)
    assert pag.moves and r.status()["active"] and r.status()["claim_held"]
    # every float stays local to where the pointer rested
    radius = hands.motion.REST_RADIUS_PX
    assert all(abs(x - 500) <= radius and abs(y - 500) <= radius for x, y in pag.moves)
    assert r.set_claim(False, 0, 2, clock["t"])
    n = len(pag.moves)
    _run(r, clock, tick, 130.0)
    assert len(pag.moves) == n and not r.status()["active"]


def test_resting_hand_stops_when_lease_runs_out(hands, rig):
    pag, tick = rig
    r, clock = _rest(hands)
    r.set_claim(True, 5, 1, clock["t"])  # lease has 5 s left
    _run(r, clock, tick, 104.0)
    assert r.status()["claim_held"]
    _run(r, clock, tick, 105.05)  # the lease ran out at 105.0
    n = len(pag.moves)
    _run(r, clock, tick, 115.0)
    assert not r.status()["claim_held"] and len(pag.moves) == n


def test_stale_claim_push_ignored(hands, rig):
    r, clock = _rest(hands)
    r.set_claim(False, 0, 5, clock["t"])
    assert not r.set_claim(True, 900, 4, clock["t"])  # older push arrived late
    assert not r.claim_held(clock["t"])


def test_ramp_resets_on_every_act(hands, rig):
    pag, tick = rig
    r, clock = _rest(hands)
    r.set_claim(True, 900, 1, clock["t"])
    _run(r, clock, tick, 105.0)
    assert r.intensity(clock["t"]) == 1.0
    r.act_begin()
    assert r.intensity(clock["t"]) == 0.0 and not r.status()["active"]
    r.act_end(clock["t"])
    assert r.intensity(clock["t"]) == 0.0  # intensity 0 at act end
    assert r.intensity(clock["t"] + 0.75) == pytest.approx(0.5)
    assert r.intensity(clock["t"] + 1.5) == 1.0  # full at 1.5 s
    assert r.intensity(clock["t"] + 800) == 1.0  # no resting-hand timeout (lease is 900 s)


def test_real_act_resets_the_global_ramp(hands, rig, monkeypatch):
    pag, tick = rig
    clock = {"t": 50.0}
    r = hands._RestingHand(rng=random.Random(1), clock=lambda: clock["t"])
    monkeypatch.setattr(hands, "_resting", r)
    monkeypatch.setattr(hands.time, "monotonic", lambda: clock["t"])
    r.set_claim(True, 900, 1, 0.0)
    assert r.intensity(50.0) == 1.0
    with hands._input("mouse"):
        hands._inject(hands.pyautogui.moveTo, 1, 2)
        assert r.intensity(50.0) == 0.0
    assert r.intensity(50.0) == 0.0 and r.last_act_end == 50.0


def test_owner_input_ends_episode_until_next_act(hands, rig, monkeypatch):
    pag, tick = rig
    r, clock = _rest(hands)
    r.set_claim(True, 900, 1, clock["t"])
    _run(r, clock, tick, 103.0)
    assert r.status()["active"]
    pag.pos = (pag.pos[0] + 60, pag.pos[1])  # owner grabs the mouse
    _run(r, clock, tick, 104.0)
    n = len(pag.moves)
    assert not r.status()["active"] and r.status()["owner_kills"] == 1
    _run(r, clock, tick, 120.0)
    assert len(pag.moves) == n  # stays still: the owner has the hand
    r.act_end(clock["t"])
    _run(r, clock, tick, 125.0)
    assert len(pag.moves) > n and r.status()["active"]


def test_owner_keyboard_input_ends_episode(hands, rig, monkeypatch):
    pag, tick = rig
    r, clock = _rest(hands)
    r.set_claim(True, 900, 1, clock["t"])
    _run(r, clock, tick, 103.0)
    assert r.status()["active"]
    # the owner presses a key: fresh OS input, outside any span of ours
    tick["t"] += 2_000
    clock["t"] += 2.0
    tick["last_input"] = tick["t"] - 50
    r.step(clock["t"])
    assert not r.status()["active"] and r.status()["owner_kills"] == 1


def test_resting_hand_still_when_locked_or_blind(hands, rig, monkeypatch):
    pag, tick = rig
    for probe in ((True, 10.0), (None, None), (False, None)):
        monkeypatch.setattr(hands, "_probe_lock_state", lambda p=probe: p)
        r, clock = _rest(hands)
        r.set_claim(True, 900, 1, clock["t"])
        _run(r, clock, tick, 105.0)
    assert pag.moves == []


def test_resting_hand_never_contends_with_an_op(hands, rig):
    pag, tick = rig
    r, clock = _rest(hands)
    r.set_claim(True, 900, 1, 0.0)
    hands._INPUT.acquire("keys", random.Random(1))
    try:
        t = threading.Thread(target=lambda: _run(r, clock, tick, 110.0))
        t.start()
        t.join()
    finally:
        hands._INPUT.release("keys")
    assert pag.moves == []


def test_resting_moves_are_on_the_self_ledger(hands, rig):
    pag, tick = rig
    r, clock = _rest(hands)
    r.set_claim(True, 900, 1, clock["t"])
    _run(r, clock, tick, 105.0)
    assert pag.moves and hands._LEDGER._spans


def test_resting_claim_route_validates(hands, rig, monkeypatch):
    r, clock = _rest(hands)
    monkeypatch.setattr(hands, "_resting", r)
    monkeypatch.setattr(hands, "start_resting_hand", lambda: None)
    with hands.app.test_request_context("/resting/claim", method="POST",
                                        json={"held": True, "expires_in": 60, "seq": 1}):
        body = hands.resting_claim()
    assert body["ok"] and body["applied"]
    with hands.app.test_request_context("/resting/claim", method="POST", json={"held": "yes"}):
        _, status = hands.resting_claim()
    assert status == 400


def test_no_jiggler_left_in_hands(hands):
    src = (ROOT / "hands/hands_relay_hands.py").read_text(encoding="utf-8")
    assert "_Jiggler" not in src and "jiggler_enabled" not in src
    assert not hasattr(hands, "start_jiggler")
    m = (ROOT / "hands/motion.py").read_text(encoding="utf-8")
    assert "plan_jiggle" not in m and "JIGGLE_DECAY" not in m


# ====================================================== gateway: claim push

def test_claim_lease_constants_unchanged(gw):
    assert (gw.CLAIM_TTL_DEFAULT, gw.CLAIM_TTL_MIN, gw.CLAIM_TTL_MAX) == (900, 30, 3600)
    assert gw.RESERVATION_GRACE == 30


def test_claim_grant_refresh_release_push_to_hands(gw, monkeypatch):
    sent = []
    monkeypatch.setattr(gw, "_post_claim_to_hands", lambda p: sent.append(p))
    monkeypatch.setattr(gw.threading, "Thread",
                        lambda target, args, daemon: type("T", (), {"start": lambda s: target(*args)})())
    gw._reset_coordination_state()
    c = gw.app.test_client()
    r = c.post("/v1/hands/claim", json={"holder": "a", "ttl_seconds": 120}, headers=TOKEN).get_json()
    assert r["granted"] and r["claim"]["expires_at"] - r["claim"]["since"] == 120  # lease unchanged
    c.post("/v1/hands/claim", json={"holder": "a", "ttl_seconds": 120}, headers=TOKEN)
    c.post("/v1/hands/release", json={"holder": "a"}, headers=TOKEN)
    assert [p["held"] for p in sent] == [True, True, False]
    assert [p["seq"] for p in sent] == sorted(p["seq"] for p in sent)
    assert 119 <= sent[0]["expires_in"] <= 120
    gw._reset_coordination_state()


# ===================================================== gateway: watchers

def test_watcher_timeout_default_30_settable_to_60(gw, monkeypatch):
    monkeypatch.delenv("WATCHER_TIMEOUT_SECONDS", raising=False)
    gw._reset_coordination_state()
    c = gw.app.test_client()
    assert c.get("/v1/watch", headers=TOKEN).get_json()["timeout_seconds"] == 30
    w = c.post("/v1/watch", json={"holder": "eyes"}, headers=TOKEN).get_json()["watcher"]
    assert w["timeout_seconds"] == 30
    w = c.post("/v1/watch", json={"holder": "eyes", "timeout_seconds": 60}, headers=TOKEN).get_json()
    assert w["watcher"]["timeout_seconds"] == 60
    w = c.post("/v1/watch", json={"holder": "eyes", "timeout_seconds": 600}, headers=TOKEN).get_json()
    assert w["watcher"]["timeout_seconds"] == 60  # clamped to the 60 s ceiling
    r = c.post("/v1/watch/config", json={"timeout_seconds": 60}, headers=TOKEN).get_json()
    assert r["timeout_seconds"] == 60
    assert c.get("/v1/watch", headers=TOKEN).get_json()["timeout_seconds"] == 60
    assert c.post("/v1/watch/config", json={"timeout_seconds": "x"}, headers=TOKEN).status_code == 400
    gw._reset_coordination_state()
    assert c.get("/v1/watch", headers=TOKEN).get_json()["timeout_seconds"] == 30


def test_watcher_timeout_from_env(gw, monkeypatch):
    monkeypatch.setenv("WATCHER_TIMEOUT_SECONDS", "60")
    gw._reset_coordination_state()
    assert gw._watcher_timeout_setting() == 60
    monkeypatch.delenv("WATCHER_TIMEOUT_SECONDS")
    gw._reset_coordination_state()


def test_watcher_lapses_after_its_timeout(gw):
    gw._reset_coordination_state()
    gw._touch_watcher("eyes", now=1000.0)
    assert [w["holder"] for w in gw._current_watchers(now=1029.0)] == ["eyes"]
    assert gw._current_watchers(now=1030.5) == []
    gw._touch_watcher("eyes", timeout_seconds=60, now=2000.0)
    assert gw._current_watchers(now=2059.0) and not gw._current_watchers(now=2060.5)
    gw._reset_coordination_state()


def test_watcher_independent_of_claim(gw):
    gw._reset_coordination_state()
    c = gw.app.test_client()
    c.post("/v1/watch", json={"holder": "eyes"}, headers=TOKEN)
    assert gw._current_claim() is None  # watching never takes the claim
    assert c.post("/v1/watch/release", json={"holder": "eyes"}, headers=TOKEN).get_json()["released"]
    assert gw._current_watchers() == []
    gw._reset_coordination_state()


def test_screen_hash_watcher_param_marks_watching(gw, monkeypatch):
    gw._reset_coordination_state()
    c = gw.app.test_client()
    c.get("/v1/screen/hash?watcher=poller", headers=TOKEN)  # backend may be down; mode still noted
    assert [w["holder"] for w in gw._current_watchers()] == ["poller"]
    gw._reset_coordination_state()


def test_info_reports_watchers(gw, monkeypatch):
    gw._reset_coordination_state()
    monkeypatch.setattr(gw, "_reachability", lambda: (False, False, None))
    c = gw.app.test_client()
    c.post("/v1/watch", json={"holder": "eyes"}, headers=TOKEN)
    info = c.get("/v1/info", headers=TOKEN).get_json()
    assert [w["holder"] for w in info["watchers"]] == ["eyes"]
    assert info["watcher_timeout_seconds"] == 30
    gw._reset_coordination_state()


# ========================================================= app: mode model

def test_mode_indicator_model(appmod):
    info = {"hands_reachable": True, "parser_reachable": True, "pc_state": None,
            "watchers": [{"holder": "eyes"}], "watcher_timeout_seconds": 30}
    m = appmod.build_dashboard_model(info, None, [], None, {}, 0)
    assert m["mode"] == "watcher" and m["watchers"] == ["eyes"] and m["watcher_timeout_s"] == 30
    assert "Mode: watcher · watching: eyes · watcher timeout" in appmod.format_diagnostics(m, "x")
    claim = {"holder": "eyes", "purpose": "", "expires_at": 100}
    m = appmod.build_dashboard_model(info, claim, [], None, {}, 0)
    assert m["mode"] == "control" and m["watchers"] == []  # the holder is not also "watching"
    m = appmod.build_dashboard_model(dict(info, watchers="junk"), None, [], None, {}, 0)
    assert m["mode"] == "none" and m["watchers"] == []


# ============================================================ GUI layout

def test_default_window_is_the_minimum_size():
    src = (ROOT / "hands_relay_v2_app.py").read_text(encoding="utf-8")
    w = int(re.search(r"width=(\d+),", src).group(1))
    h = int(re.search(r"height=(\d+),", src).group(1))
    mn = tuple(int(v) for v in re.search(r"min_size=\((\d+), (\d+)\)", src).groups())
    assert (w, h) == mn == (980, 700)


def test_ui_has_no_toggle_and_shows_readonly_state():
    html = (ROOT / "ui/index.html").read_text(encoding="utf-8")
    js = (ROOT / "ui/app.js").read_text(encoding="utf-8")
    assert 'id="jiggler"' not in html and "/api/jiggler" not in js
    assert 'id="pc-resting"' in html and 'id="agent-mode"' in html and 'id="watcher-setting"' in html


def test_css_token_row_cannot_collapse_at_small_size():
    css = (ROOT / "ui/app.css").read_text(encoding="utf-8")
    # the 980 px default must keep the 3-column grid (breakpoint below it)
    bp = int(re.search(r"@media \(max-width: (\d+)px\) \{\s*\.grid", css).group(1))
    assert bp < 980
    block = css[css.index(f"@media (max-width: {bp}px)"):]
    block = block[: block.index("\n}\n")]
    # four areas rows -> four row tracks, only the last one flexes
    assert "grid-template-rows: auto auto auto minmax(0, 1fr)" in block
    for card in ("control", "desktop", "token", "feed"):
        assert re.search(rf"\.card-{card} \{{[^}}]*align-self: start", css)
