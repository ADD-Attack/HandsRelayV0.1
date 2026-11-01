"""B04 receiver-clock lease identity, adversarial delay and concurrency."""
import importlib.util
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading
import pytest

@pytest.fixture
def hands(monkeypatch):
    spec = importlib.util.spec_from_file_location('b04_hands', Path(__file__).resolve().parents[1] / 'hands/hands_relay_hands.py')
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    h.SHARED_SECRET = 'secret'
    clock = [500.0]
    monkeypatch.setattr(h.time, 'monotonic', lambda: clock[0])
    return h, clock

@pytest.mark.parametrize('ttl', [None, True, -1, 0, 121, float('inf'), float('nan'), '30'])
def test_invalid_lifetime(hands, ttl):
    h, _ = hands
    with pytest.raises(ValueError):
        h._execution_mint('click', {'x': 1}, ttl)
    assert not h._execution_leases


def test_identity_expiry_and_wall_clock_independence(hands, monkeypatch):
    h, clock = hands
    payload = {'text': 'PRIVATE'}
    lease = h._execution_mint('type', payload, 10)
    assert 'PRIVATE' not in repr(lease) and 'digest' not in lease
    monkeypatch.setattr(h.time, 'time', lambda: -1e20)
    clock[0] += 9.9  # network/transit can only reduce remaining time
    assert h._execution_check(lease, 'type', payload) is None
    assert h._execution_check({**lease, 'boot': 'old'}, 'type', payload) == 'lease_wrong_boot'
    assert h._execution_check(lease, 'type', {'text': 'changed'}) == 'lease_operation_changed'
    assert h._execution_check({**lease, 'token': 'unknown'}, 'type', payload) == 'lease_unknown'
    clock[0] = 510
    assert h._execution_check(lease, 'type', payload, start=True) == 'deadline_expired'


def test_start_once_under_concurrency(hands):
    h, clock = hands
    lease = h._execution_mint('click', {'x': 1}, 10)
    barrier = threading.Barrier(2)
    def start():
        barrier.wait()
        return h._execution_check(lease, 'click', {'x': 1}, start=True)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: start(), range(2)))
    assert results.count(None) == 1
    assert results.count('lease_consumed') == 1
    clock[0] += 20
    assert h._execution_check(lease, 'click', {'x': 1}, start=True) == 'lease_consumed'


def test_authenticated_protocol_mint_has_no_effect(hands):
    h, _ = hands
    client = h.app.test_client()
    assert client.get('/execution/capabilities').status_code == 401
    headers = {'X-Secret': 'secret'}
    caps = client.get('/execution/capabilities', headers=headers).json
    assert caps['execution_lease_version'] == 1
    result = client.post('/execution/lease', headers=headers, json={
        'op': 'click', 'payload': {'x': 1}, 'hold_seconds': 10})
    assert result.status_code == 200
    assert result.json['lease']['boot'] == caps['boot']
    assert all(not row['started'] for row in h._execution_leases.values())
