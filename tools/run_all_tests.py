#!/usr/bin/env python3
"""Run every HandsRelay test suite, each in its own process.

Review finding T1. The suites install sys.modules fakes (pyautogui,
tkinter) and must not share an interpreter, and the older suites are
script-style (`run()` plus `check()`, no pytest test functions), which
plain `pytest` silently collects nothing from. This runner handles both:

  * a file with pytest test functions -> python -m pytest -q <file>
  * a script-style file               -> python <file>  (from its folder)

Exit status is non-zero if any suite fails.

    python tools/run_all_tests.py                          # everything
    python tools/run_all_tests.py tests/test_review_app.py # a subset
"""
import argparse
import glob
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATTERNS = (
    "test_*.py",
    "hands/test_*.py",
    "services/gateway/test_*.py",
    "services/parser/test_*.py",
    "tests/test_*.py",
)
_PYTEST_FUNCTION = re.compile(r"^\s*def test_\w+\(", re.MULTILINE)


def discover():
    found = []
    for pattern in PATTERNS:
        found.extend(sorted(glob.glob(os.path.join(ROOT, pattern))))
    return [os.path.relpath(path, ROOT) for path in found]


def is_pytest_file(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        return bool(_PYTEST_FUNCTION.search(fh.read()))


def run_one(path, timeout):
    full = os.path.join(ROOT, path)
    if is_pytest_file(path):
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", path]
        cwd = ROOT
    else:
        cmd = [sys.executable, os.path.basename(full)]
        cwd = os.path.dirname(full)
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        stdout, stderr, ok = proc.stdout, proc.stderr, proc.returncode == 0
    except subprocess.TimeoutExpired:
        stdout, stderr, ok = "", f"TIMEOUT after {timeout:.0f}s", False
    return ok, time.monotonic() - started, stdout, stderr


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run every test suite, one process each.")
    parser.add_argument("files", nargs="*", help="test files, relative to the repo root (default: all)")
    parser.add_argument("--timeout", type=float, default=600, help="seconds per suite (default 600)")
    args = parser.parse_args(argv)
    files = args.files or discover()
    failures = []
    for path in files:
        ok, seconds, stdout, stderr = run_one(path, args.timeout)
        # The verdict ("ALL PASS", pytest's summary) is on stdout; stderr
        # carries log noise from tests that exercise rejected requests.
        lines = [line for line in (stdout or stderr).strip().splitlines() if line.strip()]
        summary = lines[-1][:80] if lines else ""
        print(f"{'PASS' if ok else 'FAIL'}  {path:46s} {seconds:6.1f}s  {summary}", flush=True)
        if not ok:
            failures.append((path, stdout + stderr))
    for path, output in failures:
        print(f"\n===== {path} =====\n{output[-6000:]}")
    print(f"\n{len(files) - len(failures)} of {len(files)} suites passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
