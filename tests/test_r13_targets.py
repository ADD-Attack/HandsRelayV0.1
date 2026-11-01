"""B08 held-target policy and focus identity."""
import importlib.util
from pathlib import Path
import pytest

def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def test_p06_locked_without_target_is_terminal(monkeypatch):
    g = load("services/gateway/app.py")
    g.TOKEN = "acceptance-token"
    g.HANDS_SECRET = "x"
    monkeypatch.setattr(g, "_ensure_receipt_thread", lambda: None)
    monkeypatch.setattr(g, "_prepare_queued_execution", lambda *a: {"version": 1, "boot": "b", "token": "t"})
    monkeypatch.setattr(g, "_forward_act", lambda *a: (409, {"ok": False, "error_code": "locked"}))
    response = g.app.test_client().post("/v1/act/queued", headers={"X-Relay-Token": "acceptance-token"}, json={"op": "click", "x": 1, "y": 2})
    receipt = g._receipts[response.json["id"]]
    assert receipt["state"] == "failed"
    assert receipt["result"]["error_code"] == "target_unknown"
    assert receipt["id"] not in g._pending_payloads

def test_caller_expectation_survives_locked_hold(monkeypatch):
    g = load("services/gateway/app.py")
    g.TOKEN = "t"
    g.HANDS_SECRET = "x"
    monkeypatch.setattr(g, "_ensure_receipt_thread", lambda: None)
    monkeypatch.setattr(g, "_prepare_queued_execution", lambda *a: {"version": 1, "boot": "b", "token": "t"})
    monkeypatch.setattr(g, "_forward_act", lambda *a: (409, {"ok": False, "error_code": "locked", "foreground_hwnd": 99}))
    response = g.app.test_client().post("/v1/act/queued", headers={"X-Relay-Token": "t"}, json={"op": "click", "x": 1, "y": 2, "expect_focus": {"hwnd": 7, "process_instance": "A"}})
    rid = response.json["id"]
    assert g._receipts[rid]["state"] == "held"
    assert g._pending_payloads[rid]["expect_focus"]["hwnd"] == 7
    assert g._pending_payloads[rid]["expect_focus"]["process_instance"] == "A"

def test_focus_rechecks_selected_window_and_survives_other_foreground(monkeypatch):
    h = load("hands/hands_relay_hands.py")
    h.SHARED_SECRET = "s"
    windows = [{"_hwnd": 5, "title": "Target", "pid": 10, "focused": False}]
    calls = {"n": 0}
    def enum():
        calls["n"] += 1
        # yield probe, selection, then the under-input confirmation.
        return [] if calls["n"] >= 2 else list(windows)
    monkeypatch.setattr(h, "_enum_windows", enum)
    monkeypatch.setattr(h, "_probe_lock_state", lambda: (False, 30))
    monkeypatch.setattr(h, "_activate_window", lambda win: (_ for _ in ()).throw(AssertionError("activated")))
    gone = h.app.test_client().post("/focus", headers={"X-Secret": "s"}, json={"title": "Target"})
    assert gone.status_code == 409
    assert gone.json["error_code"] == "target_unavailable"
