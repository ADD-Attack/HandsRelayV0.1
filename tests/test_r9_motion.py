"""R9 Stage 2 — human-like pointer system (#24, #28-#30). The R9 idle
jiggler tests were retired in R11 (replaced by the resting hand; see
tests/test_r11_resting_watch.py).

Planners are tested directly (hands/motion.py, pure); the hands
integration is tested with pyautogui/time stubbed. Windows-live feel is
out of reach here — see docs/VERIFICATION.md "R9 motion + jiggler".
"""
import importlib.util
import json
import math
import random
import statistics
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(rel, tag):
    spec = importlib.util.spec_from_file_location(tag, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def motion():
    return _load("hands/motion.py", "r9_motion")


@pytest.fixture(scope="module")
def hands():
    return _load("hands/hands_relay_hands.py", "r9_motion_hands")


# ============================================================ path physics

def test_path_ends_exactly_on_target_and_is_seeded(motion):
    for seed in range(50):
        p = motion.plan_move(10, 20, 900, 500, random.Random(seed))
        assert (p[-1][0], p[-1][1]) == (900, 500)
        assert p == motion.plan_move(10, 20, 900, 500, random.Random(seed))


def test_no_fixed_curve_across_movements(motion):
    mids = {tuple(round(c) for c in motion.plan_move(0, 0, 800, 0, random.Random(s))[len(
        motion.plan_move(0, 0, 800, 0, random.Random(s))) // 2][:2]) for s in range(30)}
    assert len(mids) > 20  # every movement draws its own curve


def test_path_arcs_off_the_chord(motion):
    devs = [max(abs(q[1]) for q in motion.plan_move(0, 0, 800, 0, random.Random(s)))
            for s in range(30)]
    assert min(devs) > 5  # never a straight line


def test_velocity_slow_start_fast_middle_hard_stop(motion):
    rng = random.Random(3)
    # a direct path (no overshoot) — retry seeds until one is direct
    for seed in range(200):
        rng = random.Random(seed)
        if rng.random() >= 0.70:
            break
    p = motion.plan_move(0, 0, 1000, 0, random.Random(seed))
    pts = [(0.0, 0.0)] + [(q[0], q[1]) for q in p]
    speeds = [math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]) / max(1e-9, p[i][2])
              for i in range(len(p))]
    n = len(speeds)
    first, mid, last = speeds[: n // 8], speeds[n // 3: 2 * n // 3], speeds[-(n // 8):]
    assert statistics.mean(mid) > 2 * statistics.mean(first)
    assert statistics.mean(mid) > 2 * statistics.mean(last)


def test_fitts_small_targets_are_slower(motion):
    small = statistics.mean(motion.fitts_duration(600, 6, random.Random(s)) for s in range(200))
    large = statistics.mean(motion.fitts_duration(600, 200, random.Random(s)) for s in range(200))
    assert small > large + 0.15


def test_structural_miss_rate_near_15_percent(motion):
    misses = 0
    trials = 2000
    for s in range(trials):
        rng = random.Random(s)
        if rng.random() < 0.15:
            misses += 1
    # plan_move draws the branch with its first rng.random() after the
    # (non-random) setup, so the same seeds drive the branch choice:
    hits = 0
    for s in range(trials):
        p = motion.plan_move(0, 0, 600, 0, random.Random(s))
        # a miss path reaches a point 5-10 px from the target, then corrects
        near = [math.hypot(q[0] - 600, q[1]) for q in p[:-1]]
        if any(4.5 <= d <= 10.5 for d in near) and max(q[0] for q in p) <= 610.5:
            hits += 1
    assert 0.10 < misses / trials < 0.20
    assert hits >= misses * 0.8


def test_overshoot_then_settle_exists(motion):
    over = sum(1 for s in range(200)
               if max(q[0] for q in motion.plan_move(0, 0, 600, 0, random.Random(s))) > 601)
    assert 40 < over < 180


def test_tremor_noise_present(motion):
    # same curve with and without noise differs: two paths of the same
    # seed only differ through rng-drawn noise, so check roughness
    p = motion.plan_move(0, 0, 900, 0, random.Random(11))
    ys = [q[1] for q in p]
    second = [ys[i + 1] - 2 * ys[i] + ys[i - 1] for i in range(1, len(ys) - 1)]
    assert any(abs(v) > 0.05 for v in second)


def test_tiny_move_single_step(motion):
    p = motion.plan_move(0, 0, 2, 1, random.Random(1))
    assert len(p) == 1 and p[0][:2] == (2, 1)


# =========================================================== timing draws

def test_draw_ranges(motion):
    rng = random.Random(5)
    for _ in range(500):
        assert 0.04 <= motion.hover_delay(rng) <= 0.12
        assert 0.05 <= motion.click_hold(rng) <= 0.15
        assert 0.15 <= motion.hand_travel(rng) <= 0.30
        assert 0.20 <= motion.attention_delay(rng) <= 0.70
        assert 0.20 <= motion.attention_delay(rng, 10 ** 6) <= 0.70


def test_attention_scales_with_text(motion):
    lo = statistics.mean(motion.attention_delay(random.Random(s), 0) for s in range(300))
    hi = statistics.mean(motion.attention_delay(random.Random(s), 3000) for s in range(300))
    assert hi > lo + 0.3


def test_landing_point_gaussian_inside_box(motion):
    rng = random.Random(9)
    pts = [motion.landing_point(500, 300, 120, 40, rng) for _ in range(3000)]
    assert all(441 <= x <= 559 and 281 <= y <= 319 for x, y in pts)
    assert sum(1 for p in pts if p == (500, 300)) < 300  # not always dead-centre
    centre = sum(1 for x, y in pts if abs(x - 500) <= 20)
    assert centre > len(pts) * 0.6  # centre-weighted
    assert any(x <= 443 or x >= 557 for x, _ in pts)  # occasional edge hits
    assert motion.landing_point(5, 6, None, None, rng) == (5, 6)


def test_click_timing_fits_budget_and_degrades(motion):
    rng = random.Random(2)
    for dist in (0, 10, 100, 400, 900, 1600, 3000):
        for _ in range(100):
            a = motion.attention_delay(rng, 5000)
            t = motion.fitts_duration(max(dist, 1), 4, rng)
            h, d = motion.hover_delay(rng), motion.click_hold(rng)
            a2, t2, h2, d2 = motion.fit_click_timing(dist, a, t, h, d)
            assert a2 + t2 + h2 + d2 <= motion.click_budget(dist) + 1e-9
            assert d2 >= 0.05 and t2 > 0
    assert motion.fit_click_timing(500, 0.0, 0.2, 0.05, 0.06) == (0.0, 0.2, 0.05, 0.06)
    assert motion.click_budget(500) - motion.legacy_click_seconds(500) == pytest.approx(0.5)


def test_scroll_chunks(motion):
    rng = random.Random(4)
    plan = motion.plan_scroll(-10, rng)
    assert sum(c for c, _ in plan) == -10
    assert len({c for c, _ in plan}) > 1 or len(plan) > 3
    assert plan[-1][1] == 0.0 and all(0.03 <= p <= 0.12 for _, p in plan[:-1])
    assert motion.plan_scroll(0, rng) == []


# ========================================================= self-input ledger

def test_ledger_excludes_jiggler_spans(motion):
    led = motion.SelfInputLedger()
    # owner last touched at tick 10_000; now 70_000 -> owner idle 60 s
    assert led.owner_idle(60.0, 70_000) == 60.0
    led.record(69_000, 69_500)  # jiggler injected
    # OS now says last input at 69_400 (ours): owner idle continues
    assert led.owner_idle(0.6, 70_000) == pytest.approx(60.0)
    # owner moves at 69_900 (outside the span + slack): owner idle resets
    assert led.owner_idle(0.1, 70_000) == pytest.approx(0.1)


def test_ledger_without_owner_baseline_reports_raw(motion):
    led = motion.SelfInputLedger()
    led.record(100, 200)
    assert led.owner_idle(0.05, 200) == 0.05


def test_ledger_tick_wraparound(motion):
    led = motion.SelfInputLedger()
    assert led.owner_idle(30.0, 0xFFFFFFFF - 1000) == 30.0
    led.record(0xFFFFFFFF - 500, 300)
    assert led.owner_idle(0.4, 700) == pytest.approx(31.7, abs=0.01)


def test_ledger_truncate_reopens_owner_window(motion):
    led = motion.SelfInputLedger()
    led.owner_idle(50.0, 60_000)
    led.record(59_000, 61_000)
    led.truncate_last(59_200)
    assert led.owner_idle(0.2, 60_000) == pytest.approx(0.2)  # 59_800: owner


def test_ledger_none_passthrough(motion):
    assert motion.SelfInputLedger().owner_idle(None, 5) is None


# ============================================================ input mutex

def test_mutex_hand_travel_only_on_device_switch(motion):
    now = [100.0]
    slept = []
    m = motion.InputMutex(clock=lambda: now[0], sleep=slept.append)
    rng = random.Random(1)
    assert m.acquire("keys", rng) == 0.0
    m.release("keys")
    assert m.acquire("keys", rng) == 0.0  # same device: no travel
    m.release("keys")
    w = m.acquire("mouse", rng)  # switch immediately: travel 150-300 ms
    m.release("mouse")
    assert 0.15 <= w <= 0.30 and slept == [w]
    now[0] += 5.0
    assert m.acquire("keys", rng) == 0.0  # long gap already covers travel
    m.release("keys")


def test_mutex_blocks_jiggler_while_op_runs(motion):
    m = motion.InputMutex(sleep=lambda s: None)
    m.acquire("keys", random.Random(1))
    got = []
    t = threading.Thread(target=lambda: got.append(m.try_acquire()))
    t.start()
    t.join()
    assert got == [False]
    m.release("keys")


# ====================================================== hands integration

class _Pag:
    def __init__(self, pos=(100, 100)):
        self.pos = pos
        self.events = []
        self.fight_at = None

    def position(self):
        return self.pos

    def moveTo(self, x, y, duration=None):
        self.events.append(("move", x, y))
        self.pos = (int(round(x)), int(round(y)))
        if self.fight_at is not None and len([e for e in self.events if e[0] == "move"]) == self.fight_at:
            self.pos = (self.pos[0] + 50, self.pos[1])

    def mouseDown(self, x=None, y=None, button="left"):
        self.events.append(("down", x, y, button))

    def mouseUp(self, x=None, y=None, button="left"):
        self.events.append(("up", x, y, button))

    def click(self, x=None, y=None, button="left"):
        self.events.append(("click", x, y, button))

    def scroll(self, n):
        self.events.append(("scroll", n))


class _Time:
    def __init__(self):
        self.sleeps = []
        self.t = 1000.0

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def monotonic(self):
        return self.t

    def time(self):
        return self.t


@pytest.fixture
def rig(hands, monkeypatch):
    pag = _Pag()
    clock = _Time()
    for name in ("position", "moveTo", "mouseDown", "mouseUp", "click", "scroll"):
        monkeypatch.setattr(hands.pyautogui, name, getattr(pag, name))
    monkeypatch.setattr(hands, "time", clock)
    monkeypatch.setattr(hands, "_probe_lock_state", lambda: (None, None))
    monkeypatch.setattr(hands, "_LEDGER", hands.motion.SelfInputLedger())
    monkeypatch.setattr(hands, "_INPUT", hands.motion.InputMutex(sleep=lambda s: None))
    monkeypatch.delenv("HANDS_HUMAN_MOTION", raising=False)
    return pag, clock


def _call(hands, fn, path, body):
    with hands.app.test_request_context(path, method="POST", json=body):
        r = fn()
    return r if isinstance(r, dict) else r.get_json()


def test_flags_defaults(hands, monkeypatch):
    monkeypatch.delenv("HANDS_HUMAN_MOTION", raising=False)
    monkeypatch.setattr(hands, "_cfg", {})
    assert hands._human_motion_default() is True
    monkeypatch.setenv("HANDS_HUMAN_MOTION", "0")
    assert hands._human_motion_default() is False
    assert not hasattr(hands, "_jiggler_enabled")  # R11: no jiggler flag


def test_human_click_lands_in_box_within_budget(hands, rig):
    pag, clock = rig
    body = _call(hands, hands.click, "/click", {"x": 700, "y": 400, "w": 80, "h": 30})
    downs = [e for e in pag.events if e[0] == "down"]
    ups = [e for e in pag.events if e[0] == "up"]
    assert len(downs) == 1 and downs[0][1:3] == ups[0][1:3] == (body["x"], body["y"])
    assert 661 <= body["x"] <= 739 and 386 <= body["y"] <= 414
    t = body["timing"]
    assert t["attention"] + t["travel"] + t["hover"] + t["hold"] <= t["budget"] + 0.005
    assert sum(clock.sleeps) <= t["budget"] + 0.01
    moves = [e for e in pag.events if e[0] == "move"]
    assert (moves[-1][1], moves[-1][2]) == (body["x"], body["y"])


def test_human_click_without_box_hits_exact_point(hands, rig):
    pag, _ = rig
    body = _call(hands, hands.click, "/click", {"x": 300, "y": 250, "attention": False})
    assert (body["x"], body["y"]) == (300, 250)
    assert body["timing"]["attention"] == 0.0


def test_motion_flag_off_restores_legacy_click(hands, rig, monkeypatch):
    pag, _ = rig
    monkeypatch.setenv("HANDS_HUMAN_MOTION", "0")
    _call(hands, hands.click, "/click", {"x": 5, "y": 6})
    assert pag.events == [("move", 5, 6), ("click", 5, 6, "left")]


def test_human_drag_exact_endpoints_and_release(hands, rig):
    pag, _ = rig
    _call(hands, hands.drag, "/drag", {"x1": 200, "y1": 200, "x2": 600, "y2": 380})
    kinds = [e[0] for e in pag.events]
    d, u = kinds.index("down"), kinds.index("up")
    assert pag.events[d - 1][1:3] == (200, 200)
    assert pag.events[u - 1][1:3] == (600, 380)
    assert kinds.count("move") > 10 and u == len(kinds) - 1


def test_human_drag_releases_button_when_move_raises(hands, rig, monkeypatch):
    pag, _ = rig
    n = {"i": 0}
    real = pag.moveTo

    def boom(x, y, duration=None):
        n["i"] += 1
        if any(e[0] == "down" for e in pag.events):
            raise RuntimeError("boom")
        real(x, y, duration)

    monkeypatch.setattr(hands.pyautogui, "moveTo", boom)
    with pytest.raises(RuntimeError):
        _call(hands, hands.drag, "/drag", {"x1": 200, "y1": 200, "x2": 600, "y2": 380})
    assert [e[0] for e in pag.events].count("up") == 1


def test_human_scroll_is_chunked(hands, rig):
    pag, _ = rig
    _call(hands, hands.scroll, "/scroll", {"clicks": -9})
    chunks = [e[1] for e in pag.events if e[0] == "scroll"]
    assert sum(chunks) == -9 and len(chunks) > 1


def test_yield_ignores_resting_hand_but_not_owner(hands, rig, monkeypatch):
    monkeypatch.setattr(hands, "_tick_ms", lambda: 70_000)
    monkeypatch.setattr(hands, "_probe_lock_state", lambda: (False, 60.0))
    with hands.app.test_request_context("/click", method="POST", json={}):
        assert hands._yield_if_active() is None  # owner baseline learned
    hands._LEDGER.record(69_000, 69_500)
    monkeypatch.setattr(hands, "_probe_lock_state", lambda: (False, 0.6))  # our resting-hand move
    with hands.app.test_request_context("/click", method="POST", json={}):
        assert hands._yield_if_active() is None
    monkeypatch.setattr(hands, "_probe_lock_state", lambda: (False, 0.05))  # owner
    monkeypatch.setattr(hands, "_enum_windows", lambda: [])
    with hands.app.test_request_context("/click", method="POST", json={}):
        resp, status = hands._yield_if_active()
    assert status == 409


def test_state_reports_motion_and_resting_hand(hands, rig, monkeypatch):
    monkeypatch.setattr(hands, "_probe_lock_state", lambda: (False, 3.0))
    monkeypatch.setattr(hands, "_probe_display_raw",
                        lambda: {"screensaver": None, "display_off_after_seconds": None})
    body = hands.state()
    assert body["human_motion"] is True
    assert "jiggler" not in body
    assert set(body["resting_hand"]) == {"active", "claim_held", "intensity", "moves", "owner_kills"}
    assert body["owner_idle_seconds"] == 3.0
