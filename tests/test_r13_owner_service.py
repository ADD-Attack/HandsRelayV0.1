"""B09 owner observation: recent physical input, degraded coverage, tick rollover."""
import importlib.util
from pathlib import Path
import pytest

@pytest.fixture
def hands(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "b09_hands", Path(__file__).resolve().parents[1] / "hands/hands_relay_hands.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.SHARED_SECRET = "***"
    clock = {"t": 1000.0}
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(module.time, "sleep", lambda dt: clock.__setitem__("t", clock["t"] + dt))
    monkeypatch.setattr(module, "_probe_lock_state", lambda: (False, 30.0))
    monkeypatch.setattr(module, "_probe_locked", lambda: False)
    return module, clock

def test_recent_owner_event_yields_and_quiet_self_span_does_not(hands, monkeypatch):
    module, clock = hands
    counts = {"owner": 0, "injected": 3}
    monkeypatch.setattr(module, "_owner_input_snapshot", lambda: (counts["owner"], counts["injected"]))
    monkeypatch.setattr(module, "_tick_ms", lambda: 5000)
    module._observe_owner_counter()
    module._LEDGER.record(4900, 5100)
    monkeypatch.setattr(module, "_probe_lock_state", lambda: (False, 0.1))
    assert module._yield_if_active() is None
    counts["owner"] = 1
    clock["t"] += 1.0
    with module.app.app_context():
        refused = module._yield_if_active()
    assert refused[1] == 409
    assert refused[0].get_json()["error_code"] == "yield"
    clock["t"] += 10.0
    with module.app.app_context():
        assert module._yield_if_active() is None

def test_degraded_coverage_stops_resting_and_is_visible(hands, monkeypatch):
    module, clock = hands
    moves = []
    monkeypatch.setattr(module.pyautogui, "position", lambda: (10, 10))
    monkeypatch.setattr(module.pyautogui, "moveTo", lambda *a, **k: moves.append(a))
    rest = module._RestingHand(clock=lambda: clock["t"])
    rest.set_claim(True, 30, 1, clock["t"])
    module.mark_owner_coverage_degraded("deaf")
    rest.step(clock["t"])
    rest.step(clock["t"] + 5)
    assert moves == []
    assert rest.status()["active"] is False
    body = module.app.test_client().get("/state", headers={"X-Secret": "***"}).get_json()
    assert body["owner_coverage"] == "degraded"

def test_tick_rollover_and_glide_fault_do_not_keep_provisional_span(hands, monkeypatch):
    module, _clock = hands
    assert module.motion.tick_diff(5, 0xFFFFFFF0) == 21
    module._LEDGER.record(0xFFFFFFF0, 100)
    assert module._LEDGER._is_self(5) is True
    assert module._LEDGER._is_self(4000) is False
    monkeypatch.setattr(module, "_tick_ms", lambda: 1000)
    monkeypatch.setattr(module.pyautogui, "position", lambda: (0, 0))
    def boom(*a, **k):
        raise RuntimeError("mid-glide")
    monkeypatch.setattr(module.pyautogui, "moveTo", boom)
    rest = module._RestingHand()
    with pytest.raises(RuntimeError):
        rest._move((3, 0))
    start, end = module._LEDGER._spans[-1]
    assert (end - start) & 0xFFFFFFFF < 1500
