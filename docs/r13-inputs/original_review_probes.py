"""Read-only adversarial probes; all input and network calls are faked."""
import importlib.util
import json
import os
import random
import sys
import types
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path('/home/john/Work/relay-review-20261022')
os.environ['RELAY_GATEWAY_TOKEN'] = 'review-token'
os.environ['HANDS_SECRET'] = 'review-secret'

def load(rel, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

load('tests/conftest.py', 'review_fakes')
gw = load('services/gateway/app.py', 'review_gw')
hands = load('hands/hands_relay_hands.py', 'review_hands')
app = load('hands_relay_v2_app.py', 'review_app')
AUTH = {'X-Relay-Token': 'review-token'}
HAUTH = {'X-Secret': hands.SHARED_SECRET}
out = {}

def held(rid='held', deadline=100):
    r = dict(id=rid, op='click', state='held', deadline=deadline, updated_at=0)
    gw._reset_coordination_state()
    gw._store_receipt(r, {'x': 1, 'y': 2})
    return r

r = held()
sent = []
with patch.object(gw, '_forward_act', lambda op, p: (sent.append(p) or (200, {'ok': True}))):
    gw._pump_receipt(r, 101)
out['expired_act'] = {'dispatches_after_deadline': len(sent), 'state': r['state']}
assert sent and r['state'] == 'applied'

consumed = []
def lost_response(*args, **kw):
    consumed.append(kw['json'])
    raise gw.requests.ReadTimeout('response lost after dispatch')
with patch.object(gw.requests, 'post', lost_response):
    resp = gw.app.test_client().post('/v1/act/queued', headers=AUTH, json={'op': 'click', 'x': 1, 'y': 2})
    receipt = gw._receipts[resp.get_json()['id']]
    out['timeout_after_dispatch'] = {'dispatches': len(consumed), 'http': resp.status_code, 'state': receipt['state']}
    assert consumed and receipt['state'] == 'failed'

class EmptySuccess:
    status_code = 200
    ok = True
    def json(self):
        raise ValueError('empty body')
with patch.object(gw.requests, 'post', lambda *a, **kw: EmptySuccess()):
    resp = gw.app.test_client().post('/v1/act/queued', headers=AUTH, json={'op': 'click', 'x': 1, 'y': 2})
    receipt = gw._receipts[resp.get_json()['id']]
    out['empty_hands_200'] = {'response': resp.get_json(), 'state': receipt['state']}
    assert resp.get_json()['ok'] is True and receipt['state'] == 'applied'

r = held()
partial = {'ok': False, 'error_code': 'type_aborted', 'abort_reason': 'owner', 'chars_typed': 12}
with patch.object(gw, '_forward_act', lambda op, p: (409, partial)):
    gw._pump_receipt(r, 50)
out['held_failure_diagnostics'] = {'state': r['state'], 'result': r['result']}
assert r['result'] is None

with patch.object(gw, '_forward_act', lambda op, p: (200, {'ok': False, 'error': 'operation failed'})):
    resp = gw.app.test_client().post('/v1/act/queued', headers=AUTH, json={'op': 'click'})
    receipt = gw._receipts[resp.get_json()['id']]
    out['http_only_success'] = {'response': resp.get_json(), 'state': receipt['state']}
    assert receipt['state'] == 'applied'

with patch.object(gw, '_forward_act', lambda op, p: (409, {'ok': False, 'error_code': 'locked'})), patch.object(gw, '_ensure_receipt_thread', lambda: None):
    resp = gw.app.test_client().post('/v1/act/queued', headers=AUTH, json={'op': 'click', 'x': 1, 'y': 2})
    receipt = gw._receipts[resp.get_json()['id']]
    out['locked_hold_pin'] = {'pinned': receipt['pinned_foreground'], 'payload': gw._pending_payloads[receipt['id']]}
    assert not receipt['pinned_foreground']

current = {'hwnd': 1, 'idle': 30}
injected = []
original_acquire = hands._INPUT.acquire
def acquire_then_owner(kind, rng):
    value = original_acquire(kind, rng)
    current.update(hwnd=2, idle=0)
    return value
with ExitStack() as stack:
    stack.enter_context(patch.object(hands, '_probe_lock_state', lambda: (False, current['idle'])))
    stack.enter_context(patch.object(hands, '_foreground_window', lambda: {'_hwnd': current['hwnd']}))
    stack.enter_context(patch.object(hands._INPUT, 'acquire', acquire_then_owner))
    stack.enter_context(patch.object(hands.pyautogui, 'moveTo', lambda *a, **k: injected.append(('move', current['hwnd']))))
    stack.enter_context(patch.object(hands.pyautogui, 'click', lambda *a, **k: injected.append(('click', current['hwnd']))))
    resp = hands.app.test_client().post('/click', headers=HAUTH, json={'x': 1, 'y': 2, 'human': False, 'expect_focus': {'hwnd': 1}})
    out['guard_before_mutex'] = {'http': resp.status_code, 'injections': injected, 'owner_idle_at_injection': current['idle']}
    assert resp.status_code == 200 and injected[-1] == ('click', 2)

gw._reset_coordination_state()
with patch.object(gw, '_fetch_screenshot', lambda **kw: (None, None, (gw.jsonify({'ok': False}), 502))):
    resp = gw.app.test_client().get('/v1/screen?watcher=probe', headers=AUTH)
    watchers = gw._current_watchers()
    out['failed_fetch_still_watching'] = {'http': resp.status_code, 'holders': [w['holder'] for w in watchers]}
    assert resp.status_code == 502 and watchers

r = hands._RestingHand(clock=lambda: 100)
r.set_claim(True, 900, 7, 100)
accepted = r.set_claim(False, 0, 1, 101)
out['gateway_restart_sequence'] = {'release_accepted': accepted, 'claim_still_held': r.claim_held(101), 'last_seq': r.claim_seq}
assert not accepted and r.claim_held(101)

claim = {'holder': 'review', 'purpose': 'countdown', 'expires_at': 1000}
first = app.build_dashboard_model(None, claim, [], None, {}, 100)['claim']['expires_in_s']
second = app.build_dashboard_model(None, claim, [], None, {}, 105)['claim']['expires_in_s']
out['countdown_model_refresh'] = {'first': first, 'five_seconds_later': second, 'controller_poll_ms': app.POLL_MS}
assert first == 900 and second == 895

tick = {'now': 100000, 'last': 99000, 'pos': (500, 500), 'owner_key': None}
ledger = hands.motion.SelfInputLedger()
ledger.owner_idle(10, 99000)
r = hands._RestingHand(rng=random.Random(3), clock=lambda: 100)
def fake_move(x, y, **kw):
    tick['pos'] = (x, y)
    tick['last'] = tick['now']
def owner_key_during_sleep(delay):
    tick['now'] += max(1, int(delay * 1000))
    if tick['owner_key'] is None:
        tick['last'] = tick['now']
        tick['owner_key'] = tick['now']
with patch.object(hands, '_LEDGER', ledger), patch.object(hands, '_tick_ms', lambda: tick['now']), patch.object(hands.pyautogui, 'position', lambda: tick['pos']), patch.object(hands.pyautogui, 'moveTo', fake_move), patch.object(hands.time, 'sleep', owner_key_during_sleep):
    landed = r._move((503, 500))
    key_inside_self_span = ledger._is_self(tick['owner_key'])
    out['keyboard_during_rest_glide'] = {'landed': landed, 'owner_key_classified_as_self': key_inside_self_span, 'span': ledger._spans[-1]}
    assert landed == (503, 500) and key_inside_self_span

with patch.object(hands, '_probe_lock_state', lambda: (False, 30)), patch.object(hands.pyautogui, 'write', lambda *a, **kw: None), patch.object(hands._owner_monitor, 'ensure_started', lambda: False):
    resp = hands.app.test_client().post('/type', headers=HAUTH, json={'text': 'x'*130, 'method': 'keys', 'jitter': False})
    out['nonjitter_keys_guard'] = resp.get_json()
    assert resp.get_json()['owner_guard'] is None

from PIL import Image
primary = Image.new('RGB', (100, 100), (255, 0, 0))
def upstream_screenshot(region=None):
    if region is None:
        return primary
    x, y, w, h = region
    return primary.crop((x, y, x+w, y+h))
with patch.object(hands.pyautogui, 'screenshot', upstream_screenshot):
    secondary = hands._grab({'x': 100, 'y': 0, 'w': 100, 'h': 100})
    negative = hands._grab({'x': -100, 'y': 0, 'w': 100, 'h': 100})
    out['secondary_capture_with_upstream_semantics'] = {'right_pixel': secondary.getpixel((50, 50)), 'left_pixel': negative.getpixel((50, 50))}
    assert secondary.getpixel((50, 50)) == (0, 0, 0) and negative.getpixel((50, 50)) == (0, 0, 0)

print(json.dumps(out, indent=2))
