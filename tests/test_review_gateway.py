"""Review fixes, gateway side: L2 (claim queue), L6 (auth/watcher maps),
E2 (held acts never fire after their deadline), E4 (owner-visible
lockouts + alert), E6 (body/op validation), E7 (undecodable frames),
I4 (frame-hash reuse) -- plus the review's test gaps T4 (lockout counting
end to end), T6 (the real retry tick, not a hand-driven pump) and T8
(Pillow missing). Every test loads a fresh gateway module, so no state
leaks between tests.

    python -m pytest -q tests/test_review_gateway.py
"""
import importlib.util
import io
import itertools
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = {"X-Relay-Token": "test-token"}
BAD = {"X-Relay-Token": "not-the-token"}
YIELD = {"ok": False, "error": "user active — control yielded", "error_code": "yield"}
_seq = itertools.count()


class _Clock:
    """Stands in for the gateway's `time` module: one virtual clock."""

    def __init__(self, t=1000.0):
        self.t = float(t)

    def time(self):
        return self.t

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


@pytest.fixture
def gw():
    path = ROOT / "services/gateway/app.py"
    spec = importlib.util.spec_from_file_location(f"review_gateway_{next(_seq)}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.TOKEN = "test-token"
    mod.HANDS_SECRET = "hands-secret"
    mod._post_claim_to_hands = lambda payload: None  # no network from claim pushes
    yield mod
    mod.stop_background(timeout=0)


@pytest.fixture
def client(gw):
    return gw.app.test_client()


def _png(color=(10, 20, 30), size=(40, 30)):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def _claim(client, holder, **extra):
    return client.post("/v1/hands/claim", json={"holder": holder, **extra}, headers=TOKEN)


def _release(client, holder):
    return client.post("/v1/hands/release", json={"holder": holder}, headers=TOKEN)


# --------------------------------------------------------------- L2 queue

def test_lapsed_reservation_drops_the_dead_queue_head(gw, client, monkeypatch):
    clock = _Clock(1000)
    monkeypatch.setattr(gw, "time", clock)
    assert _claim(client, "A").get_json()["granted"] is True
    queued = _claim(client, "B", queue=True).get_json()
    assert queued["queued"] is True and queued["position"] == 1
    clock.t = 1001
    assert _release(client, "A").get_json()["reserved_for"] == "B"
    clock.t = 1010
    assert _claim(client, "C").status_code == 409  # B's grace window
    clock.t = 1040  # B never came back: its window lapsed unused
    assert _claim(client, "C").get_json()["granted"] is True
    status = client.get("/v1/hands/claim", headers=TOKEN).get_json()
    assert status["queue"] == [] and status["reserved_for"] is None
    clock.t = 1041
    # Before the fix B stayed first in line and was re-reserved here,
    # blocking every handoff for 30 s forever.
    assert _release(client, "C").get_json()["reserved_for"] is None
    clock.t = 1042
    assert _claim(client, "D").get_json()["granted"] is True


def test_claim_queue_is_capped_and_requeue_keeps_the_slot(gw, client, monkeypatch):
    monkeypatch.setattr(gw, "CLAIM_QUEUE_MAX", 3)
    assert _claim(client, "owner").get_json()["granted"] is True
    for i in range(3):
        assert _claim(client, f"w{i}", queue=True).get_json()["position"] == i + 1
    full = _claim(client, "w3", queue=True)
    assert full.status_code == 409 and full.get_json()["queue_full"] is True
    assert _claim(client, "w1", queue=True).get_json()["position"] == 2


def test_unrefreshed_queue_entries_expire(gw, client, monkeypatch):
    clock = _Clock(1000)
    monkeypatch.setattr(gw, "time", clock)
    assert _claim(client, "A", ttl_seconds=3600).get_json()["granted"] is True
    _claim(client, "B", queue=True)
    _claim(client, "C", queue=True)
    clock.t = 3000
    _claim(client, "A", ttl_seconds=3600)  # A refreshes its lease
    _claim(client, "C", queue=True)  # C refreshes its queue slot; B does not
    clock.t = 1000 + gw.QUEUE_ENTRY_TTL + 1
    queue = client.get("/v1/hands/claim", headers=TOKEN).get_json()["queue"]
    assert [entry["holder"] for entry in queue] == ["C"]


def test_holder_and_purpose_are_bounded(gw, client):
    body = _claim(client, "h" * 10_000, purpose="p" * 10_000).get_json()
    assert len(body["claim"]["holder"]) == gw.HOLDER_MAX_CHARS
    assert len(body["claim"]["purpose"]) == gw.PURPOSE_MAX_CHARS


# ------------------------------------------------------- L6 maps / watchers

def test_watchers_expire_on_write_and_are_capped(gw, monkeypatch):
    monkeypatch.setattr(gw, "WATCHERS_MAX", 3)
    for i in range(3):
        gw._touch_watcher(f"w{i}", now=1000.0 + i)
    gw._touch_watcher("w3", now=1003.0)  # over the cap: the stalest goes
    assert sorted(gw._watchers) == ["w1", "w2", "w3"]
    gw._touch_watcher("w4", now=5000.0)  # everyone else lapsed long ago
    assert sorted(gw._watchers) == ["w4"]


def test_stale_peers_are_swept(gw, monkeypatch):
    clock = _Clock(5000)
    monkeypatch.setattr(gw, "time", clock)
    monkeypatch.setattr(gw, "PEER_SWEEP_AT", 5)
    for i in range(5):
        with gw.app.test_request_context("/v1/info", environ_base={"REMOTE_ADDR": f"10.0.0.{i}"}):
            assert gw._gate_decision("bad") == ("bad", False)
    clock.t += gw.FAIL_WINDOW + 1
    with gw.app.test_request_context("/v1/info", environ_base={"REMOTE_ADDR": "10.0.0.99"}):
        gw._gate_decision("bad")
    assert set(gw._fail_by_peer) == {"10.0.0.99"}


# ---------------------------------------------- T4 + E4 lockout end to end

def test_lockout_trips_on_the_tenth_failure_logs_and_alerts_once(gw, client, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(gw, "time", clock)
    monkeypatch.setattr(gw, "_reachability", lambda: (True, True, None))
    alerts = []
    monkeypatch.setattr(gw, "ALERT_HOOK", lambda title, message: alerts.append((title, message)))
    for _ in range(gw.MAX_FAILS - 1):
        assert client.get("/v1/info", headers=BAD).status_code == 401
        clock.t += 1
    assert alerts == [] and gw._lockout_by_peer == {}
    assert client.get("/v1/info", headers=BAD).status_code == 401  # the 10th trips it
    assert len(alerts) == 1 and "LOCKOUT" in alerts[0][0]
    assert client.get("/v1/info", headers=BAD).status_code == 429
    assert client.get("/v1/info", headers=TOKEN).status_code == 200  # valid token never locked
    lines = _drain(gw.log_queue)
    assert sum("rejected request" in line for line in lines) == gw.MAX_FAILS
    assert sum(line.startswith("LOCKOUT") for line in lines) == 1
    clock.t += gw.LOCKOUT_SECONDS + 1  # the lockout expires...
    assert client.get("/v1/info", headers=BAD).status_code == 401
    assert gw._lockout_by_peer == {}  # ...and its entry is forgotten (L6)


def test_failures_outside_the_window_do_not_count(gw, client, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(gw, "time", clock)
    for _ in range(gw.MAX_FAILS - 1):
        client.get("/v1/info", headers=BAD)
    clock.t += gw.FAIL_WINDOW + 1
    for _ in range(gw.MAX_FAILS - 1):
        assert client.get("/v1/info", headers=BAD).status_code == 401
    assert gw._lockout_by_peer == {}
    assert len(gw._fail_by_peer["127.0.0.1"]) == gw.MAX_FAILS - 1


def test_standalone_gateway_alerts_via_ntfy_topic(gw, monkeypatch):
    sent = []

    class _InlineThread:
        def __init__(self, target, args=(), daemon=None):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(gw, "threading", types.SimpleNamespace(Thread=_InlineThread))
    monkeypatch.setattr(gw, "NTFY_TOPIC", "topic-x")
    monkeypatch.setattr(gw, "_post_ntfy", lambda title, message: sent.append(title))
    gw._alert("HandsRelay gateway LOCKOUT", "m")
    assert sent == ["HandsRelay gateway LOCKOUT"]


def test_log_queue_is_bounded_and_never_blocks(gw, monkeypatch):
    monkeypatch.setattr(gw, "log_queue", gw._queue_mod.Queue(maxsize=3))
    for i in range(10):
        gw.log_event(f"line {i}")
    assert gw.log_queue.qsize() == 3


# ----------------------------------------------------- E2 + T6 held acts

def test_lapsed_held_act_is_settled_without_forwarding(gw, client, monkeypatch):
    monkeypatch.setattr(gw, "_ensure_receipt_thread", lambda: None)
    monkeypatch.setattr(gw, "_forward_act", lambda op, p: (409, dict(YIELD)))
    rid = client.post(
        "/v1/act/queued", json={"op": "click", "x": 1, "y": 2, "hold_seconds": 5}, headers=TOKEN
    ).get_json()["id"]
    receipt = gw._receipts[rid]

    def forbidden(op, payload):
        raise AssertionError("a held act was forwarded after its deadline")

    monkeypatch.setattr(gw, "_forward_act", forbidden)
    out = gw._pump_receipt(receipt, receipt["deadline"] + 0.001)
    assert out["state"] == "refused-yielded"
    assert rid not in gw._pending_payloads


def test_slow_forward_cannot_make_a_later_receipt_fire_late(gw, client, monkeypatch):
    """The retry loop is serial (it preserves submission order). While a
    long held `type` finally runs, the next receipt's hold lapses: it must
    settle refused-yielded instead of firing a minute late."""
    monkeypatch.setattr(gw, "_ensure_receipt_thread", lambda: None)
    monkeypatch.setattr(gw, "_forward_act", lambda op, p: (409, dict(YIELD)))
    first = client.post(
        "/v1/act/queued", json={"op": "type", "text": "long", "hold_seconds": 120}, headers=TOKEN
    ).get_json()["id"]
    second = client.post(
        "/v1/act/queued", json={"op": "key", "key": "enter", "hold_seconds": 5}, headers=TOKEN
    ).get_json()["id"]
    clock = _Clock(gw._receipts[first]["submitted_at"] + 1)
    forwarded = []

    def forward(op, payload):
        forwarded.append(op)
        clock.t += 60  # the owner went idle; the long type runs for a minute
        return 200, {"ok": True}

    monkeypatch.setattr(gw, "_forward_act", forward)
    gw._receipt_tick(clock.time)
    assert forwarded == ["type"]
    assert gw._receipts[first]["state"] == "applied"
    assert gw._receipts[second]["state"] == "refused-yielded"


def test_stop_background_ends_the_retry_loop_for_good(gw, monkeypatch):
    monkeypatch.setattr(gw, "RECEIPT_RETRY_INTERVAL", 0.01)
    gw._ensure_receipt_thread()
    thread = gw._receipt_thread
    assert thread.is_alive()
    gw.stop_background(timeout=2)
    assert not thread.is_alive()
    gw._ensure_receipt_thread()
    assert gw._receipt_thread is thread  # a retired module never restarts it


# ---------------------------------------------------- E6 body validation

def test_unhashable_op_is_a_400_not_a_500(gw, client):
    for op in (["click"], {"x": 1}):
        r = client.post("/v1/act", json={"op": op}, headers=TOKEN)
        assert r.status_code == 400 and r.get_json()["ok"] is False


def test_non_object_json_bodies_are_rejected_cleanly(gw, client):
    for path in ("/v1/act", "/v1/act/queued", "/v1/hands/claim", "/v1/hands/release",
                 "/v1/watch", "/v1/wait", "/v1/filedrop"):
        r = client.post(path, json=[1, 2, 3], headers=TOKEN)
        assert 400 <= r.status_code < 500, (path, r.status_code)
        assert r.get_json()["ok"] is False


def test_receipts_bound_what_they_keep(gw, client, monkeypatch):
    monkeypatch.setattr(gw, "_ensure_receipt_thread", lambda: None)
    body = {"op": "x" * 100_000, **{f"k{i}": i for i in range(100)}}
    rid = client.post("/v1/act/queued", json=body, headers=TOKEN).get_json()["id"]
    rec = gw._receipts[rid]
    assert len(rec["op"]) <= 65
    assert len(rec["param_keys"]) == gw.PARAM_KEYS_MAX
    assert len(repr(rec)) < 10_000


# ------------------------------------------------- E7 undecodable frames

def test_undecodable_frame_never_becomes_an_html_500(gw, client, monkeypatch):
    monkeypatch.setattr(gw, "_fetch_screenshot", lambda **kw: (b"not a png", "image/png", None))
    r = client.get("/v1/screen", headers=TOKEN)
    assert r.status_code == 200 and r.data == b"not a png" and "ETag" not in r.headers
    r = client.get("/v1/screen/hash", headers=TOKEN)
    assert r.status_code == 502 and r.get_json()["ok"] is False
    assert client.get("/v1/screen/wait?since=x&timeout=0", headers=TOKEN).status_code == 502
    r = client.post("/v1/wait", json={"condition": {"type": "screen_hash_change"}}, headers=TOKEN)
    assert r.status_code == 502 and r.get_json()["ok"] is False


def test_nonfinite_region_is_a_400(gw, client):
    raw = '{"condition": {"type": "screen_region_change", "region": [0, 0, Infinity, 5]}}'
    r = client.post("/v1/wait", data=raw, headers={**TOKEN, "Content-Type": "application/json"})
    assert r.status_code == 400


# --------------------------------------------- I4 frame-hash reuse, T8

def test_identical_frames_are_hashed_once(gw, monkeypatch):
    png = _png()
    calls = []
    real = gw._decode_masked
    monkeypatch.setattr(gw, "_decode_masked", lambda b, m: calls.append(m) or real(b, m))
    assert gw._frame_hash(png, None, 80) == gw._frame_hash(png, None, 80)
    assert len(calls) == 1
    gw._frame_hash(png, None, 0)  # a different mask is a different key
    assert len(calls) == 2
    for i in range(gw._FRAME_HASH_CACHE_MAX + 5):
        gw._frame_hash(_png(color=(i, 0, 0)), None, 80)
    assert len(gw._frame_hash_cache) == gw._FRAME_HASH_CACHE_MAX


def test_without_pillow_hash_is_501_and_screen_has_no_etag(gw, client, monkeypatch):
    png = _png()
    monkeypatch.setattr(gw, "_fetch_screenshot", lambda **kw: (png, "image/png", None))

    def no_pillow(_bytes):
        raise gw._HashUnavailable()

    monkeypatch.setattr(gw, "_open_rgb", no_pillow)
    r = client.get("/v1/screen", headers=TOKEN)
    assert r.status_code == 200 and r.data == png and "ETag" not in r.headers
    assert client.get("/v1/screen/hash", headers=TOKEN).status_code == 501
