"""HandsRelay v2 — parser service.

Screen parsing for surfaces with no DOM and no UI Automation tree (games,
canvas, custom-drawn UI): screenshot in, labeled + numbered elements out —
the set-of-marks contract a driving model can click against.

Contract:
  POST /parse {"image_b64": "<png base64>"}
    -> {"ok": true, "elements": [{"id": 0, "label": "...", "bbox": [x0,y0,x1,y1],
        "center": [x, y], "interactable": true}], "width": W, "height": H}
  POST /find {"image_b64": "<png base64>", "text": "...",
              "case_sensitive": false}
    -> {"ok": true, "matches": [<elements whose label contains text>],
        "count": N, "width": W, "height": H}

Model: OmniParser v2 weights (icon detector + optional Florence captioner),
expected under the models directory (PARSER_MODELS_DIR, default /models in
Docker, ./models next to this file when run natively). Fetch them with
fetch_weights.py; install the heavy deps from requirements-model.txt.

Honesty rule: with no detector loaded, /parse returns 501 — never a fake
"ok" with an empty element list, which would read to a driver as a blank
screen.
"""

import base64
import binascii
import hashlib
import hmac
import io
import os
import threading
import time

from flask import Flask, jsonify, request

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16_000_000

# H2: the parser is an internal service, but it used to have no auth of
# its own — anyone who could reach it could run inference on arbitrary
# screenshots. It now requires a shared secret (PARSER_SECRET) on
# /parse and /find, sent by the gateway. /health stays unauthenticated
# (liveness only). If no secret is configured, /parse and /find fail
# closed with 503 instead of serving unauthenticated.
PARSER_SECRET = os.environ.get("PARSER_SECRET", "").strip()


def _parser_auth():
    """(error_response, status) or (None, None) when authorised."""
    if not PARSER_SECRET:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "parser secret not configured — refusing to serve unauthenticated",
                }
            ),
            503,
        )
    provided = request.headers.get("X-Parser-Secret") or ""
    if not hmac.compare_digest(provided, PARSER_SECRET):
        return jsonify({"ok": False, "error": "bad parser secret"}), 401
    return None, None

CONF = float(os.environ.get("PARSER_CONF", "0.25"))


def _default_models_dir():
    if os.path.isdir("/models"):
        return "/models"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")


MODELS_DIR = os.environ.get("PARSER_MODELS_DIR", _default_models_dir())

# M4: expected SHA-256 of icon_detect/model.pt at the pinned revision
# in fetch_weights.py. A .pt file is a pickle payload, so the parser
# refuses to load a detector whose hash does not match — a planted
# model.pt must not become code execution. Other candidate weights
# files (best.pt, a root model.pt) are not hash-checked; they are an
# explicit operator choice, not the fetch path.
EXPECTED_DETECTOR_SHA256 = (
    "dab3d4351ad00b035db829909a4db98354d5a90f6990e4ac00222a9a95d4bf57"
)


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

_state_lock = threading.Lock()
_infer_lock = threading.RLock()
_detector = None
_detector_state = "not_tried"  # -> loaded | weights_missing | deps_missing | load_error: ...
_detector_tried_at = 0.0  # monotonic time of the last load attempt
_captioner = None
_captioner_state = "not_tried"

# Review E8: a failed detector load is retried at most this often (it used
# to be final until restart, even after fetch_weights.py added the weights).
RETRY_SECONDS = float(os.environ.get("PARSER_RETRY_SECONDS", "60"))
_loader_stop = threading.Event()
_loader_thread = None


def _find_detector_weights():
    for cand in (
        os.path.join(MODELS_DIR, "icon_detect", "model.pt"),
        os.path.join(MODELS_DIR, "icon_detect", "best.pt"),
        os.path.join(MODELS_DIR, "model.pt"),
    ):
        if os.path.isfile(cand):
            return cand
    return None


def _load_attempt_due():
    """True when a (re)load may be attempted now (call with _state_lock or
    as a lock-free pre-check). A loaded detector never reloads."""
    if _detector is not None:
        return False
    if _detector_state == "not_tried":
        return True
    return time.monotonic() - _detector_tried_at >= RETRY_SECONDS


def _get_detector():
    global _detector, _detector_state, _detector_tried_at
    if not _load_attempt_due():
        return _detector
    with _state_lock:
        if not _load_attempt_due():
            return _detector
        _detector_tried_at = time.monotonic()
        weights = _find_detector_weights()
        if not weights:
            _detector_state = "weights_missing"
            return None
        # M4: refuse a tampered detector on the standard fetch path.
        if weights.endswith(os.path.join("icon_detect", "model.pt")):
            try:
                if _sha256_file(weights) != EXPECTED_DETECTOR_SHA256:
                    _detector_state = (
                        "load_error: icon_detect/model.pt SHA-256 mismatch "
                        "(refusing to load a possibly planted weights file)"
                    )
                    return None
            except OSError as exc:
                _detector_state = f"load_error: cannot read weights: {exc}"
                return None
        try:
            from ultralytics import YOLO
        except ImportError:
            _detector_state = "deps_missing"
            return None
        try:
            _detector = YOLO(weights)
            _detector_state = "loaded"
        except Exception as exc:  # weights present but unreadable
            _detector_state = f"load_error: {exc}"
        return _detector


def start_background_loader(interval=None):
    """Load the detector in the background, retrying until it loads.

    Review E8: /health is a pure read by design (it must never trigger or
    block on a load), so model_loaded used to stay false until something
    called /parse -- a `parser_model_loaded` wait could not succeed on its
    own. The packaged app and `python app.py` start this loader.
    """
    global _loader_thread
    if _loader_thread is not None and _loader_thread.is_alive():
        return
    _loader_stop.clear()
    period = RETRY_SECONDS if interval is None else float(interval)

    def _run():
        while not _loader_stop.is_set():
            try:
                if _get_detector() is not None:
                    return
            except Exception:
                pass
            _loader_stop.wait(period)

    _loader_thread = threading.Thread(target=_run, name="parser-model-loader", daemon=True)
    _loader_thread.start()


def stop_background():
    """Stop the background loader (review L3: the app calls this when it
    retires the module)."""
    _loader_stop.set()


def _get_captioner():
    """Optional Florence-2 captioner (OmniParser's icon_caption_florence).

    Returns (model, processor) or None. Labels fall back to "icon" without it.
    """
    global _captioner, _captioner_state
    # Same lock as predict/generate so a load cannot race an inference.
    # Remote code from the Florence folder is not enabled. If the folder
    # cannot be loaded by the stock class, the captioner stays unloaded
    # and labels remain "icon".
    with _infer_lock:
        if _captioner is not None or _captioner_state != "not_tried":
            return _captioner
        cap_dir = os.path.join(MODELS_DIR, "icon_caption_florence")
        if not os.path.isdir(cap_dir):
            _captioner_state = "weights_missing"
            return None
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoProcessor
        except ImportError:
            _captioner_state = "deps_missing"
            return None
        try:
            model = AutoModelForCausalLM.from_pretrained(cap_dir).eval()
            processor = AutoProcessor.from_pretrained(cap_dir)
            _captioner = (model, processor)
            _captioner_state = "loaded"
        except Exception as exc:
            _captioner_state = f"load_error: {exc}"
        return _captioner


def _caption(crop):
    cap = _get_captioner()
    if cap is None:
        return None
    model, processor = cap
    try:
        import torch

        task = "<CAPTION>"
        inputs = processor(text=task, images=crop, return_tensors="pt")
        with _infer_lock:
            with torch.no_grad():
                ids = model.generate(
                    input_ids=inputs["input_ids"],
                    pixel_values=inputs["pixel_values"],
                    max_new_tokens=32,
                )
        text = processor.batch_decode(ids, skip_special_tokens=True)[0]
        return text.strip() or None
    except Exception:
        return None


def _model_status():
    # Pure read of the state globals — deliberately NO _get_detector() /
    # _get_captioner() here: those lazy-load model weights under
    # _state_lock, and /health (polled by the gateway every few seconds
    # and by supervisor health checks) must never trigger a load or
    # block behind one. Loading happens on the parse path only.
    return {
        "detector": _detector_state,
        "captioner": _captioner_state,
        "models_dir": MODELS_DIR,
        "loaded": _detector is not None,
    }


@app.get("/health")
def health():
    status = _model_status()
    return jsonify(
        {"ok": True, "service": "parser", "model_loaded": status["loaded"], "model": status}
    )


def _model_not_loaded_response():
    return (
        jsonify(
            {
                "ok": False,
                "error": "parser model not loaded",
                "model": _model_status(),
                "hint": "run fetch_weights.py and install requirements-model.txt",
            }
        ),
        501,
    )


def _run_parse(raw_bytes):
    """Detection + caption core shared by /parse and /find.

    Returns (elements, width, height) on success, or None when the
    image bytes are not a readable image (the caller turns that into a
    400). Callers check the detector first so the 501 honesty rule
    keeps its original precedence over a bad-image 400.
    """
    detector = _get_detector()
    if detector is None:
        return None
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    except Exception:
        return None

    width, height = image.size
    captioner = _get_captioner()
    with _infer_lock:
        results = detector.predict(image, conf=CONF, verbose=False)
        boxes = []
        for r in results:
            if r.boxes is None:
                continue
            for b in r.boxes:
                x0, y0, x1, y1 = (int(v) for v in b.xyxy[0].tolist())
                boxes.append((x0, y0, x1, y1))
        boxes.sort(key=lambda b: (b[1], b[0]))  # reading order: top-to-bottom, left-to-right

        elements = []
        for i, (x0, y0, x1, y1) in enumerate(boxes):
            label = None
            if captioner is not None:
                label = _caption(image.crop((x0, y0, x1, y1)))
            elements.append(
                {
                    "id": i,
                    "label": label or "icon",
                    "label_source": "captioner" if label else "detector",
                    "bbox": [x0, y0, x1, y1],
                    "center": [(x0 + x1) // 2, (y0 + y1) // 2],
                    "interactable": True,
                }
            )
        return elements, width, height


def _find_matches(elements, text, case_sensitive=False):
    """Pure filter: elements whose label contains text."""
    if case_sensitive:
        return [e for e in elements if text in (e.get("label") or "")]
    needle = text.lower()
    return [e for e in elements if needle in (e.get("label") or "").lower()]


def _decode_image_b64(body):
    """(raw_bytes, error_response) — exactly one of the two is None."""
    image_b64 = body.get("image_b64") or ""
    if not image_b64:
        return None, (jsonify({"ok": False, "error": "image_b64 required"}), 400)
    try:
        return base64.b64decode(image_b64, validate=True), None
    except binascii.Error:
        return None, (
            jsonify({"ok": False, "error": "image_b64 is not valid base64"}),
            400,
        )


@app.post("/parse")
def parse():
    err, status = _parser_auth()
    if err is not None:
        return err, status
    body = request.get_json(force=True, silent=True) or {}
    raw, err = _decode_image_b64(body)
    if err is not None:
        return err

    if _get_detector() is None:
        return _model_not_loaded_response()

    result = _run_parse(raw)
    if result is None:
        return jsonify({"ok": False, "error": "image bytes are not a readable image"}), 400
    elements, width, height = result
    return jsonify({"ok": True, "elements": elements, "width": width, "height": height})


@app.post("/find")
def find():
    err, status = _parser_auth()
    if err is not None:
        return err, status
    body = request.get_json(force=True, silent=True) or {}
    text = body.get("text") or ""
    if not text:
        return jsonify({"ok": False, "error": "text required"}), 400
    raw, err = _decode_image_b64(body)
    if err is not None:
        return err

    if _get_detector() is None:
        return _model_not_loaded_response()

    result = _run_parse(raw)
    if result is None:
        return jsonify({"ok": False, "error": "image bytes are not a readable image"}), 400
    elements, width, height = result
    matches = _find_matches(elements, text, bool(body.get("case_sensitive")))
    return jsonify(
        {"ok": True, "matches": matches, "count": len(matches), "width": width, "height": height}
    )


if __name__ == "__main__":
    from waitress import serve

    # Loopback by default: the parser is an internal service, and
    # /parse and /find now require the shared PARSER_SECRET (H2) — a
    # run with no secret configured fails closed with 503 on those
    # routes instead of serving unauthenticated. Docker sets
    # PARSER_HOST=127.0.0.1 (docker-compose.yml) so the gateway
    # container can reach it on the compose network.
    start_background_loader()  # review E8: model_loaded without waiting for a /parse
    serve(app, host=os.environ.get("PARSER_HOST", "127.0.0.1"), port=5011, threads=4)
