"""B14: one sync worker, same-claim ramp, and permit loss mid-glide."""
import importlib.util
from pathlib import Path

def load_hands(directory, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "b14_hands", Path(__file__).resolve().parents[1] / "hands/hands_relay_hands.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.SHARED_SECRET = "s"
    module._claim_sync = module.claim_sync.ClaimSync(str(directory))
    clock = {"t": 1000.0}
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(module, "start_resting_hand", lambda: None)
    monkeypatch.setattr(module, "_tick_ms", lambda: 1000)
    return module, clock

def post_grant(module, epoch, seq, claim_id, expires=30):
    issued = module.app.test_client().get("/resting/challenge", headers={"X-Secret": "s"}).get_json()
    return module.app.test_client().post("/resting/claim", headers={"X-Secret": "s"}, json={
        "held": True, "expires_in": expires, "seq": seq, "epoch": epoch,
        "pairing": "pair-1", "boot": issued["boot"], "challenge": issued["challenge"],
        "claim_id": claim_id,
    })

def test_same_claim_reconnect_keeps_ramp_and_owner_stop(tmp_path, monkeypatch):
    module, clock = load_hands(tmp_path, monkeypatch)
    assert post_grant(module, 3, 1, "claim-a").get_json()["applied"] is True
    module._resting.last_act_end = 50.0
    module._resting.owner_stopped = True
    clock["t"] = module._resting.claim_until + 1
    assert module._resting.claim_held(clock["t"]) is False
    assert post_grant(module, 3, 2, "claim-a").get_json()["applied"] is True
    assert module._resting.last_act_end == 50.0
    assert module._resting.owner_stopped is True
    assert module._resting.claim_held(clock["t"]) is True

def test_permit_loss_stops_the_next_glide_step(tmp_path, monkeypatch):
    module, clock = load_hands(tmp_path, monkeypatch)
    moves = []
    pos = {"p": (0, 0)}

    def move(x, y, duration=0):
        moves.append((x, y))
        pos["p"] = (x, y)
        rest.claim_until = 0

    monkeypatch.setattr(module.pyautogui, "position", lambda: pos["p"])
    monkeypatch.setattr(module.pyautogui, "moveTo", move)
    monkeypatch.setattr(module.motion, "plan_rest_path", lambda *a, **k: [(1, 0, 0), (2, 0, 0), (3, 0, 0)])
    rest = module._RestingHand(clock=lambda: clock["t"])
    rest.claim_until = clock["t"] + 5
    assert rest._move((3, 0)) is None
    assert moves == [(1, 0)]

def load_gateway():
    spec = importlib.util.spec_from_file_location(
        "b14_gateway", Path(__file__).resolve().parents[1] / "services/gateway/app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class _Now:
    def __init__(self, target, args=(), kwargs=None, daemon=None):
        self.target, self.args = target, args
    def start(self):
        self.target(*self.args)

def test_rejected_release_is_retried_with_a_higher_sequence(monkeypatch):
    gateway = load_gateway()
    monkeypatch.setattr(gateway.threading, "Thread", _Now)
    monkeypatch.setattr(gateway.time, "sleep", lambda _s: None)
    sent = []

    class Response:
        def __init__(self, payload, applied):
            self._body = {
                "ok": True, "applied": applied, "boot": payload.get("boot"),
                "epoch": payload.get("epoch"), "seq": payload.get("seq"),
                "claim_id": payload.get("claim_id"),
            }
        def json(self):
            return self._body

    def post(payload):
        sent.append(payload["seq"])
        return Response(payload, applied=len(sent) > 1)

    monkeypatch.setattr(gateway, "_post_claim_to_hands", post)
    gateway._claim_authority = type("A", (), {"serving": True, "epoch": 4, "pairing": "pair-1"})()
    gateway._push_claim_locked(None, 1000.0)
    assert sent == [1, 2]
    assert gateway._sync_health["ok"] is True
    assert gateway._sync_health["attempts"] == 0
    assert gateway.SYNC_RETRY_MAX_S == 0.5

def test_startup_with_no_claim_sends_a_release(tmp_path, monkeypatch):
    gateway = load_gateway()
    monkeypatch.setattr(gateway.threading, "Thread", _Now)
    sent = []

    class Response:
        def json(self):
            return {"ok": True, "applied": True, "boot": None, "epoch": 1, "seq": 1, "claim_id": None}
    monkeypatch.setattr(gateway, "_post_claim_to_hands", lambda payload: sent.append(payload) or Response())
    gateway.bind_claim_authority(str(tmp_path))
    assert sent and sent[0]["held"] is False and sent[0]["epoch"] == 1
    gateway._claim_authority.release()

def test_dashboard_shows_sync_health():
    spec = importlib.util.spec_from_file_location(
        "b14_app", Path(__file__).resolve().parents[1] / "hands_relay_v2_app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    info = {"hands_reachable": True, "parser_reachable": True, "pc_state": None,
            "watchers": [], "claim_sync": {"ok": False, "error": "ack rejected"}}
    model = module.build_dashboard_model(info, None, [], None, {}, 0)
    assert model["sync"]["ok"] is False
    assert "Claim sync: degraded" in module.format_diagnostics(model, "now")
