"""B03 terminal state, retention, and dashboard model contracts."""
import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
def load(path):
    spec = importlib.util.spec_from_file_location('b03_' + path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

@pytest.mark.parametrize('reason,state', [('owner', 'aborted-owner'), ('locked', 'aborted'), ('foreground_changed', 'aborted')])
def test_abort_polling_list_and_dashboard(monkeypatch, reason, state):
    g = load(ROOT / 'services/gateway/app.py')
    g.TOKEN = 'test-token'
    result = {'ok': False, 'error_code': 'type_aborted', 'aborted': True,
              'abort_reason': reason, 'chars': 40, 'chars_typed': 12}
    monkeypatch.setattr(g, '_forward_act', lambda *a: (409, result))
    c = g.app.test_client()
    headers = {'X-Relay-Token': 'test-token'}
    response = c.post('/v1/act/queued', headers=headers, json={'op': 'type', 'text': 'secret'})
    rid = response.json['id']
    assert c.get('/v1/act/queued/' + rid, headers=headers).json['state'] == state
    assert c.get('/v1/act/queued', headers=headers).json['receipts'][0]['state'] == state
    g._pump_receipt(g._receipts[rid], 100)
    assert rid not in g._pending_payloads
    app = load(ROOT / 'hands_relay_v2_app.py')
    snap = app._snapshot_receipt_rows(g._receipts)
    assert app.receipt_state_counts(snap) == {state: 1}
    assert app.summarize_receipts(snap, 100)[0]['state'] == state
    js = (ROOT / 'ui/app.js').read_text()
    assert (f'"{state}": "chip-bad"' in js or f'{state}: "chip-bad"' in js)


def test_evicted_receipt_is_unavailable_not_unexecuted():
    g = load(ROOT / 'services/gateway/app.py')
    g.TOKEN = 'test-token'
    g.RECEIPT_MAX = 1
    g._store_receipt({'id': 'old', 'state': 'applied'})
    g._store_receipt({'id': 'new', 'state': 'applied'})
    r = g.app.test_client().get('/v1/act/queued/old', headers={'X-Relay-Token': 'test-token'})
    assert r.status_code == 404
    assert r.json['state'] == 'unavailable'
