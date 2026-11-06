"""Tests for the tunnel fallback fix (H5).

A Tailscale Serve failure used to silently fall back to a public
Cloudflare quick tunnel. Now a Tailscale failure stops, loudly, and a
public tunnel starts only when HANDS_ENABLE_PUBLIC_TUNNEL is set
explicitly.

Runs anywhere — no Windows, no real tunnel: fake pyautogui / tkinter
modules are injected before the hands module is loaded, and
shutil.which / subprocess are stubbed per case. RelayApp is built
without __init__ (no Tk root needed): _run_tunnel only touches
self.transport / self.tunnel_proc.

    python3 test_tunnel_fallback.py
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
HANDS_PATH = os.path.join(HERE, "hands_relay_hands.py")

fake_pag = types.ModuleType("pyautogui")
fake_pag.FAILSAFE = False
fake_pag.PAUSE = 0
sys.modules["pyautogui"] = fake_pag

fake_tk = types.ModuleType("tkinter")


class _Dummy:
    def __init__(self, *a, **k):
        pass


for _name in ("Tk", "StringVar", "Label", "Entry", "Button", "Text"):
    setattr(fake_tk, _name, _Dummy)
sys.modules["tkinter"] = fake_tk

spec = importlib.util.spec_from_file_location("hands_relay_hands", HANDS_PATH)
hands = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hands)


class _FakeProc:
    def __init__(self):
        self.stdout = []


def _drain_log():
    out = []
    while not hands.log_queue.empty():
        out.append(hands.log_queue.get_nowait())
    return out


def run():
    failures = []

    def check(name, cond):
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            failures.append(name)

    orig_which = shutil.which
    orig_run = subprocess.run
    orig_popen = subprocess.Popen
    old_tunnel = os.environ.pop("HANDS_ENABLE_TUNNEL", None)
    old_public = os.environ.pop("HANDS_ENABLE_PUBLIC_TUNNEL", None)
    try:
        # --- Tailscale fails, no public opt-in: no tunnel at all -----
        popen_calls = []

        def _which_fail(name):
            return "/usr/bin/tailscale" if name == "tailscale" else "/usr/bin/cloudflared"

        shutil.which = _which_fail
        subprocess.run = lambda *a, **k: (_ for _ in ()).throw(Exception("boom"))
        subprocess.Popen = lambda *a, **k: popen_calls.append(a) or _FakeProc()
        _drain_log()
        app = object.__new__(hands.RelayApp)
        app.tunnel_proc = None
        app.transport = None
        app._run_tunnel()
        logged = _drain_log()
        check("Tailscale failure does not start cloudflared",
              popen_calls == [])
        check("Tailscale failure logs that it is NOT falling back",
              any("NOT falling back" in line for line in logged))

        # --- Tailscale missing, public opt-in set: Cloudflare starts -
        shutil.which = lambda name: None if name == "tailscale" else "/usr/bin/cloudflared"
        subprocess.run = orig_run
        os.environ["HANDS_ENABLE_PUBLIC_TUNNEL"] = "1"
        popen_calls.clear()
        _drain_log()
        app._run_tunnel()
        check("explicit public opt-in starts cloudflared",
              len(popen_calls) == 1
              and popen_calls[0][0][0] == "/usr/bin/cloudflared")

        # --- Tailscale missing, no public opt-in: nothing starts -------
        os.environ.pop("HANDS_ENABLE_PUBLIC_TUNNEL", None)
        popen_calls.clear()
        _drain_log()
        app._run_tunnel()
        logged = _drain_log()
        check("no public opt-in starts no tunnel", popen_calls == [])
        check("no public opt-in says so in the log",
              any("not enabled" in line for line in logged))
    finally:
        shutil.which = orig_which
        subprocess.run = orig_run
        subprocess.Popen = orig_popen
        for name, old in (
            ("HANDS_ENABLE_TUNNEL", old_tunnel),
            ("HANDS_ENABLE_PUBLIC_TUNNEL", old_public),
        ):
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old

    print("FAILED: " + ", ".join(failures) if failures else "ALL PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
