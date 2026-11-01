"""B06 route coverage and exception-safe post-acquisition refusal."""
import importlib.util
from pathlib import Path
import threading
import pytest

@pytest.mark.parametrize('path,payload', [
    ('move', {'x': 1, 'y': 2}), ('click', {'x': 1, 'y': 2}),
    ('type', {'text': 'private', 'method': 'keys'}), ('key', {'key': 'a'}),
    ('scroll', {'clicks': 1}), ('hotkey', {'keys': ['ctrl', 'a']}),
    ('break', {}), ('drag', {'x1': 1, 'y1': 2, 'x2': 3, 'y2': 4}),
    ('focus', {'title': 'safe'}), ('clipboard/set', {'text': 'private'}),
])
def test_lock_change_after_acquisition_refuses_every_route(monkeypatch, path, payload):
    spec = importlib.util.spec_from_file_location('b06_hands', Path(__file__).resolve().parents[1] / 'hands/hands_relay_hands.py')
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    h.SHARED_SECRET = 'secret'
    state = {'locked': False}
    monkeypatch.setattr(h, '_probe_lock_state', lambda: (state['locked'], 30))
    original = h._INPUT.acquire
    def acquire(*args):
        result = original(*args)
        state['locked'] = True
        return result
    monkeypatch.setattr(h._INPUT, 'acquire', acquire)
    class Clipboard:
        def set_text(self, text):
            pytest.fail('clipboard mutation')
    monkeypatch.setattr(h, '_clipboard_backend', lambda: Clipboard())
    monkeypatch.setattr(h, '_activate_window', lambda *a: pytest.fail('focus effect'))
    monkeypatch.setattr(h._resting, 'act_begin', lambda: pytest.fail('refusal reset resting ramp'))
    for name in ('moveTo', 'click', 'write', 'press', 'scroll', 'hotkey', 'mouseDown'):
        monkeypatch.setattr(h.pyautogui, name, lambda *a, **k: pytest.fail('input effect'))
    response = h.app.test_client().post('/' + path, headers={'X-Secret': 'secret'}, json=payload)
    assert response.status_code == 409
    assert response.json['error_code'] == 'locked'
    acquired = []
    def probe():
        result = h._INPUT.try_acquire()
        acquired.append(result)
        if result:
            h._INPUT.release('keys')
    worker = threading.Thread(target=probe)
    worker.start()
    worker.join(2)
    assert acquired == [True]
