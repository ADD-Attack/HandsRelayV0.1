#!/usr/bin/env python3
"""Fetch the OmniParser v2 weights into the parser's models directory.

Usage:
    pip install huggingface_hub
    python fetch_weights.py [models_dir]

models_dir defaults to PARSER_MODELS_DIR, then /models (Docker volume),
then ./models next to this file (native runs).

Downloads from microsoft/OmniParser-v2.0:
    icon_detect/model.pt        (~250 MB — the icon detector)
    icon_caption_florence/      (~1 GB — optional captioner; labels icons)
"""

import hashlib
import os
import sys

# M4: the weights download is pinned and hash-verified. A YOLO .pt file
# is a pickle payload, so an unpinned download (or a planted model.pt)
# is code execution as the parser user.
# Pinned revision: microsoft/OmniParser-v2.0 main at
# f55d0750e5b94db2125ef0b45b0fa4a85ddc59b4, verified against the
# Hugging Face API on 2026-11-05. The expected SHA-256 below is the
# LFS oid of icon_detect/model.pt at that revision (the hash of the
# actual file bytes, from the repo tree API).
REVISION = "f55d0750e5b94db2125ef0b45b0fa4a85ddc59b4"
EXPECTED_SHA256 = {
    "icon_detect/model.pt": (
        "dab3d4351ad00b035db829909a4db98354d5a90f6990e4ac00222a9a95d4bf57"
    ),
}


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_dir():
    if os.environ.get("PARSER_MODELS_DIR"):
        return os.environ["PARSER_MODELS_DIR"]
    if os.path.isdir("/models"):
        return "/models"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else default_dir()
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("huggingface_hub is not installed. Run: pip install huggingface_hub")
        return 1
    print(f"Downloading OmniParser v2 weights into {target} ...")
    print(f"Pinned revision: {REVISION}")
    snapshot_download(
        repo_id="microsoft/OmniParser-v2.0",
        revision=REVISION,
        local_dir=target,
        # icon_caption_florence is not present in the pinned revision
        # (upstream ships icon_caption/ instead); the pattern is kept
        # so a future pinned revision that restores it still downloads.
        allow_patterns=["icon_detect/*", "icon_caption_florence/*"],
    )
    det = os.path.join(target, "icon_detect", "model.pt")
    if not os.path.isfile(det):
        print("Download finished but icon_detect/model.pt is missing — check the output above.")
        return 1
    # Verify the detector weights before anything loads them: a
    # mismatched file is deleted, not left in place for the parser
    # to pickle-load.
    expected = EXPECTED_SHA256["icon_detect/model.pt"]
    actual = _sha256_file(det)
    if actual != expected:
        print(
            f"ERROR: icon_detect/model.pt SHA-256 mismatch: got {actual}, "
            f"expected {expected} for revision {REVISION}. Deleting it."
        )
        try:
            os.unlink(det)
        except OSError:
            pass
        return 1
    print(f"OK: detector weights at {det} (SHA-256 verified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
