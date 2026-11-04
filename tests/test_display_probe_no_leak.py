"""Regression: the Windows display probe must not leak ctypes types.

_probe_display_raw() used to define its GUID struct inside the function
and pass it to ctypes.POINTER(). POINTER() keeps every pointer type it
creates in a process-global cache (CPython <= 3.13) and that entry pins
the struct class, so every GET /state (every gateway /v1/info refresh,
~every 5 s in the packaged app) leaked ~6 KB for the life of the process.

Off Windows the probe returns before touching ctypes, which is why no
existing test could see it: here os.name is forced to "nt" and the Win32
DLLs are faked so the real Windows branch runs. Every fake call reports
failure, so nothing is ever dereferenced.

    python -m pytest -q tests/test_display_probe_no_leak.py
"""
import ctypes
import gc
import importlib.util
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def hands():
    path = ROOT / "hands/hands_relay_hands.py"
    spec = importlib.util.spec_from_file_location("display_probe_leak_hands", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_windll():
    def fail(*_args):
        return 0  # Win32 BOOL failure

    return types.SimpleNamespace(
        user32=types.SimpleNamespace(SystemParametersInfoW=fail),
        kernel32=types.SimpleNamespace(GetSystemPowerStatus=fail, LocalFree=fail),
        powrprof=types.SimpleNamespace(
            PowerGetActiveScheme=lambda *_a: 1,  # non-zero = error, no LocalFree path
            PowerReadACValueIndex=lambda *_a: 1,
            PowerReadDCValueIndex=lambda *_a: 1,
        ),
    )


def _live_structure_types():
    gc.collect()
    return sum(
        1 for o in gc.get_objects()
        if isinstance(o, type) and issubclass(o, ctypes.Structure)
    )


def test_display_probe_does_not_accumulate_ctypes_types(hands, monkeypatch):
    monkeypatch.setattr(hands.ctypes, "windll", _fake_windll(), raising=False)
    monkeypatch.setattr(hands.os, "name", "nt")
    assert hands._probe_display_raw() == {
        "screensaver": None, "display_off_after_seconds": None}
    cache = getattr(ctypes, "_pointer_type_cache", None)
    cache_before = len(cache) if isinstance(cache, dict) else None
    types_before = _live_structure_types()
    for _ in range(500):
        hands._probe_display_raw()
    # Before the fix: +500 pinned struct classes and +500 cache entries.
    assert _live_structure_types() - types_before < 5
    if cache_before is not None:
        assert len(cache) == cache_before
