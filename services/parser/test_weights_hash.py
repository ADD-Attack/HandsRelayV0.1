"""Tests for the M4 weights pin and hash verification.

fetch_weights.py pins the Hugging Face revision and verifies the
SHA-256 of icon_detect/model.pt after download, deleting a mismatched
file. The parser also refuses to load a detector whose hash does not
match the pinned value, so a planted model.pt cannot become code
execution through the YOLO pickle load.

    python3 test_weights_hash.py
"""

import hashlib
import importlib.util
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run():
    failures = []

    def check(name, cond):
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            failures.append(name)

    # --- fetch_weights: revision pin + hash verification --------------
    calls = {}

    def fake_download(repo_id=None, revision=None, local_dir=None, allow_patterns=None):
        calls.update(
            repo_id=repo_id, revision=revision, local_dir=local_dir,
            allow_patterns=allow_patterns,
        )
        det = os.path.join(local_dir, "icon_detect", "model.pt")
        os.makedirs(os.path.dirname(det), exist_ok=True)
        with open(det, "wb") as fh:
            fh.write(calls["content"])

    fake_hub = types.ModuleType("huggingface_hub")
    fake_hub.snapshot_download = fake_download
    sys.modules["huggingface_hub"] = fake_hub
    fw = _load("fetch_weights_under_test", os.path.join(HERE, "fetch_weights.py"))

    check("pinned revision is a 40-char hex commit",
          len(fw.REVISION) == 40 and all(c in "0123456789abcdef" for c in fw.REVISION))
    check("expected detector hash is a 64-char hex string",
          len(fw.EXPECTED_SHA256["icon_detect/model.pt"]) == 64)

    old_argv = sys.argv
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # Good download: content hashes to the expected value.
            calls["content"] = b"good-weights"
            fw.EXPECTED_SHA256 = {
                "icon_detect/model.pt": hashlib.sha256(b"good-weights").hexdigest()
            }
            sys.argv = ["fetch_weights.py", tmp]
            check("matching hash: main() returns 0",
                  fw.main() == 0)
            check("matching hash: snapshot_download got the pinned revision",
                  calls["revision"] == fw.REVISION)
            check("matching hash: file kept",
                  os.path.isfile(os.path.join(tmp, "icon_detect", "model.pt")))

            # Bad download: mismatched hash is deleted and main() fails.
            calls["content"] = b"planted-weights"
            sys.argv = ["fetch_weights.py", tmp]
            check("mismatched hash: main() returns 1",
                  fw.main() == 1)
            check("mismatched hash: file deleted",
                  not os.path.exists(os.path.join(tmp, "icon_detect", "model.pt")))
    finally:
        sys.argv = old_argv

    # --- parser: load-time hash check ---------------------------------
    parser = _load("parser_under_test", os.path.join(HERE, "app.py"))
    with tempfile.TemporaryDirectory() as tmp:
        parser.MODELS_DIR = tmp
        det_dir = os.path.join(tmp, "icon_detect")
        os.makedirs(det_dir, exist_ok=True)
        det = os.path.join(det_dir, "model.pt")

        def _reset():
            parser._detector = None
            parser._detector_state = "not_tried"
            parser._detector_tried_at = 0.0

        with open(det, "wb") as fh:
            fh.write(b"planted-weights")
        _reset()
        check("planted model.pt: _get_detector() refuses to load",
              parser._get_detector() is None)
        check("planted model.pt: state names the SHA-256 mismatch",
              "SHA-256 mismatch" in parser._detector_state)

        with open(det, "wb") as fh:
            fh.write(b"good-weights")
        parser.EXPECTED_DETECTOR_SHA256 = hashlib.sha256(b"good-weights").hexdigest()
        _reset()
        # Hash passes; ultralytics is not installed here, so the load
        # stops at deps_missing — which proves the hash gate passed.
        check("matching hash: load proceeds past the hash gate",
              parser._get_detector() is None
              and parser._detector_state == "deps_missing")

    print("FAILED: " + ", ".join(failures) if failures else "ALL PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
