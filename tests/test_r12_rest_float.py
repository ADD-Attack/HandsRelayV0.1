"""R12 — wishlist #36: the resting float TRAVELS instead of teleporting.

R11 planned each float step with motion.plan_rest but executed it as a
single pyautogui.moveTo(duration=0): the pointer snapped 1-3 px, held
still, snapped again. R12 routes the travel through the human-like
planner (motion.plan_rest_path: _segment Bezier/minimum-jerk curve,
densified to pixel resolution) and _RestingHand._move glides it.

Continuity threshold used throughout: consecutive emitted pointer
positions differ by at most 1 px per axis (Chebyshev <= 1). One pixel
is the display's own quantum — no smaller move exists — so this is the
strongest continuity a pointer can have, and it is exactly what the
old snap violated (a whole 1-3 px step in one emitted jump).

The hard contract is unchanged from R11 (claim-held + agent-idle only,
starts after an act, owner input overrides instantly, no timeout,
radius <= REST_RADIUS_PX, ramp 0->full over REST_RAMP_S, float motion
stays on the SelfInputLedger): R11's suite covers most of it; here we
add the glide-level proofs and the mid-glide owner-grab abort.
"""
import importlib.util
import math
import os
import random
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("RELAY_GATEWAY_TOKEN", "r12-token")
os.environ.setdefault("HANDS_SECRET", "r12-secret")
TOKEN = {"X-Relay-Token": os.environ["RELAY_GATEWAY_TOKEN"]}


def _load(rel, tag):
    spec = importlib.util.spec_from_file_location(tag, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def motion():
    return _load("hands/motion.py", "r12_motion")


@pytest.fixture(scope="module")
def hands():
    return _load("hands/hands_relay_hands.py", "r12_hands")


@pytest.fixture(scope="module")
def gw():
    return _load("services/gateway/app.py", "r12_gateway")


def _cheb(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


# ============================================================ the glide

GLIDES = [
    (0, 0, 1, 0), (0, 0, 3, 0), (0, 0, 0, -2), (0, 0, 3, 3),
    (0, 0, 5, 1), (10, 10, 7, 12), (0, 0, -4, -4), (0, 0, 2, -1),
]


def test_glide_visits_every_pixel_no_jumps(motion):
    for x0, y0, x1, y1 in GLIDES:
        for seed in range(20):
            steps = motion.plan_rest_path(x0, y0, x1, y1, random.Random(seed))
            assert steps, (x0, y0, x1, y1, seed)
            assert steps[-1][:2] == (x1, y1)  # lands exactly on target
            prev = (x0, y0)
            for sx, sy, delay in steps:
                assert isinstance(sx, int) and isinstance(sy, int)
                assert delay >= 0.0
                assert _cheb(prev, (sx, sy)) <= 1, (prev, (sx, sy))
                prev = (sx, sy)
            total = sum(s[2] for s in steps)
            assert 0.05 <= total <= motion.REST_GLIDE_MAX_S + 1e-9


def test_glide_travels_instead_of_jumping(motion):
    # The old behaviour for a 3 px step was ONE emitted jump of 3 px.
    # The glide must break it into single-pixel travel, spread in time.
    for x0, y0, x1, y1 in [(0, 0, 3, 0), (0, 0, 3, 3), (0, 0, 5, 1), (0, 0, -4, -4)]:
        for seed in range(20):
            steps = motion.plan_rest_path(x0, y0, x1, y1, random.Random(seed))
            assert len(steps) >= 2
            assert max(_cheb(a[:2], b[:2]) for a, b in zip([(x0, y0)] + [s[:2] for s in steps][:-1], steps)) <= 1
            assert sum(s[2] for s in steps) >= 0.08  # travel takes time


def test_glide_zero_length_is_empty(motion):
    assert motion.plan_rest_path(7, 7, 7, 7, random.Random(1)) == []


LONG_GLIDES = [
    (0, 0, 40, 3),      # the reviewer's F1 repro shape (long, shallow)
    (0, 0, 50, 0),      # flat, 50 px span
    (0, 0, 48, 48),     # long diagonal
    (0, 0, 3, 40),      # steep
    (0, 0, -40, -3),    # negative directions
    (0, 0, 50, 12),
    (0, 0, 12, -50),
    (0, 0, 25, 25),
    (100, 100, 60, 97),  # offset start, travelling back
]


def test_long_glides_hold_the_1px_bound_at_any_span(motion):
    # Regression (review F1): the densifier originally interpolated
    # from the MOVING emitted point using the whole-span fraction,
    # so sub-steps grew super-linearly and anchor spans >= 4 Chebyshev
    # at longer glides emitted 2 px jumps. The small shapes above
    # (max ~5.7 px) structurally cannot reach that failure, and
    # plan_rest_path is the designated reuse point for the longer
    # #28/#30 glides — so the bound is asserted here at spans up to
    # 50 px, across slope classes and 50 seeds each.
    for x0, y0, x1, y1 in LONG_GLIDES:
        for seed in range(50):
            steps = motion.plan_rest_path(x0, y0, x1, y1, random.Random(seed))
            assert steps, (x0, y0, x1, y1, seed)
            assert steps[-1][:2] == (x1, y1)  # exact landing
            # Travel, not a snap: covering the dominant axis at
            # <= 1 px per emitted step needs at least this many.
            assert len(steps) >= max(abs(x1 - x0), abs(y1 - y0))
            prev = (x0, y0)
            for sx, sy, delay in steps:
                assert delay >= 0.0
                assert _cheb(prev, (sx, sy)) <= 1, (prev, (sx, sy), seed)
                prev = (sx, sy)
            total = sum(s[2] for s in steps)
            assert 0.0 < total <= motion.REST_GLIDE_MAX_S + 1e-6


def test_long_glide_reviewer_repro(motion):
    # The exact F1 verdict case: (0,0) -> (40,3) seed 1 emitted
    # (22, -1) -> (24, 0) — a 2 px jump — before the fix.
    steps = motion.plan_rest_path(0, 0, 40, 3, random.Random(1))
    pts = [(0, 0)] + [s[:2] for s in steps]
    assert all(_cheb(a, b) <= 1 for a, b in zip(pts, pts[1:]))
    assert pts[-1] == (40, 3)


def test_float_start_frame_matches_anchor_rounding(motion):
    # F2: the emission frame must sit in the same _rhu lattice as the
    # anchors; int() truncation disagreed by up to 1 px on negative
    # float starts, which could stack with real motion into a 2 px
    # first emitted step. Measure the chain from the _rhu frame;
    # targets are ints (the production call shape — pyautogui targets
    # built with int(round(...))).
    starts = [(-2.6, -3.7), (-2.4, -3.2), (3.7, -1.2), (-0.4, 0.6),
              (10.5, -7.5), (-49.5, 20.25)]
    for fx, fy in starts:
        frame = (motion._rhu(fx), motion._rhu(fy))
        for tx, ty in [(6, 4), (-6, -5), (30, -20)]:
            for seed in range(30):
                steps = motion.plan_rest_path(fx, fy, tx, ty, random.Random(seed))
                assert steps[-1][:2] == (tx, ty)
                prev = frame
                for sx, sy, _ in steps:
                    assert _cheb(prev, (sx, sy)) <= 1, (prev, (sx, sy))
                    prev = (sx, sy)


def test_glide_duration_scales_with_distance_and_is_bounded(motion):
    def mean(dist):
        rng = random.Random(11)
        return sum(motion.rest_glide_seconds(dist, rng) for _ in range(300)) / 300

    for d in (0.0, 1.0, 3.0, 6.0, 40.0):
        rng = random.Random(5)
        for _ in range(200):
            v = motion.rest_glide_seconds(d, rng)
            assert 0.0 < v <= motion.REST_GLIDE_MAX_S
    assert mean(0.0) < mean(3.0) < mean(6.0)


def test_glide_never_leaves_the_radius_disc(motion):
    # Both endpoints are float offsets inside the resting disc; every
    # emitted sub-step must stay within rounding of the disc too.
    rng = random.Random(23)
    for _ in range(300):
        a = (rng.uniform(-4, 4), rng.uniform(-4, 4))
        b = (rng.uniform(-4, 4), rng.uniform(-4, 4))
        if math.hypot(*a) > 4.0 or math.hypot(*b) > 4.0:
            continue
        steps = motion.plan_rest_path(a[0], a[1], b[0], b[1], rng)
        for sx, sy, _ in steps:
            assert abs(sx) <= 4 and abs(sy) <= 4


# ============================================================ executor

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
        self.tick["last_input"] = self.tick["t"]


@pytest.fixture
def rig(hands, monkeypatch):
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


def test_glide_move_emits_continuous_positions(hands, rig):
    pag, tick = rig
    r, clock = _rest(hands)
    landed = r._move((503, 501))
    assert landed == (503, 501)
    assert pag.moves[-1] == (503, 501)
    prev = (500, 500)
    for mv in pag.moves:
        assert _cheb(prev, mv) <= 1  # the R11 snap would jump up to 3-4
        prev = mv
    assert len(pag.moves) >= 3  # travel, not one jump
    assert hands._LEDGER._spans  # the glide is on the self-input ledger


def test_owner_grab_mid_glide_aborts(hands, rig, monkeypatch):
    pag, tick = rig

    def owner_yanks(x, y, duration=None):
        pag.moveTo(x, y, duration)
        if len(pag.moves) == 1:
            pag.pos = (pag.pos[0] + 30, pag.pos[1])  # owner's hand grabs it

    monkeypatch.setattr(hands.pyautogui, "moveTo", owner_yanks)
    r, clock = _rest(hands)
    assert r._move((503, 500)) is None  # fought -> episode ends (as R11)
    assert len(pag.moves) == 1  # aborted at the next sub-step check


def test_move_exception_mid_glide_still_closes_ledger_span(hands, rig, monkeypatch):
    # F3: an injection fault mid-glide must not leave the SelfInputLedger
    # span standing at the widened 1500 ms provisional (a 6x wider window
    # in which real owner input could be misread as self-input than
    # R11's equivalent fault). The try/finally in _move closes the span
    # at its true end on the fault path too; the exception itself still
    # propagates to _run's handler exactly as before.
    pag, tick = rig
    calls = {"n": 0}

    def flaky(x, y, duration=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injection failed")
        pag.moveTo(x, y, duration)

    monkeypatch.setattr(hands.pyautogui, "moveTo", flaky)
    r, clock = _rest(hands)
    with pytest.raises(RuntimeError):
        r._move((503, 500))
    assert len(pag.moves) == 1  # died on the second sub-step
    start, end = hands._LEDGER._spans[-1]
    assert start == tick["t"]
    assert end == (tick["t"] + 50) & 0xFFFFFFFF  # truncated, not t0 + 1500


def _run(r, clock, tick, until, dt=0.1):
    while clock["t"] < until:
        r.step(clock["t"])
        clock["t"] += dt
        tick["t"] += int(dt * 1000)


def test_episode_float_travel_is_continuous_and_local(hands, rig):
    pag, tick = rig
    r, clock = _rest(hands)
    r.set_claim(True, 900, 1, clock["t"])
    _run(r, clock, tick, 106.0)
    assert pag.moves and r.status()["active"]
    prev = (500, 500)
    for mv in pag.moves:
        assert _cheb(prev, mv) <= 1  # across and within glides
        assert abs(mv[0] - 500) <= 4 and abs(mv[1] - 500) <= 4  # radius
        prev = mv


# ============================================================ release id

def test_info_reports_release(gw, monkeypatch):
    gw._reset_coordination_state()
    monkeypatch.setattr(gw, "_reachability", lambda: (False, False, None))
    info = gw.app.test_client().get("/v1/info", headers=TOKEN).get_json()
    assert info["release"] == "v3 R12" == gw.RELEASE
    gw._reset_coordination_state()
