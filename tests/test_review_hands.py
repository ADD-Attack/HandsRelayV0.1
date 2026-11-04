"""Review fixes, hands side: E1 (focus + hwnd pin without pywin32),
I2 (cheap foreground lookup, psutil imported once), E5 (validated legacy
routes, structured failsafe), E6 (non-object bodies), L4 (file-drop caps
and purge), L5 (elevation retention, standalone drain), L6 (per-peer auth
state), I5 (hook monitor stop/restart) -- plus T3 (the conftest patches
strictly now) and T8 (the 10-minute file-drop expiry is exercised).

    python -m pytest -q tests/test_review_hands.py
"""
import importlib.util
import itertools
import sys
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SECRET = {"X-Secret": "s3cret-for-tests"}
_seq = itertools.count()


@pytest.fixture
def hands():
    path = ROOT / "hands/hands_relay_hands.py"
    spec = importlib.util.spec_from_file_location(f"review_hands_{next(_seq)}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.SHARED_SECRET = SECRET["X-Secret"]
    mod._probe_lock_state = lambda: (None, None)  # owner idle, desktop unlocked
    yield mod
    mod.stop_background()


@pytest.fixture
def client(hands):
    return hands.app.test_client()


class _FakeUser32:
    def __init__(self, iconic=False, set_ok=True):
        self.calls = []
        self._iconic, self._set_ok = iconic, set_ok

    def IsIconic(self, hwnd):
        self.calls.append(("IsIconic", hwnd))
        return self._iconic

    def ShowWindow(self, hwnd, cmd):
        self.calls.append(("ShowWindow", hwnd, cmd))
        return True

    def SetForegroundWindow(self, hwnd):
        self.calls.append(("SetForegroundWindow", hwnd))
        return 1 if self._set_ok else 0


# --------------------------------------------------------------- T3 harness

def test_conftest_monkeypatch_is_strict_again(hands, monkeypatch):
    with pytest.raises(AttributeError):
        monkeypatch.setattr(hands, "_no_such_seam", 1)
    monkeypatch.setattr(hands.ctypes, "windll", object())  # Windows-only: allowed


# ------------------------------------------------- E1 focus without pywin32

def test_activate_window_uses_ctypes_not_pywin32(hands, monkeypatch):
    monkeypatch.setattr(hands.os, "name", "nt")
    monkeypatch.setitem(sys.modules, "win32gui", None)  # pywin32 absent
    monkeypatch.setitem(sys.modules, "win32con", None)
    user32 = _FakeUser32(iconic=True)
    monkeypatch.setattr(hands, "_user32", lambda: user32)
    assert hands._activate_window({"_hwnd": 77}) is True
    assert user32.calls == [("IsIconic", 77), ("ShowWindow", 77, 9), ("SetForegroundWindow", 77)]
    refused = _FakeUser32(set_ok=False)
    monkeypatch.setattr(hands, "_user32", lambda: refused)
    assert hands._activate_window({"_hwnd": 77}) is False  # foreground lock: never claim ok


def test_pygetwindow_fallback_window_is_reachable(hands, monkeypatch):
    """The fallback used to be dead code: _activate_window imported pywin32
    first, and fallback windows only exist when that import fails."""
    monkeypatch.setattr(hands.os, "name", "nt")
    monkeypatch.setitem(sys.modules, "win32gui", None)
    monkeypatch.setitem(sys.modules, "win32con", None)
    activated = []
    win = types.SimpleNamespace(isMinimized=False, activate=lambda: activated.append(True))
    assert hands._activate_window({"_window": win}) is True and activated == [True]


def test_fallback_enumeration_carries_the_hwnd(hands, monkeypatch):
    monkeypatch.setattr(hands.os, "name", "nt")
    monkeypatch.setitem(sys.modules, "win32gui", None)
    monkeypatch.setitem(sys.modules, "win32process", None)
    monkeypatch.setattr(hands, "_monitor_rects",
                        lambda: [{"x": 0, "y": 0, "w": 1920, "h": 1080, "primary": True}])
    pgw = types.SimpleNamespace(title="Chess", left=0, top=0, width=800, height=600,
                                visible=True, isActive=True, _hWnd=4242)
    monkeypatch.setattr(hands.pyautogui, "getAllWindows", lambda: [pgw])
    [win] = hands._enum_windows()
    assert win["_hwnd"] == 4242 and win["focused"] is True
    monkeypatch.setattr(hands, "_foreground_window_native", lambda: None)
    assert hands._foreground_hwnd() == 4242  # the held-act pin now works without pywin32


def test_foreground_lookup_prefers_the_native_single_window_path(hands, monkeypatch):
    monkeypatch.setattr(hands, "_foreground_window_native",
                        lambda: {"title": "Chess", "process": "chrome.exe", "_hwnd": 77, "focused": True})

    def no_scan():
        raise AssertionError("enumerated every window to find the focused one")

    monkeypatch.setattr(hands, "_enum_windows", no_scan)
    assert hands._foreground_hwnd() == 77
    assert hands._focus_matches({"hwnd": 77}, hands._foreground_window()) is True


def test_psutil_import_is_attempted_once(hands, monkeypatch):
    monkeypatch.setattr(hands, "_PSUTIL", None)
    monkeypatch.setitem(sys.modules, "psutil", None)  # import psutil -> ImportError
    assert hands._psutil() is None and hands._PSUTIL is False
    monkeypatch.setitem(sys.modules, "psutil", types.ModuleType("psutil"))
    assert hands._psutil() is None  # the failure is cached, not retried per window


# ----------------------------------------- E5 legacy routes + failsafe, E6

@pytest.mark.parametrize(
    "path, body",
    [
        ("/move", {}),
        ("/move", {"x": "left", "y": 1}),
        ("/click", {"x": 1}),
        ("/click", {"x": 1, "y": 2, "button": "sideways"}),
        ("/key", {}),
        ("/key", {"key": ["ctrl", "c"]}),
        ("/scroll", {"clicks": "lots"}),
    ],
)
def test_legacy_routes_answer_json_400_for_bad_input(client, path, body):
    r = client.post(path, json=body, headers=SECRET)
    assert r.status_code == 400, r.data
    assert r.get_json()["ok"] is False


def test_failsafe_is_a_structured_non_yield_409(hands, client, monkeypatch):
    def corner(*args, **kwargs):
        raise hands.pyautogui.FailSafeException("pointer in a corner")

    monkeypatch.setattr(hands.pyautogui, "moveTo", corner)
    r = client.post("/move", json={"x": 1, "y": 2, "human": False}, headers=SECRET)
    body = r.get_json()
    assert r.status_code == 409 and body["error_code"] == "failsafe"
    # The gateway must not mistake it for a yield (which it would hold and retry).
    assert "yield" not in body["error"].lower() and "user active" not in body["error"].lower()
    assert any("FAILSAFE" in line for line in _drain(hands.log_queue))


def test_non_object_bodies_are_400_not_500(hands, client, monkeypatch):
    monkeypatch.setitem(hands._cfg, "elevate_allowlist", {"np": {"exe": "notepad.exe"}})
    assert client.post("/hotkey", json=["ctrl", "c"], headers=SECRET).status_code == 400
    assert client.post("/elevate", json={"name": ["np"]}, headers=SECRET).status_code == 404


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


# --------------------------------------------------- L4 + T8 file-drop

def _begin(client, size, name="f.bin", folder=None):
    return client.post(
        "/filedrop/begin",
        json={"folder": folder, "filename": name, "size": size, "sha256": "0" * 64},
        headers=SECRET,
    )


def test_open_uploads_are_capped(hands, client, monkeypatch, tmp_path):
    monkeypatch.setenv("HANDS_FILEDROP_DIRS", str(tmp_path))
    monkeypatch.setattr(hands, "_FILEDROP_MAX_OPEN", 2)
    assert _begin(client, 10, folder=str(tmp_path)).status_code == 200
    assert _begin(client, 10, folder=str(tmp_path)).status_code == 200
    assert _begin(client, 10, folder=str(tmp_path)).status_code == 503
    monkeypatch.setattr(hands, "_FILEDROP_MAX_OPEN", 10)
    monkeypatch.setattr(hands, "_FILEDROP_MAX_BUFFERED", 25)
    assert _begin(client, 10, folder=str(tmp_path)).status_code == 503  # 20 + 10 > 25


def test_stale_upload_is_purged_by_any_authenticated_request(hands, client, monkeypatch, tmp_path):
    monkeypatch.setenv("HANDS_FILEDROP_DIRS", str(tmp_path))
    upload_id = _begin(client, 10, folder=str(tmp_path)).get_json()["upload_id"]
    hands._uploads[upload_id]["created_at"] -= hands._FILEDROP_TTL + 1
    assert client.get("/state", headers=SECRET).status_code == 200  # not a file-drop call
    assert upload_id not in hands._uploads


# ------------------------------------------------------ L5 + E3 elevation

def test_elevation_records_are_pruned_and_capped(hands, client, monkeypatch):
    monkeypatch.setitem(hands._cfg, "elevate_allowlist", {"np": {"exe": "notepad.exe"}})
    old = time.time() - hands.ELEVATE_RETAIN_SECONDS - 10
    past_ttl = time.time() - hands.ELEVATE_TTL_SECONDS - 10
    hands._elevations["settled"] = {"id": "settled", "name": "np", "state": "denied", "created_at": old}
    hands._elevations["stale"] = {"id": "stale", "name": "np", "state": "pending", "created_at": past_ttl}
    client.post("/elevate", json={"name": "np"}, headers=SECRET)
    assert "settled" not in hands._elevations  # settled + older than retention
    assert hands._elevations["stale"]["state"] == "expired"  # kept an hour, no longer resolvable
    monkeypatch.setattr(hands, "ELEVATE_MAX", 3)
    for _ in range(6):
        assert client.post("/elevate", json={"name": "np"}, headers=SECRET).status_code == 200
    assert len(hands._elevations) <= 3


def test_expire_stale_elevations_settles_unpolled_requests(hands):
    hands._elevations["e1"] = {"id": "e1", "name": "np", "state": "pending",
                               "created_at": time.time() - hands.ELEVATE_TTL_SECONDS - 1}
    hands._elevations["e2"] = {"id": "e2", "name": "np", "state": "pending", "created_at": time.time()}
    hands.expire_stale_elevations()
    assert hands._elevations["e1"]["state"] == "expired"
    assert hands._elevations["e2"]["state"] == "pending"


def test_standalone_window_denies_elevation_requests(hands, client, monkeypatch):
    monkeypatch.setitem(hands._cfg, "elevate_allowlist", {"np": {"exe": "notepad.exe"}})
    eid = client.post("/elevate", json={"name": "np"}, headers=SECRET).get_json()["id"]
    window = object.__new__(hands.RelayApp)  # no Tk: only what _poll touches
    lines = []
    window._append_log = lines.append
    window.stopping = True
    hands.RelayApp._poll(window)
    assert hands._elevations[eid]["state"] == "denied"
    assert any("denied" in line for line in lines)


# --------------------------------------------------------- L6 auth state

def test_expired_lockout_is_forgotten(hands, client, monkeypatch):
    hands._lockout_by_peer["127.0.0.1"] = time.monotonic() - 1  # lapsed
    assert client.get("/state", headers={"X-Secret": "wrong"}).status_code == 401
    assert "127.0.0.1" not in hands._lockout_by_peer


# --------------------------------------------------------- I5 hook monitor

def test_stopped_monitor_never_starts(hands, monkeypatch):
    monkeypatch.setattr(hands.os, "name", "nt")
    monitor = hands._OwnerInputMonitor()
    monitor.stop()
    assert monitor.ensure_started(wait=0) is False and monitor._thread is None


def test_dead_hook_thread_restarts_after_backoff(hands, monkeypatch):
    monkeypatch.setattr(hands.os, "name", "nt")
    monitor = hands._OwnerInputMonitor()
    runs = []
    monitor._run = lambda: runs.append(threading.current_thread().name)
    monitor.ensure_started(wait=0)
    monitor._thread.join(1)
    first = monitor._thread
    monitor.ensure_started(wait=0)  # dead, but inside the backoff: no restart
    assert monitor._thread is first
    monitor._started_at -= monitor.RESTART_BACKOFF + 1
    monitor.ensure_started(wait=0)
    monitor._thread.join(1)
    assert monitor._thread is not first and len(runs) == 2
