"""HandsRelay v2 — end-to-end smoke check (WP0).

One command, a pass/fail line per check. Designed for the owner's Windows
PC; runs anywhere Python + the service requirements are installed.

    python tools\\smoke.py                 # start parser + gateway natively
                                           # (loopback only), test against the
                                           # hands at 127.0.0.1:5000
    python tools\\smoke.py --use-running   # test a gateway that is already
                                           # running (e.g. the packaged app);
                                           # needs RELAY_GATEWAY_TOKEN set
    python tools\\smoke.py --mock-hands    # no real hands: start the mock
                                           # (any OS, used in CI/dev)

What it checks:
  1. parser /health and gateway /health answer
  2. gateway refuses a request with no token (401)
  3. gateway /v1/info answers with the token; reports hands/parser reach
  4. if the hands are reachable: /v1/screen returns a PNG
  5. if the hands are reachable and --no-action is not given: one harmless
     action through /v1/act (a lone "shift" key press)

If the hands are not running, checks 4–5 are reported as SKIP with the
reason, never as an obscure failure. Secrets (gateway token, hands
secret) are never printed. Exit code: 0 = every executed check passed,
1 = at least one failed.
"""

import argparse
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time

try:
    import requests
except ImportError:  # pragma: no cover - environment problem, say it plainly
    print("FAIL  python package 'requests' is not installed "
          "(pip install -r services/gateway/requirements.txt)")
    sys.exit(1)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class Report:
    def __init__(self):
        self.rows = []

    def add(self, status, name, detail=""):
        self.rows.append((status, name, detail))
        line = f"{status:<5} {name}"
        if detail:
            line += f" — {detail}"
        print(line, flush=True)

    def passed(self, name, detail=""):
        self.add("PASS", name, detail)

    def failed(self, name, detail=""):
        self.add("FAIL", name, detail)

    def skipped(self, name, detail=""):
        self.add("SKIP", name, detail)

    @property
    def ok(self):
        return all(status != "FAIL" for status, _, _ in self.rows)


def port_answering(port, host="127.0.0.1"):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_health(url, timeout):
    deadline = time.monotonic() + timeout
    last = "no answer"
    while time.monotonic() < deadline:
        try:
            r = requests.get(url, timeout=2)
            if r.status_code == 200:
                return True, r
            last = f"HTTP {r.status_code}"
        except requests.RequestException as exc:
            last = type(exc).__name__
        time.sleep(0.3)
    return False, last


def read_hands_secret(path):
    env = os.environ.get("HANDS_SECRET", "")
    if env:
        return env, "HANDS_SECRET environment value"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
        if value:
            return value, path
    except OSError:
        pass
    return "", None


class Services:
    """Child processes this run started (and must stop)."""

    def __init__(self, logdir):
        self.procs = []
        self.logdir = logdir

    def start(self, name, args, env):
        log_path = os.path.join(self.logdir, f"{name}.log")
        fh = open(log_path, "w", encoding="utf-8")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.Popen(
            [sys.executable] + args,
            cwd=ROOT,
            env=env,
            stdout=fh,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        self.procs.append((name, proc, fh, log_path))
        return proc

    def log_tail(self, name, lines=15):
        for n, _, fh, path in self.procs:
            if n == name:
                fh.flush()
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        return "".join(f.readlines()[-lines:])
                except OSError:
                    return ""
        return ""

    def stop(self):
        for _, proc, fh, _ in self.procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            fh.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--use-running", action="store_true",
                    help="test an already-running gateway (token from RELAY_GATEWAY_TOKEN)")
    ap.add_argument("--mock-hands", action="store_true",
                    help="start the mock hands instead of using the real ones")
    ap.add_argument("--no-action", action="store_true",
                    help="skip the harmless key press")
    ap.add_argument("--hands-url", default="http://127.0.0.1:5000")
    ap.add_argument("--hands-secret-file",
                    default=os.path.join(os.path.expanduser("~"), ".HandsRelayV0.1",
                                         "hands-secret.txt"))
    ap.add_argument("--gateway-port", type=int, default=5010)
    ap.add_argument("--parser-port", type=int, default=5011)
    ap.add_argument("--timeout", type=float, default=30.0,
                    help="seconds to wait for each service to come up")
    args = ap.parse_args(argv)

    report = Report()
    logdir = tempfile.mkdtemp(prefix="muse-smoke-")
    services = Services(logdir)
    gateway = f"http://127.0.0.1:{args.gateway_port}"
    parser = f"http://127.0.0.1:{args.parser_port}"

    try:
        if args.use_running:
            token = os.environ.get("RELAY_GATEWAY_TOKEN", "")
            if not token:
                report.failed("gateway token",
                              "--use-running needs RELAY_GATEWAY_TOKEN set to the running "
                              "gateway's token (copy it from the app window)")
                return finish(report)
            report.passed("gateway token", "taken from RELAY_GATEWAY_TOKEN (not shown)")
        else:
            token = secrets.token_hex(16)
            busy = [p for p in (args.gateway_port, args.parser_port) if port_answering(p)]
            if busy:
                report.failed(
                    "ports free",
                    f"port(s) {', '.join(map(str, busy))} already answering — the packaged "
                    "app or another copy is running. Close it, or rerun with --use-running "
                    "and RELAY_GATEWAY_TOKEN set.",
                )
                return finish(report)

            hands_url = args.hands_url.rstrip("/")
            if args.mock_hands:
                hands_secret = secrets.token_urlsafe(24)
                mock_port = free_port()
                hands_url = f"http://127.0.0.1:{mock_port}"
                services.start(
                    "mock-hands",
                    ["tests/mock_hands.py", "--port", str(mock_port)],
                    dict(os.environ, MOCK_HANDS_SECRET=hands_secret),
                )
                up, _ = wait_health(f"{hands_url}/health", args.timeout)
                if not up:
                    report.failed("mock hands start", services.log_tail("mock-hands"))
                    return finish(report)
                report.passed("mock hands start", f"port {mock_port}")
            else:
                hands_secret, source = read_hands_secret(args.hands_secret_file)
                if hands_secret:
                    report.passed("hands secret", f"read from {source} (value not shown)")
                else:
                    report.skipped(
                        "hands secret",
                        f"none found in HANDS_SECRET or {args.hands_secret_file} — the hands "
                        "app creates it on first run; hand checks will be skipped",
                    )

            env = dict(os.environ)
            env.update(
                PARSER_HOST="127.0.0.1",
                PARSER_PORT=str(args.parser_port),
                GATEWAY_HOST="127.0.0.1",
                GATEWAY_PORT=str(args.gateway_port),
                RELAY_GATEWAY_TOKEN=token,
                HANDS_URL=hands_url,
                HANDS_SECRET=hands_secret,
                PARSER_URL=parser,
            )
            services.start("parser", ["services/parser/app.py"], env)
            services.start("gateway", ["services/gateway/app.py"], env)

        # 1. health
        for name, base, svc in (("parser", parser, "parser"), ("gateway", gateway, "gateway")):
            up, detail = wait_health(f"{base}/health", args.timeout)
            if up:
                extra = ""
                if name == "parser":
                    loaded = detail.json().get("model_loaded")
                    extra = "model loaded" if loaded else "model not loaded (parse returns 501 — expected without weights)"
                report.passed(f"{name} /health", extra)
            else:
                tail = "" if args.use_running else services.log_tail(svc)
                report.failed(f"{name} /health", f"{detail}. {tail.strip()[-400:]}")
        if not report.ok:
            return finish(report)

        # 2. auth is enforced
        r = requests.get(f"{gateway}/v1/info", timeout=5)
        if r.status_code == 401:
            report.passed("gateway rejects missing token", "HTTP 401")
        else:
            report.failed("gateway rejects missing token", f"expected 401, got {r.status_code}")

        # 3. info
        auth = {"X-Relay-Token": token}
        r = requests.get(f"{gateway}/v1/info", headers=auth, timeout=10)
        if r.status_code != 200:
            report.failed("gateway /v1/info", f"HTTP {r.status_code}: {r.text[:200]}")
            return finish(report)
        info = r.json()
        hands_ok = bool(info.get("hands_reachable"))
        detail = (f"hands_reachable={hands_ok}, parser_reachable={info.get('parser_reachable')}")
        if "control" in info:
            detail += f", control={info['control']}"
        report.passed("gateway /v1/info", detail)

        # 4–5. hands
        if not hands_ok:
            why = "hands are not running (no answer at %s)" % info.get("hands_url")
            if not info.get("hands_secret_configured"):
                why += "; also, the gateway has no hands secret configured"
            report.skipped("screenshot via /v1/screen", why)
            report.skipped("harmless action via /v1/act", why)
            print("\nHands not running: start the packaged app (Start) or "
                  "hands/hands_relay_hands.py, then rerun.")
            return finish(report)

        r = requests.get(f"{gateway}/v1/screen", headers=auth, timeout=30)
        if r.status_code == 200 and r.content.startswith(PNG_MAGIC):
            report.passed("screenshot via /v1/screen", f"{len(r.content)} bytes PNG")
        else:
            report.failed("screenshot via /v1/screen", f"HTTP {r.status_code}: {r.text[:200]}")

        if args.no_action:
            report.skipped("harmless action via /v1/act", "--no-action given")
        else:
            r = requests.post(f"{gateway}/v1/act", headers=auth,
                              json={"op": "key", "key": "shift"}, timeout=30)
            if r.status_code == 200 and r.json().get("ok"):
                report.passed("harmless action via /v1/act", "pressed shift")
            elif r.status_code == 409:
                report.skipped("harmless action via /v1/act",
                               "control is yielded to the person at the PC (takeover "
                               "interlock) — leave the mouse/keyboard alone and rerun")
            else:
                report.failed("harmless action via /v1/act", f"HTTP {r.status_code}: {r.text[:200]}")
        return finish(report)
    finally:
        services.stop()


def finish(report):
    passed = sum(1 for s, _, _ in report.rows if s == "PASS")
    failed = sum(1 for s, _, _ in report.rows if s == "FAIL")
    skipped = sum(1 for s, _, _ in report.rows if s == "SKIP")
    verdict = "PASS" if report.ok else "FAIL"
    print(f"\nSMOKE {verdict}: {passed} passed, {failed} failed, {skipped} skipped")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
