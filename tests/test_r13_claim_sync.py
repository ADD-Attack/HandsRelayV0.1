"""B13: epoch ordering, single-use challenges, and the two-second permit."""
import importlib.util
import json
from pathlib import Path
import pytest

def load(directory, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "b13_hands", Path(__file__).resolve().parents[1] / "hands/hands_relay_hands.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.SHARED_SECRET = "s"
    module._claim_sync = module.claim_sync.ClaimSync(str(directory))
    clock = {"t": 1000.0}
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(module, "start_resting_hand", lambda: None)
    return module, clock

def post(module, payload):
    return module.app.test_client().post(
        "/resting/claim", headers={"X-Secret": "s"}, json=payload)

def grant(module, epoch, seq, expires=30, held=True, boot=None, challenge=None, claim_id="claim-a"):
    if challenge is None:
        issued = module.app.test_client().get("/resting/challenge", headers={"X-Secret": "s"}).get_json()
        challenge = issued["challenge"]
        boot = issued["boot"]
    return post(module, {
        "held": held, "expires_in": expires, "seq": seq, "epoch": epoch,
        "pairing": "pair-1", "boot": boot, "challenge": challenge, "claim_id": claim_id,
    })

def test_higher_epoch_release_beats_a_delayed_old_epoch(tmp_path, monkeypatch):
    module, clock = load(tmp_path, monkeypatch)
    first = grant(module, 8, 7)
    assert first.status_code == 200 and first.get_json()["applied"] is True
    assert module._resting.claim_held(clock["t"])
    deadline = module._resting.claim_until
    assert deadline == clock["t"] + module.claim_sync.SYNC_PERMIT_S
    release = grant(module, 9, 1, expires=0, held=False, claim_id=None)
    assert release.status_code == 200 and release.get_json()["applied"] is True
    assert module._resting.claim_held(clock["t"]) is False
    delayed = grant(module, 8, 100)
    assert delayed.status_code == 200 and delayed.get_json()["applied"] is False
    assert module._resting.claim_held(clock["t"]) is False

def test_duplicate_grant_does_not_move_the_deadline_and_stale_release_is_ignored(tmp_path, monkeypatch):
    module, clock = load(tmp_path, monkeypatch)
    issued = module.app.test_client().get("/resting/challenge", headers={"X-Secret": "s"}).get_json()
    body = {
        "held": True, "expires_in": 30, "seq": 3, "epoch": 4,
        "pairing": "pair-1", "boot": issued["boot"], "challenge": issued["challenge"],
        "claim_id": "same",
    }
    assert post(module, body).get_json()["applied"] is True
    deadline = module._resting.claim_until
    clock["t"] += 0.2
    replay = post(module, body)
    assert replay.status_code == 200 and replay.get_json()["applied"] is True
    assert module._resting.claim_until == deadline
    stale = grant(module, 4, 2, held=False, expires=0, claim_id="same")
    assert stale.get_json()["applied"] is False
    assert module._resting.claim_until == deadline and module._resting.claim_held(clock["t"])

def test_grant_arriving_after_its_lease_does_not_activate(tmp_path, monkeypatch):
    module, clock = load(tmp_path, monkeypatch)
    issued = module.app.test_client().get("/resting/challenge", headers={"X-Secret": "s"}).get_json()
    clock["t"] += 1.2
    response = post(module, {
        "held": True, "expires_in": 1, "seq": 1, "epoch": 2,
        "pairing": "pair-1", "boot": issued["boot"], "challenge": issued["challenge"],
        "claim_id": "late",
    })
    assert response.status_code == 200 and response.get_json()["applied"] is False
    assert module._resting.claim_held(clock["t"]) is False
    assert module.claim_sync.SYNC_PERMIT_S == 2.0
    assert module.claim_sync.SYNC_REFRESH_S == 0.5

def test_restart_drops_grants_and_an_old_boot_cannot_revive_them(tmp_path, monkeypatch):
    module, clock = load(tmp_path, monkeypatch)
    assert grant(module, 6, 2).get_json()["applied"] is True
    old_boot = module._claim_sync.boot
    module._restart_hands_boot()
    assert module._resting.claim_held(clock["t"]) is False
    assert module._claim_sync.boot != old_boot
    revived = post(module, {
        "held": True, "expires_in": 30, "seq": 99, "epoch": 100,
        "pairing": "pair-1", "boot": old_boot, "challenge": "not-from-this-boot",
        "claim_id": "old",
    })
    assert revived.get_json()["applied"] is False
    assert module._resting.claim_held(clock["t"]) is False
    assert grant(module, 6, 1).get_json()["applied"] is True
    assert module._resting.claim_held(clock["t"]) is True

def test_persist_failure_does_not_enable_motion(tmp_path, monkeypatch):
    module, clock = load(tmp_path, monkeypatch)
    def fail(*_a, **_k):
        raise OSError("disk")
    monkeypatch.setattr(module.claim_sync.os, "replace", fail)
    response = grant(module, 3, 1)
    assert response.get_json()["applied"] is False
    assert module._resting.claim_held(clock["t"]) is False
    assert module._claim_sync.sync_error
    stored = json.loads((Path(tmp_path) / "accepted-epoch.json").read_text()) if (Path(tmp_path) / "accepted-epoch.json").exists() else None
    assert stored is None

def test_legacy_three_field_body_still_applies(tmp_path, monkeypatch):
    module, _clock = load(tmp_path, monkeypatch)
    response = post(module, {"held": True, "expires_in": 60, "seq": 1})
    assert response.get_json()["ok"] is True and response.get_json()["applied"] is True
