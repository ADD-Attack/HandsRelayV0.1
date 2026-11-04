"""Live Windows soak for the L1 memory-leak fix (review findings L1, T2).

Runs only on the target Windows machine, and only when asked:

    set RELAY_LIVE_SOAK=1
    python -m pytest -q tests/test_windows_live_soak.py

Unlike tests/test_display_probe_no_leak.py (which fakes the DLLs so it
can run anywhere), this calls the REAL Win32 power / screensaver APIs
behind GET /state thousands of times and checks that Python memory stays
flat. Read-only: it changes nothing on the PC. Before the fix the probe
kept ~6 KB per call (12 MB over 2000 calls).
"""
import ctypes
import gc
import importlib.util
import os
import tracemalloc
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "nt" or os.environ.get("RELAY_LIVE_SOAK") != "1",
    reason="live Windows soak (set RELAY_LIVE_SOAK=1 on the PC)",
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def hands():
    spec = importlib.util.spec_from_file_location("live_soak_hands", ROOT / "hands/hands_relay_hands.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _retained_after(fn, calls):
    fn()  # warm-up: one-time caches are not a leak
    gc.collect()
    tracemalloc.start()
    before, _ = tracemalloc.get_traced_memory()
    for _ in range(calls):
        fn()
    gc.collect()
    after, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return after - before


def test_real_display_probe_keeps_memory_flat(hands):
    cache = getattr(ctypes, "_pointer_type_cache", None)
    entries_before = len(cache) if isinstance(cache, dict) else None
    grown = _retained_after(hands._probe_display_raw, 2000)
    assert grown < 200 * 1024, f"display probe retained {grown} bytes over 2000 calls"
    if entries_before is not None:
        assert len(cache) == entries_before


def test_state_route_keeps_memory_flat(hands):
    hands.SHARED_SECRET = "live-soak-secret"
    client = hands.app.test_client()

    def call():
        assert client.get("/state", headers={"X-Secret": "live-soak-secret"}).status_code == 200

    grown = _retained_after(call, 1000)
    assert grown < 512 * 1024, f"/state retained {grown} bytes over 1000 calls"
