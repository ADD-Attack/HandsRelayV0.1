"""Tests for the packaged app's gateway token (wishlist #1, H3).

H3 changed the default: the token is now fresh per launch (origin
"ephemeral", 64 hex chars from secrets.token_hex(32)) and is never
written to disk unless persistence is explicitly opted into with
HANDSRELAY_PERSIST_SECRETS=1 (or persist=True). These checks pin the
behaviour:

  * default: first call returns ("ephemeral") and writes no file;
    a second call returns a DIFFERENT token
  * persist=True: first call creates the file and returns ("created");
    second call returns the SAME token with origin "saved"
  * a pre-existing token file is loaded verbatim (never rotated)
  * a too-short file is treated as absent and replaced
  * a persisted file is created with mode 0o600 on POSIX

Runs on Linux like test_app.py: tkinter / pyautogui / winreg are
stubbed before the app module is loaded from its file path, and HOME
is redirected to a temp dir so the real ~/.HandsRelayV0.1 is untouched.

    python3 test_gateway_token.py
"""

import importlib.util
import os
import stat
import sys
import tempfile
import types

# --- fake tkinter (+ scrolledtext submodule) ------------------------------
_fake_tk = types.ModuleType("tkinter")
_fake_tk.Tk = object
_fake_tk.TclError = Exception
_fake_scrolled = types.ModuleType("tkinter.scrolledtext")
_fake_scrolled.ScrolledText = object
_fake_tk.scrolledtext = _fake_scrolled
sys.modules["tkinter"] = _fake_tk
sys.modules["tkinter.scrolledtext"] = _fake_scrolled

# --- fake pyautogui --------------------------------------------------------
_fake_gui = types.ModuleType("pyautogui")
_fake_gui.FAILSAFE = False
_fake_gui.PAUSE = 0
sys.modules["pyautogui"] = _fake_gui

# --- fake winreg (the app only touches it inside functions) ---------------
sys.modules["winreg"] = types.ModuleType("winreg")

HERE = os.path.dirname(os.path.abspath(__file__))
APP_PATH = os.path.join(HERE, "hands_relay_v2_app.py")

spec = importlib.util.spec_from_file_location("relay_app_under_test", APP_PATH)
appmod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(appmod)


def _is_hex(token, length=64):
    return len(token) == length and all(c in "0123456789abcdef" for c in token)


def run():
    failures = []

    def check(name, cond):
        if not cond:
            failures.append(name)

    old_home = os.environ.get("HOME")
    old_profile = os.environ.get("USERPROFILE")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HOME"] = tmp
        # On Windows os.path.expanduser("~") reads USERPROFILE, not HOME.
        os.environ["USERPROFILE"] = tmp
        try:
            path = appmod._gateway_token_path()
            check("path is under HOME/.HandsRelayV0.1",
                  path == os.path.join(tmp, ".HandsRelayV0.1", "gateway-token.txt"))

            # Default: ephemeral, nothing written, each call fresh.
            token1, origin1 = appmod._load_or_create_gateway_token()
            check("default first call origin is ephemeral", origin1 == "ephemeral")
            check("default first token is 64 hex chars", _is_hex(token1))
            check("default writes no token file", not os.path.exists(path))
            token2, origin2 = appmod._load_or_create_gateway_token()
            check("default second call origin is ephemeral", origin2 == "ephemeral")
            check("default second call returns a different token", token2 != token1)

            # Opt-in persistence: created, then saved with the same token.
            token3, origin3 = appmod._load_or_create_gateway_token(persist=True)
            check("persist first call origin is created", origin3 == "created")
            check("persist first token is 64 hex chars", _is_hex(token3))
            check("persist token file written", os.path.isfile(path))
            if os.name != "nt":
                mode = stat.S_IMODE(os.stat(path).st_mode)
                check("persist token file is mode 0o600", mode == 0o600)
            token4, origin4 = appmod._load_or_create_gateway_token(persist=True)
            check("persist second call origin is saved", origin4 == "saved")
            check("persist second call returns the same token", token4 == token3)

            # A pre-existing token (e.g. minted by the shipped app) is
            # loaded verbatim — the swap must not rotate it.
            known = "fa90cd059d69eed11e2ad26f117ce73f" * 2
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(known + "\n")
            token5, origin5 = appmod._load_or_create_gateway_token(persist=True)
            check("pre-existing token origin is saved", origin5 == "saved")
            check("pre-existing token loaded verbatim", token5 == known)

            # A too-short file is not a token; it is replaced.
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("short\n")
            token6, origin6 = appmod._load_or_create_gateway_token(persist=True)
            check("short file replaced with created", origin6 == "created")
            check("replacement token differs from short file",
                  token6 != "short" and _is_hex(token6))

            # The hands secret follows the same default.
            secret1, sorigin1 = appmod._load_or_create_hands_secret()
            check("hands secret default origin is ephemeral",
                  sorigin1 == "ephemeral")
            check("hands secret file not written by default",
                  not os.path.exists(appmod._hands_secret_path()))
            secret2, sorigin2 = appmod._load_or_create_hands_secret(persist=True)
            check("hands secret persist origin is created",
                  sorigin2 == "created")
            check("hands secret persist file written",
                  os.path.isfile(appmod._hands_secret_path()))
            check("hands secret is a fresh value", secret2 != secret1)
        finally:
            for name, old in (("HOME", old_home), ("USERPROFILE", old_profile)):
                if old is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old

    print("FAILED: " + ", ".join(failures) if failures else "ALL PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
