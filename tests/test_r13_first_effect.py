"""B07 expiry at the first side effect, including preparatory waits."""
import importlib.util
from pathlib import Path
import pytest

ROUTES = [('move', {'x': 1, 'y': 2}), ('click', {'x': 1, 'y': 2}),
          ('type', {'text': 'abc', 'method': 'keys'}), ('key', {'key': 'a'}),
          ('scroll', {'clicks': 1}), ('hotkey', {'keys': ['ctrl', 'a']}),
          ('break', {}), ('drag', {'x1': 1, 'y1': 2, 'x2': 3, 'y2': 4}),
          ('focus', {'title': 'safe'}), ('clipboard/set', {'text': 'abc'})]

@pytest.fixture
def rig(monkeypatch):
    spec = importlib.util.spec_from_file_location('b07_hands', Path(__file__).resolve().parents[1] / 'hands/hands_relay_hands.py')
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    h.SHARED_SECRET = 'secret'
    clock, effects = [100.0], []
    monkeypatch.setattr(h.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(h.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(h, '_probe_lock_state', lambda: (False, 30))
    monkeypatch.setattr(h, '_probe_locked', lambda: False)
    monkeypatch.setattr(h, '_enum_windows', lambda: [{'_hwnd': 1, 'title': 'safe', 'focused': True}])
    monkeypatch.setattr(h, '_activate_window', lambda *a: effects.append('focus') or True)
    monkeypatch.setattr(h.pyautogui, 'position', lambda: (0, 0))
    for name in ('moveTo', 'click', 'mouseDown', 'mouseUp', 'write', 'press', 'scroll', 'hotkey'):
        monkeypatch.setattr(h.pyautogui, name, lambda *a, _name=name, **k: effects.append(_name))
    class Clipboard:
        def get_text(self): return 'old'
        def set_text(self, text): effects.append('clipboard')
    monkeypatch.setattr(h, '_clipboard_backend', lambda: Clipboard())
    return h, clock, effects


def post(h, path, payload):
    return h.app.test_client().post('/' + path, headers={'X-Secret': 'secret'}, json=payload)


def leased(h, path, payload):
    op = 'clipboard_set' if path == 'clipboard/set' else path
    return {**payload, '_execution_lease': h._execution_mint(op, payload, 1)}

@pytest.mark.parametrize('path,payload', ROUTES)
def test_expiry_while_waiting_for_input_all_routes(rig, monkeypatch, path, payload):
    h, clock, effects = rig
    body = leased(h, path, payload)
    acquire = h._INPUT.acquire
    def late(*a):
        result = acquire(*a)
        clock[0] = 101  # equality is expired
        return result
    monkeypatch.setattr(h._INPUT, 'acquire', late)
    monkeypatch.setattr(h._resting, 'act_begin', lambda: pytest.fail('refusal reset ramp'))
    response = post(h, path, body)
    assert response.status_code == 409
    assert response.json['error_code'] == 'deadline_expired'
    assert effects == []


def test_attention_expiry_before_movement(rig, monkeypatch):
    h, clock, effects = rig
    body = leased(h, 'click', {'x': 20, 'y': 20, 'human': True})
    monkeypatch.setattr(h.time, 'sleep', lambda _: clock.__setitem__(0, 102))
    response = post(h, 'click', body)
    assert response.json['error_code'] == 'deadline_expired'
    assert effects == []


def test_started_before_deadline_can_finish_and_cannot_replay(rig, monkeypatch):
    h, clock, effects = rig
    body = leased(h, 'click', {'x': 20, 'y': 20, 'human': True, 'attention': False})
    monkeypatch.setattr(h, '_plan_path', lambda *a, **k: [(20, 20, 2)])
    response = post(h, 'click', body)
    assert response.status_code == 200
    assert effects == ['moveTo', 'mouseDown', 'mouseUp']
    assert post(h, 'click', body).json['error_code'] == 'lease_consumed'
    assert effects == ['moveTo', 'mouseDown', 'mouseUp']


def test_clipboard_read_delay_does_not_allow_mutation_or_restore(rig, monkeypatch):
    h, clock, effects = rig
    class SlowClipboard:
        def get_text(self):
            clock[0] = 102
            return 'old'
        def set_text(self, text): effects.append('clipboard')
    monkeypatch.setattr(h, '_clipboard_backend', lambda: SlowClipboard())
    body = leased(h, 'type', {'text': 'abc', 'method': 'paste'})
    response = post(h, 'type', body)
    assert response.json['error_code'] == 'deadline_expired'
    assert effects == []  # no fallback typing, no cleanup mutation


def test_no_effect_context_does_not_reset_resting_state(rig, monkeypatch):
    h, clock, effects = rig
    calls = []
    monkeypatch.setattr(h._resting, 'act_begin', lambda: calls.append('begin'))
    monkeypatch.setattr(h._resting, 'act_end', lambda *a: calls.append('end'))
    with h._input('mouse'):
        pass
    assert calls == []
    with h._input('mouse'):
        h._inject(h.pyautogui.moveTo, 1, 2)
    assert calls == ['begin', 'end']


def test_native_down_failure_still_releases(rig, monkeypatch):
    h, clock, effects = rig
    body = leased(h, 'click', {'x': 20, 'y': 20, 'human': True, 'attention': False})
    monkeypatch.setattr(h, '_plan_path', lambda *a, **k: [(20, 20, 0)])
    def partial_down(*a, **k):
        effects.append('mouseDown')
        raise RuntimeError('native press failed after possible effect')
    monkeypatch.setattr(h.pyautogui, 'mouseDown', partial_down)
    assert post(h, 'click', body).status_code == 500
    assert effects == ['moveTo', 'mouseDown', 'mouseUp']
    assert post(h, 'click', body).json['error_code'] == 'lease_consumed'
