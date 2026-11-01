"""B11: owner/lock/focus before pointer, chord, focus, and resting units."""
import importlib.util
from pathlib import Path
import pytest

def load():
    spec = importlib.util.spec_from_file_location(
        "b11_hands", Path(__file__).resolve().parents[1] / "hands/hands_relay_hands.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.SHARED_SECRET = "s"
    return module

@pytest.fixture
def hands(monkeypatch):
    module = load()
    monkeypatch.setattr(module, "_probe_lock_state", lambda: (False, 30.0))
    monkeypatch.setattr(module, "_probe_locked", lambda: False)
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    monkeypatch.setattr(module, "_tick_ms", lambda: 1000)
    return module

def post(module, path, payload):
    return module.app.test_client().post("/" + path, headers={"X-Secret": "s"}, json=payload)

def arm_after_init(monkeypatch, hands, counts):
    phase = {"init": False}
    real = hands._TypeOwnerGuard.__init__

    def init(self):
        real(self)
        phase["init"] = True

    monkeypatch.setattr(hands._TypeOwnerGuard, "__init__", init)
    monkeypatch.setattr(hands, "_owner_input_snapshot",
                        lambda: (1 if phase["init"] else counts["owner"], counts["injected"]))

def test_owner_before_first_move_yields(hands, monkeypatch):
    counts = {"owner": 0, "injected": 3}
    moves = []
    arm_after_init(monkeypatch, hands, counts)
    monkeypatch.setattr(hands.pyautogui, "moveTo", lambda *a, **k: moves.append(a))
    response = post(hands, "move", {"x": 5, "y": 6, "human": False})
    assert response.status_code == 409
    assert response.get_json()["error_code"] == "yield"
    assert moves == []

def test_same_tick_during_scroll_chunk_stops_the_next_chunk(hands, monkeypatch):
    counts = {"owner": 0, "injected": 4}
    chunks = []

    def scroll(amount):
        chunks.append(amount)
        counts["injected"] += 1
        counts["owner"] += 1

    monkeypatch.setattr(hands, "_owner_input_snapshot", lambda: (counts["owner"], counts["injected"]))
    monkeypatch.setattr(hands.pyautogui, "scroll", scroll)
    monkeypatch.setattr(hands.motion, "plan_scroll", lambda clicks, rng: [(1, 0.01), (1, 0.01), (1, 0.0)])
    response = post(hands, "scroll", {"clicks": 3, "human": True})
    body = response.get_json()
    assert response.status_code == 409
    assert chunks == [1]
    assert body["error_code"] == "input_aborted" and body["abort_reason"] == "owner"

def test_drag_releases_the_button_when_owner_arrives_while_held(hands, monkeypatch):
    counts = {"owner": 0, "injected": 8}
    events = []

    def move(*_a, **_k):
        events.append("move")

    def down(**_k):
        events.append("down")
        counts["owner"] += 1
        counts["injected"] += 1

    def up(**_k):
        events.append("up")

    monkeypatch.setattr(hands, "_owner_input_snapshot", lambda: (counts["owner"], counts["injected"]))
    monkeypatch.setattr(hands.pyautogui, "moveTo", move)
    monkeypatch.setattr(hands.pyautogui, "mouseDown", down)
    monkeypatch.setattr(hands.pyautogui, "mouseUp", up)
    response = post(hands, "drag", {
        "x1": 0, "y1": 0, "x2": 9, "y2": 9, "human": False, "duration": 0})
    assert response.status_code == 409
    assert response.get_json()["abort_reason"] == "owner"
    assert events == ["move", "down", "up"]

def test_failed_hotkey_releases_every_modifier(hands, monkeypatch):
    ups = []

    def hotkey(*_a, **_k):
        raise RuntimeError("chord failed")

    monkeypatch.setattr(hands.pyautogui, "hotkey", hotkey)
    monkeypatch.setattr(hands.pyautogui, "keyUp", lambda key: ups.append(key))
    response = post(hands, "hotkey", {"keys": ["ctrl", "shift", "l"]})
    assert response.status_code == 500
    assert ups == ["ctrl", "shift", "l"]

def test_break_stops_before_the_next_chord(hands, monkeypatch):
    counts = {"owner": 0, "injected": 2}
    chords = []

    def hotkey(*_a, **_k):
        chords.append(_a)
        counts["owner"] += 1
        counts["injected"] += 1

    monkeypatch.setattr(hands, "_owner_input_snapshot", lambda: (counts["owner"], counts["injected"]))
    monkeypatch.setattr(hands.pyautogui, "hotkey", hotkey)
    monkeypatch.setattr(hands.pyautogui, "keyUp", lambda *_a, **_k: None)
    response = post(hands, "break", {"times": 3})
    assert response.status_code == 409
    assert chords == [("ctrl", "c")]
    assert response.get_json()["abort_reason"] == "owner"

def test_focus_does_not_activate_after_owner_event(hands, monkeypatch):
    counts = {"owner": 0, "injected": 1}
    activated = []
    arm_after_init(monkeypatch, hands, counts)
    monkeypatch.setattr(hands, "_enum_windows", lambda: [{"title": "Kept", "_hwnd": 5}])
    monkeypatch.setattr(hands, "_activate_window", lambda win: activated.append(win) or True)
    response = post(hands, "focus", {"title": "Kept"})
    assert response.status_code == 409
    assert response.get_json()["error_code"] == "yield"
    assert activated == []

def test_resting_lock_stops_the_next_substep(hands, monkeypatch):
    locked = {"v": False}
    moves = []
    pos = {"p": (0, 0)}

    def move(x, y, duration=0):
        moves.append((x, y))
        pos["p"] = (x, y)
        locked["v"] = True

    monkeypatch.setattr(hands, "_probe_locked", lambda: locked["v"])
    monkeypatch.setattr(hands.pyautogui, "position", lambda: pos["p"])
    monkeypatch.setattr(hands.pyautogui, "moveTo", move)
    monkeypatch.setattr(hands.motion, "plan_rest_path",
                        lambda *a, **k: [(1, 0, 0), (2, 0, 0), (3, 0, 0)])
    assert hands._RestingHand()._move((3, 0)) is None
    assert moves == [(1, 0)]
