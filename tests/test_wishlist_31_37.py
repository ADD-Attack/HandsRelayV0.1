"""Wishlist #31-#37 (owner, 2026-10-21): pause on takeover, a live watch
tracker, the paste path's owner guard, a live claim countdown, an
"aborted" state for owner takeovers, a gliding resting hand, and the
product-line rename -- each pinned here.

    python -m pytest -q tests/test_wishlist_31_37.py
"""
import importlib.util
import io
import itertools
import random
import re
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = {"X-Relay-Token": "test-token"}
SECRET = {"X-Secret": "s3cret-for-tests"}
UI_KEY = "ui-key-for-tests"
UI = {"X-UI-Key": UI_KEY, "Host": "127.0.0.1:5999"}
YIELD = {"ok": False, "error": "user active — control yielded", "error_code": "yield"}
_seq = itertools.count()


def _load(rel, tag):
    spec = importlib.util.spec_from_file_location(f"{tag}_{next(_seq)}", ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def gw():
    mod = _load("services/gateway/app.py", "wl_gateway")
    mod.TOKEN = "test-token"
    mod.HANDS_SECRET = "hands-secret"
    mod._post_claim_to_hands = lambda payload: None
    yield mod
    mod.stop_background(timeout=0)


@pytest.fixture
def client(gw):
    return gw.app.test_client()


@pytest.fixture
def hands():
    mod = _load("hands/hands_relay_hands.py", "wl_hands")
    mod.SHARED_SECRET = SECRET["X-Secret"]
    mod._probe_lock_state = lambda: (None, None)
    yield mod
    mod.stop_background()


@pytest.fixture(scope="module")
def appmod():
    return _load("hands_relay_v2_app.py", "wl_app")


def _png():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (1, 2, 3)).save(buf, format="PNG")
    return buf.getvalue()


def _wait_for(predicate, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ------------------------------------------------------------ #31 pause

def test_owner_pause_refuses_acts_holds_queued_ones_and_only_owner_resumes(gw, client, monkeypatch):
    monkeypatch.setattr(gw, "_ensure_receipt_thread", lambda: None)
    ok, state, _ = gw.set_pause(True, by="owner")
    assert ok and state["by"] == "owner"
    r = client.post("/v1/act", json={"op": "click", "x": 1, "y": 2}, headers=TOKEN)
    assert r.status_code == 409 and r.get_json()["error_code"] == "paused"
    assert r.headers.get("X-Relay-Paused") == "1"  # a polling agent can see it on any response
    r = client.post("/v1/act/queued", json={"op": "click", "x": 1, "y": 2}, headers=TOKEN)
    assert r.status_code == 202 and r.get_json()["state"] == "held"  # waits for resume
    r = client.post("/v1/resume", json={"holder": "muse"}, headers=TOKEN)
    assert r.status_code == 403  # the agent cannot lift the owner's pause
    assert gw.set_pause(False, by="owner")[0] is True
    r = client.get("/v1/pause", headers=TOKEN)
    assert r.get_json()["paused"] is False and "X-Relay-Paused" not in r.headers


def test_agent_can_pause_and_resume_itself(gw, client):
    r = client.post("/v1/pause", json={"holder": "muse", "reason": "owner took over"}, headers=TOKEN)
    assert r.get_json()["pause"]["by"] == "agent"
    assert client.post("/v1/resume", json={"holder": "muse"}, headers=TOKEN).status_code == 200
    assert gw._pause_state() is None


def test_pause_stops_the_resting_hand_and_resume_restores_it(gw, client, monkeypatch):
    pushes = []
    monkeypatch.setattr(gw, "_post_claim_to_hands", pushes.append)
    client.post("/v1/hands/claim", json={"holder": "muse"}, headers=TOKEN)
    assert _wait_for(lambda: len(pushes) == 1) and pushes[-1]["held"] is True
    gw.set_pause(True, by="owner")
    assert _wait_for(lambda: len(pushes) == 2) and pushes[-1]["held"] is False
    gw.set_pause(False, by="owner")
    assert _wait_for(lambda: len(pushes) == 3) and pushes[-1]["held"] is True


# ----------------------------------------------- #32 watch tracker, #37

def test_screen_reads_are_tracked_and_reported(gw, client, monkeypatch):
    png = _png()
    monkeypatch.setattr(gw, "_fetch_screenshot", lambda **kw: (png, "image/png", None))
    monkeypatch.setattr(gw, "_reachability", lambda: (True, True, None))
    info = client.get("/v1/info", headers=TOKEN).get_json()
    assert info["last_poll"]["at"] is None and info["relay_line"] == "v4"
    client.get("/v1/screen/hash?watcher=muse", headers=TOKEN)
    last = client.get("/v1/info", headers=TOKEN).get_json()["last_poll"]
    assert last["route"] == "/v1/screen/hash" and last["watcher"] == "muse" and last["age_s"] < 5


def test_watch_state_names_what_the_agent_is_doing(appmod):
    ws = appmod.watch_state
    assert ws({"by": "owner"}, ["muse"], None, 100.0) == "paused"
    assert ws(None, ["muse"], None, 100.0) == "watching"
    assert ws(None, [], {"at": 90.0}, 100.0) == "polling"
    assert ws(None, [], {"at": 0.0}, 100.0) == "idle"
    assert ws(None, [], None, 100.0) == "idle"


def test_model_carries_pause_last_poll_and_absolute_expiry(appmod):
    info = {
        "hands_reachable": True, "parser_reachable": True, "watchers": [],
        "pause": {"by": "owner", "holder": "", "reason": "", "since": 50.0},
        "last_poll": {"at": 95.0, "route": "/v1/screen/hash", "watcher": "muse"},
    }
    model = appmod.build_dashboard_model(info, {"holder": "muse", "expires_at": 1000.0}, [], None, {}, 100.0)
    assert model["pause"]["by"] == "owner" and model["watch_state"] == "paused"
    assert model["last_poll"]["watcher"] == "muse"
    assert model["claim"]["expires_at"] == 1000.0  # #34: the window counts down from this
    assert "PAUSED by owner" in appmod.format_diagnostics(model, "12:00:00")


class _Root:
    def after(self, ms, fn, *args):
        pass


def test_owner_pause_button_and_live_view(appmod, gw, monkeypatch):
    monkeypatch.setattr(appmod, "_autostart_enabled", lambda: False)
    relay = appmod.RelayApp(_Root())
    ui = appmod.build_ui_server(relay, UI_KEY, [5999]).test_client()
    assert ui.post("/api/pause", json={"paused": True}, headers=UI).status_code == 409  # not started
    relay._gateway_module = gw
    gw._claim = {"holder": "muse", "purpose": "", "since": time.time(), "expires_at": time.time() + 600}
    r = ui.post("/api/pause", json={"paused": True}, headers=UI)
    assert r.get_json()["paused"] is True and gw._pause_state()["by"] == "owner"
    view = ui.get("/api/state", headers=UI).get_json()
    assert view["pause"]["by"] == "owner" and view["relay_line"] == "v4"
    assert isinstance(view["server_time"], float)
    assert 590 < view["claim_expires_at"] - view["server_time"] <= 600  # #34: live, per tick
    assert ui.post("/api/pause", json={"paused": False}, headers=UI).get_json()["paused"] is False
    assert ui.post("/api/pause", json={"paused": "yes"}, headers=UI).status_code == 400


# ------------------------------------------------- #33 paste-path guard

class _Clip:
    def __init__(self):
        self.text = "previous"

    def get_text(self):
        return self.text

    def set_text(self, text):
        self.text = text


@pytest.fixture
def typing(hands, monkeypatch):
    clip, pressed = _Clip(), []
    monkeypatch.setattr(hands, "_clipboard_backend", lambda: clip)
    monkeypatch.setattr(hands.pyautogui, "hotkey", lambda *keys: pressed.append(keys))
    monkeypatch.setattr(hands._owner_monitor, "ensure_started", lambda wait=0.5: False)
    monkeypatch.setattr(hands, "_probe_locked", lambda: False)
    monkeypatch.setattr(hands.time, "sleep", lambda s: None)
    return clip, pressed


def _snapshots(*values):
    queue = list(values)
    return lambda: queue.pop(0) if len(queue) > 1 else queue[0]


def test_paste_aborts_when_the_owner_moves_before_ctrl_v(hands, typing, monkeypatch):
    clip, pressed = typing
    # Guard baseline, then an owner event while the clipboard was being set.
    monkeypatch.setattr(hands, "_owner_input_snapshot", _snapshots((0, 0), (1, 0)))
    r = hands.app.test_client().post("/type", json={"text": "x" * 200}, headers=SECRET)
    body = r.get_json()
    assert r.status_code == 409 and body["error_code"] == "type_aborted"
    assert body["abort_reason"] == "owner" and body["method_used"] == "paste"
    assert body["chars_typed"] == 0 and body["guarded"] is True
    assert pressed == []  # Ctrl+V never pressed
    assert clip.text == "previous"  # the owner's clipboard is restored


def test_paste_aborts_when_the_target_window_changes(hands, typing, monkeypatch):
    clip, pressed = typing
    monkeypatch.setattr(hands, "_owner_input_snapshot", lambda: (0, 0))
    monkeypatch.setattr(hands, "_foreground_hwnd", _snapshots(10, 20))
    r = hands.app.test_client().post("/type", json={"text": "y" * 200}, headers=SECRET)
    assert r.status_code == 409 and r.get_json()["abort_reason"] == "foreground_changed"
    assert pressed == []


def test_guarded_paste_reports_its_guard(hands, typing, monkeypatch):
    clip, pressed = typing
    monkeypatch.setattr(hands, "_owner_input_snapshot", lambda: (0, 0))
    body = hands.app.test_client().post("/type", json={"text": "z" * 200}, headers=SECRET).get_json()
    assert body["ok"] is True and body["method_used"] == "paste"
    assert body["owner_guard"] == "hook" and body["guarded"] is True
    assert body["guard_scope"] == "pre-paste" and "warning" not in body
    assert pressed == [("ctrl", "v")]


def test_unguarded_type_cannot_be_missed(hands, typing, monkeypatch):
    monkeypatch.setattr(hands, "_owner_input_snapshot", lambda: None)
    monkeypatch.setattr(hands, "_probe_dwtime", lambda: None)
    body = hands.app.test_client().post(
        "/type", json={"text": "hi", "method": "keys"}, headers=SECRET
    ).get_json()
    assert body["ok"] is True and body["guarded"] is False
    assert body["warning"].startswith("UNGUARDED")


# ----------------------------------------------- #35 "aborted" receipts

@pytest.mark.parametrize("result", [
    {"ok": False, "error_code": "type_aborted", "abort_reason": "owner", "chars_typed": 12},
    {"ok": False, "error_code": "failsafe"},
])
def test_owner_aborts_read_aborted_not_failed(gw, client, monkeypatch, result):
    monkeypatch.setattr(gw, "_forward_act", lambda op, p: (409, dict(result)))
    rid = client.post("/v1/act/queued", json={"op": "type", "text": "x"}, headers=TOKEN).get_json()["id"]
    rec = client.get(f"/v1/act/queued/{rid}", headers=TOKEN).get_json()
    assert rec["state"] == "aborted" and rec["aborted_by"] == "owner"


def test_held_act_the_owner_aborts_settles_aborted(gw, client, monkeypatch):
    monkeypatch.setattr(gw, "_ensure_receipt_thread", lambda: None)
    seq = [(409, dict(YIELD)),
           (409, {"ok": False, "error_code": "type_aborted", "abort_reason": "owner", "chars_typed": 12})]
    monkeypatch.setattr(gw, "_forward_act", lambda op, p: seq.pop(0))
    rid = client.post("/v1/act/queued", json={"op": "type", "text": "x"}, headers=TOKEN).get_json()["id"]
    receipt = gw._receipts[rid]
    out = gw._pump_receipt(receipt, receipt["submitted_at"] + 1)
    assert out["state"] == "aborted" and out["abort_reason"] == "owner"


def test_feed_shows_aborted_distinctly(appmod):
    js = (ROOT / "ui/app.js").read_text(encoding="utf-8")
    assert 'aborted: "chip-info"' in js and "aborted — owner" in js


# ------------------------------------------------- #36 gliding rest hand

def test_glide_moves_one_pixel_at_a_time_and_eases_into_rest(hands):
    steps = hands.motion.plan_glide((100, 100), (103, 98), random.Random(4))
    assert steps[-1][:2] == (103, 98) and steps[-1][2] == 0.0
    prev = (100, 100)
    for x, y, _delay in steps:
        assert abs(x - prev[0]) <= 1 and abs(y - prev[1]) <= 1
        prev = (x, y)
    delays = [d for _x, _y, d in steps[:-1]]
    assert all(d > 0 for d in delays) and sum(delays) <= hands.motion.REST_GLIDE_S[1]
    assert delays[-1] > delays[0]  # decelerates into the rest point
    assert hands.motion.plan_glide((5, 5), (5, 5), random.Random(1)) == []


def test_resting_hand_glides_instead_of_teleporting(hands, monkeypatch):
    tick = {"t": 1_000_000, "last_input": 1_000_000 - 2_000}
    pos, moves = {"p": (500, 500)}, []

    def move_to(x, y, duration=None):
        moves.append((x, y))
        pos["p"] = (int(round(x)), int(round(y)))
        tick["last_input"] = tick["t"]

    monkeypatch.setattr(hands.pyautogui, "position", lambda: pos["p"])
    monkeypatch.setattr(hands.pyautogui, "moveTo", move_to)
    monkeypatch.setattr(hands, "_probe_lock_state",
                        lambda: (False, (tick["t"] - tick["last_input"]) / 1000.0))
    monkeypatch.setattr(hands, "_tick_ms", lambda: tick["t"])
    monkeypatch.setattr(hands, "_LEDGER", hands.motion.SelfInputLedger())
    monkeypatch.setattr(hands, "_INPUT", hands.motion.InputMutex(sleep=lambda s: None))
    clock = {"t": 100.0}
    rest = hands._RestingHand(rng=random.Random(5), clock=lambda: clock["t"])
    rest.set_claim(True, 900, 1, clock["t"])
    while clock["t"] < 110.0:
        rest.step(clock["t"])
        clock["t"] += 0.05
        tick["t"] += 50
    assert len(moves) > 5 and rest.kills == 0
    prev = (500, 500)
    for x, y in moves:  # never more than one pixel per injected move
        assert max(abs(x - prev[0]), abs(y - prev[1])) <= 1
        prev = (x, y)
    assert max(max(abs(x - 500), abs(y - 500)) for x, y in moves) <= 5


# --------------------------------------------- #37 the line is v4 now

def test_build_reports_line_v4(appmod, gw):
    assert appmod.RELAY_LINE == "v4" and gw.RELAY_LINE == "v4"


HISTORICAL_DOCS = {
    "docs/TAKEOVER-BRIEF.md", "docs/SWEEP-LEDGER.md", "docs/R72-FIXROUND.md",
    "docs/IMPL-PACKET.md", "docs/WISHLIST-FULL.md",
}


def test_no_operative_reference_to_the_old_line():
    """Wishlist #37: zero references to the old product line outside the
    dated docs, which carry an explicit historical-record marker."""
    old = "v" + "3"  # spelled out so this file does not match itself
    pattern = re.compile(rf"\b{old}\b|{old}[-r_]", re.IGNORECASE)
    hits = []
    for path in ROOT.rglob("*"):
        parts = set(path.relative_to(ROOT).parts)
        if not path.is_file() or ".git" in parts or "__pycache__" in parts:
            continue
        rel = path.relative_to(ROOT).as_posix()
        if old in path.name.lower():
            hits.append(f"file name: {rel}")
        if rel in HISTORICAL_DOCS or path.suffix.lower() in (".png", ".ico", ".pyc", ".exe"):
            continue
        if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
            hits.append(rel)
    assert hits == []
    for rel in HISTORICAL_DOCS:
        assert "Historical record" in (ROOT / rel).read_text(encoding="utf-8")
