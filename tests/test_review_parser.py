"""Review fix E8, parser side: a failed detector load is retried (it used
to be final until restart, even after fetch_weights.py), and a background
loader turns /health's model_loaded true without anyone calling /parse --
so a gateway `parser_model_loaded` wait can actually succeed.

    python -m pytest -q tests/test_review_parser.py
"""
import importlib.util
import itertools
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_seq = itertools.count()


@pytest.fixture
def parser(tmp_path):
    path = ROOT / "services/parser/app.py"
    spec = importlib.util.spec_from_file_location(f"review_parser_{next(_seq)}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.MODELS_DIR = str(tmp_path)
    yield mod
    mod.stop_background()


@pytest.fixture
def fake_ultralytics(monkeypatch):
    class YOLO:
        def __init__(self, weights):
            self.weights = weights

    module = types.ModuleType("ultralytics")
    module.YOLO = YOLO
    monkeypatch.setitem(sys.modules, "ultralytics", module)
    return module


def _add_weights(parser):
    weights = Path(parser.MODELS_DIR) / "icon_detect" / "model.pt"
    weights.parent.mkdir(parents=True, exist_ok=True)
    weights.write_bytes(b"weights")
    return weights


def test_missing_weights_are_rechecked_after_the_retry_window(parser, fake_ultralytics, monkeypatch):
    assert parser._get_detector() is None
    assert parser._detector_state == "weights_missing"
    _add_weights(parser)  # fetch_weights.py ran while the parser was up
    assert parser._get_detector() is None  # inside the retry window: no re-check yet
    monkeypatch.setattr(parser, "RETRY_SECONDS", 0.0)
    assert parser._get_detector() is not None  # used to need a restart
    assert parser._detector_state == "loaded"


def test_background_loader_sets_model_loaded_without_a_parse(parser, fake_ultralytics):
    client = parser.app.test_client()
    assert client.get("/health").get_json()["model_loaded"] is False
    _add_weights(parser)
    parser.start_background_loader(interval=0.01)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not client.get("/health").get_json()["model_loaded"]:
        time.sleep(0.02)
    assert client.get("/health").get_json()["model_loaded"] is True


def test_background_loader_stops(parser):
    parser.start_background_loader(interval=0.01)
    thread = parser._loader_thread
    parser.stop_background()
    thread.join(2)
    assert not thread.is_alive()
