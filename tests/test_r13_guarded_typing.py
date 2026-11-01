"""B10: one key loop, next-unit owner stop, and narrow paste fallback."""
import importlib.util
from pathlib import Path
import pytest

def load():
    spec = importlib.util.spec_from_file_location(
        "b10_hands", Path(__file__).resolve().parents[1] / "hands/hands_relay_hands.py")
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
    return module

def post(module, payload):
    return module.app.test_client().post(
        "/type", headers={"X-Secret": "s"}, json=payload)

def test_owner_before_first_unit_yields_without_a_write(hands, monkeypatch):
    phase = {"init": False}
    written = []
    real_init = hands._TypeOwnerGuard.__init__

    def init(self):
        real_init(self)
        phase["init"] = True

    monkeypatch.setattr(hands._TypeOwnerGuard, "__init__", init)
    monkeypatch.setattr(hands, "_owner_input_snapshot",
                        lambda: (1 if phase["init"] else 0, 5))
    monkeypatch.setattr(hands.pyautogui, "write", lambda *a, **k: written.append(a))
    response = post(hands, {"text": "abcdef", "method": "keys", "jitter": False})
    assert response.status_code == 409
    assert response.get_json()["error_code"] == "yield"
    assert written == []
    assert "chars_typed" not in response.get_json()

def test_same_tick_owner_and_injected_event_stops_the_next_unit(hands, monkeypatch):
    counts = {"owner": 0, "injected": 9}
    written = []

    def snapshot():
        return (counts["owner"], counts["injected"])

    def write(ch, **_k):
        written.append(ch)
        counts["injected"] += 1
        counts["owner"] += 1  # same call, same tick: still the owner's event

    monkeypatch.setattr(hands, "_owner_input_snapshot", snapshot)
    monkeypatch.setattr(hands.pyautogui, "write", write)
    for jitter in (False, True):
        written.clear()
        counts["owner"], counts["injected"] = 0, 9
        response = post(hands, {"text": "abcdef", "method": "keys", "jitter": jitter})
        body = response.get_json()
        assert response.status_code == 409
        assert body["chars_typed"] == 1 and written == ["a"]
        assert body["owner_guard"] == "hook"
        assert body["owner_attribution_limit"] == "non-injected-events-only"

def test_nonjitter_and_jitter_stop_on_the_same_character(hands, monkeypatch):
    counts = {"owner": 0}
    written = []

    injected = {"n": 4}

    def write(ch, **_k):
        written.append(ch)
        injected["n"] += 1
        if len(written) == 3:
            counts["owner"] += 1

    monkeypatch.setattr(hands, "_owner_input_snapshot",
                        lambda: (counts["owner"], injected["n"]))
    monkeypatch.setattr(hands.pyautogui, "write", write)
    seen = []
    for jitter in (False, True):
        written.clear()
        counts["owner"] = 0
        injected["n"] = 4
        response = post(hands, {"text": "123456789", "method": "keys", "jitter": jitter})
        seen.append((response.status_code, response.get_json()["chars_typed"], "".join(written)))
    assert seen == [(409, 3, "123"), (409, 3, "123")]

def test_focus_change_after_a_character_keeps_the_prefix(hands, monkeypatch):
    written = []
    windows = [{"title": "Kept", "focused": True}]
    monkeypatch.setattr(hands, "_enum_windows", lambda: list(windows))
    monkeypatch.setattr(hands, "_owner_input_snapshot", lambda: (0, 1))

    def write(ch, **_k):
        written.append(ch)
        if len(written) == 2:
            windows[0]["focused"] = False

    monkeypatch.setattr(hands.pyautogui, "write", write)
    response = post(hands, {"text": "abcdef", "method": "keys", "expect_focus": "Kept"})
    body = response.get_json()
    assert response.status_code == 409
    assert body["error_code"] == "type_aborted"
    assert body["abort_reason"] == "foreground_changed"
    assert body["chars_typed"] == 2 and "".join(written) == "ab"

def test_untouched_clipboard_failure_falls_back_to_keys_once(hands, monkeypatch):
    written = []

    class Clip:
        def get_text(self):
            return "ORIGINAL"
        def set_text(self, _value):
            raise OSError("OpenClipboard failed")

    monkeypatch.setattr(hands, "_clipboard_backend", lambda: Clip())
    monkeypatch.setattr(hands.pyautogui, "write", lambda ch, **_k: written.append(ch))
    response = post(hands, {"text": "ab", "method": "paste"})
    body = response.get_json()
    assert response.status_code == 200 and body["method_used"] == "keys"
    assert written == ["a", "b"]
    assert "unavailable" in body["note"]

def test_mutation_failure_does_not_type_or_invent_a_count(hands, monkeypatch):
    class Clip:
        def __init__(self):
            self.changed = False
            self.sets = []
        def get_text(self):
            return "ORIGINAL"
        def set_text(self, value):
            self.sets.append(value)
            self.changed = True
            raise OSError("SetClipboardData failed")

    clip = Clip()
    monkeypatch.setattr(hands, "_clipboard_backend", lambda: clip)
    monkeypatch.setattr(hands.pyautogui, "write", lambda *a, **k: pytest.fail("typed after mutation"))
    monkeypatch.setattr(hands.pyautogui, "hotkey", lambda *a, **k: pytest.fail("chord after failed mutation"))
    response = post(hands, {"text": "SECRET", "method": "paste"})
    body = response.get_json()
    assert response.status_code == 502
    assert body["error_code"] == "paste_uncertain" and body["injected"] == "uncertain"
    assert "chars_typed" not in body
    assert clip.sets == ["SECRET"]

def test_owner_before_chord_does_not_paste_or_type(hands, monkeypatch):
    counts = {"owner": 0}
    chord = []

    class Clip:
        def __init__(self):
            self.text = "ORIGINAL"
        def get_text(self):
            return self.text
        def set_text(self, value):
            self.text = value
            counts["owner"] += 1

    clip = Clip()
    monkeypatch.setattr(hands, "_owner_input_snapshot", lambda: (counts["owner"], 2))
    monkeypatch.setattr(hands, "_clipboard_backend", lambda: clip)
    monkeypatch.setattr(hands.pyautogui, "hotkey", lambda *a, **k: chord.append(a))
    monkeypatch.setattr(hands.pyautogui, "write", lambda *a, **k: pytest.fail("typed instead of aborting paste"))
    response = post(hands, {"text": "SECRET", "method": "paste"})
    body = response.get_json()
    assert response.status_code == 409
    assert body["error_code"] == "paste_aborted" and body["injected"] is False
    assert "chars_typed" not in body
    assert chord == []
    assert clip.text == "ORIGINAL"

def test_owner_clipboard_value_is_not_overwritten(hands, monkeypatch):
    class Clip:
        def __init__(self):
            self.sets = []
            self.reads = 0
        def get_text(self):
            self.reads += 1
            return "ORIGINAL" if self.reads == 1 else "OWNER"
        def set_text(self, value):
            self.sets.append(value)

    clip = Clip()
    monkeypatch.setattr(hands, "_clipboard_backend", lambda: clip)
    monkeypatch.setattr(hands.pyautogui, "hotkey", lambda *a, **k: None)
    response = post(hands, {"text": "SECRET", "method": "paste"})
    assert response.status_code == 200
    assert clip.sets == ["SECRET"]

def test_failed_chord_releases_modifiers_and_does_not_type(hands, monkeypatch):
    ups = []

    class Clip:
        def __init__(self):
            self.text = "ORIGINAL"
        def get_text(self):
            return self.text
        def set_text(self, value):
            self.text = value

    clip = Clip()
    monkeypatch.setattr(hands, "_clipboard_backend", lambda: clip)
    def hotkey(*_a, **_k):
        raise RuntimeError("chord failed")
    monkeypatch.setattr(hands.pyautogui, "hotkey", hotkey)
    monkeypatch.setattr(hands.pyautogui, "keyUp", lambda key: ups.append(key))
    monkeypatch.setattr(hands.pyautogui, "write", lambda *a, **k: pytest.fail("duplicate typing"))
    response = post(hands, {"text": "SECRET", "method": "paste"})
    body = response.get_json()
    assert response.status_code == 502 and body["error_code"] == "paste_uncertain"
    assert "chars_typed" not in body
    assert ups == ["ctrl", "v"]
    assert clip.text == "ORIGINAL"
