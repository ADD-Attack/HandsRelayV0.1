"""B17: contact can stay fresh while observation is an error or stale."""
import importlib.util
import io
from pathlib import Path

from PIL import Image


def png(color=(255, 0, 0)):
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), color).save(buf, format="PNG")
    return buf.getvalue()


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def time(self):
        return self.value

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def load_gateway(monkeypatch, clock):
    path = Path(__file__).resolve().parents[1] / "services/gateway/app.py"
    spec = importlib.util.spec_from_file_location("b17_gateway", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.TOKEN = "b17-token"
    module.HANDS_SECRET = "b17-secret"
    monkeypatch.setattr(module, "time", clock)
    module._reset_coordination_state()
    return module


TOKEN = {"X-Relay-Token": "b17-token"}


def watcher(module, holder="eyes"):
    rows = [row for row in module._current_watchers() if row["holder"] == holder]
    assert rows
    return rows[0]


def test_registration_is_contact_not_a_successful_observation(monkeypatch):
    clock = Clock()
    module = load_gateway(monkeypatch, clock)
    response = module.app.test_client().post("/v1/watch", json={"holder": "eyes"}, headers=TOKEN)
    row = response.get_json()["watcher"]
    assert row["last_seen"] == 1000.0
    assert row["last_success_at"] is None
    assert row["last_fetch_ok"] is None
    assert row["observation"] == "unknown"
    assert row["process_heartbeat_at"] == 1000.0


def test_failure_then_heartbeat_keeps_the_error(monkeypatch):
    clock = Clock()
    module = load_gateway(monkeypatch, clock)
    image = png()
    monkeypatch.setattr(module, "_fetch_screenshot", lambda **kw: (image, "image/png", None))
    client = module.app.test_client()
    assert client.get("/v1/screen?watcher=eyes", headers=TOKEN).status_code == 200
    first = watcher(module)
    clock.value = 1001.0
    monkeypatch.setattr(
        module, "_fetch_screenshot", lambda **kw: (None, None, (module.jsonify({"ok": False}), 502))
    )
    assert client.get("/v1/screen?watcher=eyes", headers=TOKEN).status_code == 502
    failed = watcher(module)
    assert failed["last_seen"] > first["last_seen"]
    assert failed["last_success_at"] == first["last_success_at"]
    assert failed["last_fetch_ok"] is False
    assert failed["observation"] == "error"
    clock.value = 1010.0
    client.post("/v1/watch", json={"holder": "eyes"}, headers=TOKEN)
    beat = watcher(module)
    assert beat["process_heartbeat_at"] == 1010.0
    assert beat["last_success_at"] == first["last_success_at"]
    assert beat["last_fetch_ok"] is False
    assert beat["observation"] == "error"
    assert beat["stale"] is True


def test_same_pixels_advance_observation_but_not_content_change(monkeypatch):
    clock = Clock()
    module = load_gateway(monkeypatch, clock)
    monkeypatch.setattr(module, "_fetch_screenshot", lambda **kw: (png(), "image/png", None))
    client = module.app.test_client()
    assert client.get("/v1/screen?watcher=eyes", headers=TOKEN).status_code == 200
    first = watcher(module)
    clock.value = 1002.0
    assert client.get("/v1/screen?watcher=eyes", headers=TOKEN).status_code == 200
    second = watcher(module)
    assert second["observation_generation"] == first["observation_generation"] + 1
    assert second["content_changed_at"] == first["content_changed_at"]
    assert second["last_success_at"] == 1002.0
    assert second["observation"] == "fresh"


def test_validated_304_counts_as_a_new_observation(monkeypatch):
    clock = Clock()
    module = load_gateway(monkeypatch, clock)
    monkeypatch.setattr(module, "_fetch_screenshot", lambda **kw: (png(), "image/png", None))
    client = module.app.test_client()
    first = client.get("/v1/screen?watcher=eyes", headers=TOKEN)
    clock.value = 1003.0
    again = client.get(
        "/v1/screen?watcher=eyes",
        headers={**TOKEN, "If-None-Match": first.headers["ETag"]},
    )
    assert again.status_code == 304
    row = watcher(module)
    assert row["last_success_at"] == 1003.0
    assert row["observation_generation"] == 2


def test_invalid_capture_does_not_advance_success(monkeypatch):
    clock = Clock()
    module = load_gateway(monkeypatch, clock)
    client = module.app.test_client()
    monkeypatch.setattr(module, "_fetch_screenshot", lambda **kw: (png(), "image/png", None))
    assert client.get("/v1/screen?watcher=eyes", headers=TOKEN).status_code == 200
    previous = watcher(module)["last_success_at"]
    clock.value = 1004.0
    monkeypatch.setattr(module, "_fetch_screenshot", lambda **kw: (b"not-a-png", "image/png", None))
    response = client.get("/v1/screen?watcher=eyes", headers=TOKEN)
    assert response.status_code == 502
    row = watcher(module)
    assert row["last_success_at"] == previous
    assert row["last_fetch_ok"] is False
    assert row["last_error"] == "invalid_capture"


def test_paused_watcher_is_not_fresh(monkeypatch):
    clock = Clock()
    module = load_gateway(monkeypatch, clock)
    monkeypatch.setattr(module, "_fetch_screenshot", lambda **kw: (png(), "image/png", None))
    client = module.app.test_client()
    assert client.get("/v1/screen?watcher=eyes", headers=TOKEN).status_code == 200
    response = client.post("/v1/watch", json={"holder": "eyes", "paused": True}, headers=TOKEN)
    row = response.get_json()["watcher"]
    assert row["observation"] == "paused"
    assert row["last_fetch_ok"] is True
    assert row["last_success_at"] == 1000.0


def test_dashboard_shows_observation_separately_from_contact():
    path = Path(__file__).resolve().parents[1] / "hands_relay_v2_app.py"
    spec = importlib.util.spec_from_file_location("b17_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    info = {
        "hands_reachable": True,
        "parser_reachable": True,
        "pc_state": None,
        "watchers": [{
            "holder": "eyes",
            "observation": "error",
            "stale": True,
            "last_fetch_ok": False,
        }],
        "watcher_timeout_seconds": 30,
        "watcher_stale_seconds": 10,
    }
    model = module.build_dashboard_model(info, None, [], None, {}, 0)
    assert model["watchers"] == ["eyes"]
    assert model["observations"][0]["observation"] == "error"
    assert model["watcher_stale_s"] == 10
    text = module.format_diagnostics(model, "now")
    assert "watching: eyes" in text
    assert "Observation: eyes error" in text
