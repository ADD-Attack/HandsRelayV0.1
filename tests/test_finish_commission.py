"""Behavior regressions for the v4 finish commission (no live desktop)."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def hands(monkeypatch):
    mod = load('hands/hands_relay_hands.py', 'finish_hands')
    monkeypatch.setattr(mod, '_yield_if_active', lambda: None)
    monkeypatch.setattr(mod, '_probe_locked', lambda: False)
    monkeypatch.setattr(mod, '_foreground_hwnd', lambda: 10)
    monkeypatch.setattr(mod._owner_monitor, 'ensure_started', lambda: None)
    yield mod
    mod.stop_background()


@pytest.mark.parametrize('owner_at,expected_chars', [(0, 0), (1, 130), (None, 130)])
def test_paste_guard_before_and_after_injection(hands, monkeypatch, owner_at, expected_chars):
    samples = []
    calls = []
    clipboard = ['original']
    class Guard:
        mode = 'hook'
        def sample(self):
            index = len(samples)
            samples.append(index)
            return index == owner_at
        def after_write(self):
            pass
    monkeypatch.setattr(hands, '_TypeOwnerGuard', Guard)
    monkeypatch.setattr(hands, '_clipboard_backend', lambda: SimpleNamespace(
        get_text=lambda: clipboard[0], set_text=lambda value: clipboard.__setitem__(0, value)))
    monkeypatch.setattr(hands.pyautogui, 'hotkey', lambda *keys: calls.append(keys))
    with hands.app.test_request_context('/type', method='POST', json={'text': 'x' * 130, 'method': 'auto'}):
        result = hands.type_text()
    response, status = result if isinstance(result, tuple) else (result, 200)
    data = response.get_json()
    assert status == (200 if owner_at is None else 409)
    assert data['method_used'] == 'paste'
    assert data['owner_guard'] == 'hook'
    assert len(calls) == (0 if owner_at == 0 else 1)
    assert clipboard == ['original']
    if owner_at is not None:
        assert data['abort_reason'] == 'owner'
        assert data['chars_typed'] == expected_chars


def test_paste_refuses_changed_foreground(hands, monkeypatch):
    handles = iter([10, 11])
    monkeypatch.setattr(hands, '_foreground_hwnd', lambda: next(handles))
    monkeypatch.setattr(hands, '_clipboard_backend', lambda: SimpleNamespace(get_text=lambda: 'original', set_text=lambda value: None))
    calls = []
    monkeypatch.setattr(hands.pyautogui, 'hotkey', lambda *args: calls.append(args))
    with hands.app.test_request_context('/type', method='POST', json={'text':'x'*130}):
        response, status = hands.type_text()
    assert status == 409
    assert response.get_json()['abort_reason'] == 'foreground'
    assert response.get_json()['chars_typed'] == 0
    assert not calls


@pytest.mark.parametrize('held', [False, True])
def test_owner_aborted_receipt_is_terminal_and_not_replayed(monkeypatch, held):
    gw = load('services/gateway/app.py', 'finish_gateway')
    gw.TOKEN = 'test-token'
    monkeypatch.setattr(gw, '_ensure_receipt_thread', lambda: None)
    calls = []
    abort = {'ok': False, 'error_code': 'type_aborted', 'abort_reason': 'owner', 'chars_typed': 130}
    def forward(op, payload):
        calls.append(op)
        if held and len(calls) == 1:
            return 409, {'ok': False, 'error_code': 'yield', 'error': 'user active'}
        return 409, abort
    monkeypatch.setattr(gw, '_forward_act', forward)
    try:
        response = gw.app.test_client().post('/v1/act/queued', headers={'X-Relay-Token':'test-token'}, json={'op':'type', 'text':'x'*130})
        receipt = gw._receipts[response.get_json()['id']]
        if held:
            gw._pump_receipt(receipt, receipt['submitted_at'] + 1)
        assert receipt['state'] == 'aborted-owner'
        assert receipt['result']['chars_typed'] == 130
        assert receipt['id'] not in gw._pending_payloads
        before = len(calls)
        gw._pump_receipt(receipt, receipt['submitted_at'] + 2)
        assert len(calls) == before
    finally:
        gw.stop_background(timeout=0)


def test_watcher_age_is_bounded_and_filters_malformed_records():
    mod = load('hands_relay_v4_app.py', 'finish_app')
    assert mod._watcher_activity([{'holder':'a','last_seen':90}, {'holder':'b','last_seen':110}, {'holder':'c','last_seen':float('nan')}, None], 100) == [
        {'holder':'a','last_poll_ago_s':10}, {'holder':'b','last_poll_ago_s':0}]


def test_legacy_import_shares_current_state(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    import hands_relay_v2_app as old
    import hands_relay_v4_app as current
    assert old is current


def test_legacy_autostart_migrates_when_toggled(monkeypatch):
    mod = load('hands_relay_v4_app.py', 'finish_autostart')
    values = {mod.LEGACY_AUTOSTART_VALUE_NAME: 'old.exe'}
    def query(key, name):
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], 1
    def delete(key, name):
        values.pop(name, None)
    wr = SimpleNamespace(HKEY_CURRENT_USER=1, REG_SZ=1,
        OpenKey=lambda *args: 1, CreateKey=lambda *args: 1, CloseKey=lambda key: None,
        QueryValueEx=query, DeleteValue=delete,
        SetValueEx=lambda key, name, reserved, kind, value: values.__setitem__(name, value))
    monkeypatch.setattr(mod, '_winreg', lambda: wr)
    assert mod._autostart_enabled()
    assert mod._set_autostart(True)
    assert mod.LEGACY_AUTOSTART_VALUE_NAME not in values
    assert mod.AUTOSTART_VALUE_NAME in values
    assert mod._set_autostart(False)
    assert not values


@pytest.mark.parametrize('kind', ['gateway', 'hands'])
def test_upgrade_reuses_legacy_credential_file(monkeypatch, tmp_path, kind):
    mod = load('hands_relay_v4_app.py', 'finish_credentials')
    path_fn = mod._gateway_token_path if kind == 'gateway' else mod._hands_secret_path
    assert Path(path_fn()).parent.name == '.HandsRelayV0.1'
    saved = 'synthetic-test-value-not-a-real-secret'
    path = tmp_path / ('gateway-token.txt' if kind == 'gateway' else 'hands-secret.txt')
    path.write_text(saved + '\n')
    monkeypatch.setattr(mod, '_gateway_token_path' if kind == 'gateway' else '_hands_secret_path', lambda: str(path))
    loader = mod._load_or_create_gateway_token if kind == 'gateway' else mod._load_or_create_hands_secret
    assert loader() == (saved, 'saved')
    assert path.read_text() == saved + '\n'


def test_paste_lock_after_injection_is_not_reported_as_abort(hands, monkeypatch):
    # The paste is atomic: once Ctrl+V is sent, a later lock does not undo
    # the delivered text, so the reply must not invite a duplicate retry.
    locks = iter([False, True, True])
    monkeypatch.setattr(hands, '_probe_locked', lambda: next(locks, True))
    monkeypatch.setattr(hands, '_clipboard_backend', lambda: SimpleNamespace(get_text=lambda: 'original', set_text=lambda value: None))
    calls = []
    monkeypatch.setattr(hands.pyautogui, 'hotkey', lambda *args: calls.append(args))
    with hands.app.test_request_context('/type', method='POST', json={'text': 'x' * 130}):
        result = hands.type_text()
    response, status = result if isinstance(result, tuple) else (result, 200)
    assert status == 200
    assert response.get_json()['method_used'] == 'paste'
    assert len(calls) == 1
