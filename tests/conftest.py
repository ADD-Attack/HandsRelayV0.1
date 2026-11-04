"""Operator test harness for the R2 verification run.

The repo's existing hands tests inject fake pyautogui / tkinter modules
into sys.modules before loading the hands module (see
hands/test_actuation.py). tests/test_v3r2_remaining.py loads the hands
and packaged-app modules directly, so it needs the same fakes present
when its module-scoped fixtures execute. On the Windows build host both
modules import for real; in the operator's Linux test venv they do not.
These fakes mirror the established pattern: attribute access returns a
recorder no-op so monkeypatch.setattr(..., raising=True) finds the
attributes it expects to replace.
"""
import inspect
import sys
import types

import pytest
from _pytest.monkeypatch import MonkeyPatch

# pytest's own "no value given" sentinel, whatever this pytest version
# calls it (notset in older releases, NOTSET in newer ones).
_NOTSET = inspect.signature(MonkeyPatch.setattr).parameters["value"].default

# Windows-only names a Linux test host does not have at all. Patching
# these must not fail there; on the Windows build host they exist anyway.
_MAY_BE_ABSENT = frozenset(
    {
        "windll", "WinDLL", "oledll", "OleDLL", "WINFUNCTYPE",
        "FormatError", "GetLastError", "get_last_error", "set_last_error",
    }
)


class _HostAwareMonkeyPatch(MonkeyPatch):
    """setattr is strict (pytest's raising=True) except for the
    Windows-only ctypes names above.

    Review T3: this fixture used to default raising=False for EVERY
    patch, so a renamed or misspelled seam silently patched nothing and
    the test still passed against the real code path. Pass raising=False
    explicitly when a test genuinely creates a new attribute.
    """

    def setattr(self, target, name, value=_NOTSET, raising=None):
        if raising is None:
            raising = not (isinstance(name, str) and name in _MAY_BE_ABSENT)
        return super().setattr(target, name, value, raising=raising)


@pytest.fixture
def monkeypatch():
    mp = _HostAwareMonkeyPatch()
    yield mp
    mp.undo()


def _recorder(*args, **kwargs):
    return None


class _FakeModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        fn = _recorder
        self.__dict__[name] = fn
        return fn


def _install(name):
    if name not in sys.modules:
        mod = _FakeModule(name)
        sys.modules[name] = mod
    return sys.modules[name]


_pag = _install("pyautogui")
_pag.FAILSAFE = False
_pag.PAUSE = 0
if isinstance(_pag, _FakeModule):

    class FailSafeException(Exception):
        """Stands in for pyautogui.FailSafeException (the owner's
        pointer-in-a-corner emergency stop), so the hands module registers
        its real failsafe error handler under test (review E5)."""

    _pag.FailSafeException = FailSafeException
_install("tkinter")
_install("tkinter.messagebox")
