"""Shared fixtures: load the hands and gateway modules on any OS.

The hands module imports pyautogui and tkinter at import time; neither
works on a headless Linux CI box, so both are replaced with small stubs
that record what the hands asked for. The hands file is copied into a
temp dir first, so its config/log side files never land in the repo, and
HOME points at a temp dir so the per-machine secret file does too.
"""

import importlib.util
import os
import shutil
import sys
import threading
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakePyAutoGUI(types.ModuleType):
    """Records calls; keeps a cursor position like the real thing."""

    KEYBOARD_KEYS = [
        "a", "b", "c", "d", "left", "right", "up", "down", "enter", "tab",
        "esc", "shift", "ctrl", "alt", "win", "winleft", "winright",
        "space", "f4", "pagedown", "pageup",
    ]

    def __init__(self):
        super().__init__("pyautogui")
        self.FAILSAFE = True
        self.PAUSE = 0.0
        self.pos = (100, 100)
        self.calls = []
        self.on_write = None  # hook for tests: called after each write()

    def position(self):
        return self.pos

    def moveTo(self, x, y, duration=0):
        self.calls.append(("moveTo", x, y))
        self.pos = (x, y)

    def click(self, x=None, y=None, button="left"):
        self.calls.append(("click", x, y, button))
        if x is not None:
            self.pos = (x, y)

    def write(self, text, interval=0):
        self.calls.append(("write", text))
        if self.on_write:
            self.on_write(text)

    def press(self, key):
        self.calls.append(("press", key))

    def hotkey(self, *keys):
        self.calls.append(("hotkey",) + tuple(keys))

    def scroll(self, clicks):
        self.calls.append(("scroll", clicks))

    def screenshot(self):
        from PIL import Image

        return Image.new("RGB", (4, 3), (128, 128, 128))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_gui(monkeypatch):
    gui = FakePyAutoGUI()
    monkeypatch.setitem(sys.modules, "pyautogui", gui)
    tk = types.ModuleType("tkinter")
    tk.Tk = object  # only referenced in annotations at import time
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    return gui


@pytest.fixture
def hands(fake_gui, tmp_path, monkeypatch):
    """The real hands module, stubbed I/O, fake clock, no watcher threads."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    src = os.path.join(ROOT, "hands", "hands_relay_hands.py")
    dst = tmp_path / "hands_relay_hands.py"
    shutil.copy(src, dst)
    module = _load("hands_under_test", str(dst))
    clock = FakeClock()
    module.TAKEOVER = module.Takeover(
        get_position=fake_gui.position,
        quiet_seconds=10,
        clock=clock,
        on_change=module._on_takeover_change,
    )
    module.TAKEOVER._started = True  # tests drive the detectors by hand
    module.TAKEOVER_ENABLED = True
    module.SHARED_SECRET = "s" * 32
    module.app.config["TESTING"] = True
    module.clock = clock
    yield module
    sys.modules.pop("hands_under_test", None)


@pytest.fixture
def serve_app():
    """Serve a WSGI app on a free loopback port; returns its base URL."""
    from werkzeug.serving import make_server

    servers = []

    def _serve(app):
        server = make_server("127.0.0.1", 0, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}"

    yield _serve
    for server in servers:
        server.shutdown()


@pytest.fixture
def gateway():
    module = _load("gateway_under_test", os.path.join(ROOT, "services", "gateway", "app.py"))
    module.TOKEN = "t" * 32
    module.app.config["TESTING"] = True
    yield module
    sys.modules.pop("gateway_under_test", None)
