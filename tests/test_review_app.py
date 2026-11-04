"""Review fixes, packaged-app side: L3 (a self-heal restart retires the
old server's worker threads and the old module's threads), I3 (one
scheduler thread instead of a thread per after()), E3 (an unpolled,
timed-out elevation request no longer pins its card or blocks self-heal),
E4 (gateway lockouts reach the window log; alerts go through the hands),
E8 (the parser's background loader is started), I1 (gateway bind host) --
and the review's test gap T5: the restart path itself is exercised,
including against a real waitress server.

    python -m pytest -q tests/test_review_app.py
"""
import importlib.util
import itertools
import queue
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_seq = itertools.count()


def _load(rel, tag):
    spec = importlib.util.spec_from_file_location(f"{tag}_{next(_seq)}", ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def appmod():
    return _load("hands_relay_v2_app.py", "review_app")


@pytest.fixture
def hands():
    mod = _load("hands/hands_relay_hands.py", "review_app_hands")
    yield mod
    mod.stop_background()


class _Root:
    def __init__(self):
        self.after_calls = []

    def after(self, ms, fn, *args):
        self.after_calls.append((ms, fn))


@pytest.fixture
def relay(appmod, monkeypatch):
    monkeypatch.setattr(appmod, "_autostart_enabled", lambda: False)
    return appmod.RelayApp(_Root())


# ------------------------------------------------------------ I3 scheduler

def test_scheduler_runs_in_time_order_on_one_thread_and_survives_errors(appmod):
    sched = appmod._Scheduler()
    seen, done = [], threading.Event()

    def boom():
        raise RuntimeError("a bad callback")

    sched.after(40, lambda: seen.append(("b", threading.current_thread().name)))
    sched.after(0, boom)
    sched.after(10, lambda: seen.append(("a", threading.current_thread().name)))
    sched.after(80, done.set)
    assert done.wait(3)
    assert [label for label, _ in seen] == ["a", "b"]
    assert {name for _, name in seen} == {"relay-scheduler"}


def test_scheduler_does_not_start_a_thread_per_call(appmod):
    sched = appmod._Scheduler()
    runners, finished = set(), threading.Event()
    for _ in range(50):
        sched.after(0, lambda: runners.add(threading.current_thread().name))
    sched.after(0, finished.set)
    assert finished.wait(2)
    assert runners == {"relay-scheduler"}  # used to be one new Timer thread per call


# ----------------------------------------------- L3 + T5 the restart path

def test_waitress_close_alone_strands_workers_and_retire_stops_them(appmod, relay):
    import waitress

    def make():
        server = waitress.create_server(appmod.flask.Flask("x"), host="127.0.0.1", port=0, threads=4)
        threading.Thread(target=server.run, daemon=True).start()
        return server

    # The bug: close() leaves all worker threads parked forever.
    leaky = make()
    leaky.close()
    time.sleep(0.2)
    assert len(leaky.task_dispatcher.threads) == 4
    leaky.task_dispatcher.shutdown(timeout=2)  # clean up the demonstration
    # The fix: retiring the service also shuts the dispatcher down.
    server = make()
    relay.servers["parser"] = server
    relay._retire_service("parser")
    assert "parser" not in relay.servers
    assert len(server.task_dispatcher.threads) == 0


def test_retire_service_stops_the_module_threads(appmod, relay):
    calls = []

    class _Dispatcher:
        def shutdown(self, cancel_pending=True, timeout=5):
            calls.append(("shutdown", timeout))

    class _Server:
        task_dispatcher = _Dispatcher()

        def close(self):
            calls.append("close")

    relay.servers["gateway"] = _Server()
    relay._gateway_module = types.SimpleNamespace(stop_background=lambda: calls.append("stop"))
    relay._retire_service("gateway")
    assert calls == ["close", ("shutdown", 2.0), "stop"]


def test_self_heal_retires_before_restarting(appmod, relay, monkeypatch):
    events = []
    relay._health = {"gateway": appmod.ServiceHealth()}
    relay.servers["gateway"] = object()
    monkeypatch.setattr(relay, "_skip_restart", lambda name: False)
    monkeypatch.setattr(relay, "_retire_service", lambda name, timeout=2.0: events.append(("retire", name)))
    monkeypatch.setattr(relay, "_start_one", lambda name: events.append(("start", name)))
    for _ in range(3):
        relay._note_health("gateway", False)
    assert events == [("retire", "gateway"), ("start", "gateway")]


# ------------------------------------------------------- E3 elevation card

def test_unpolled_timed_out_request_closes_its_card_and_unblocks_self_heal(appmod, relay, hands):
    stale = {"id": "e1", "name": "np", "exe": "notepad.exe", "args": "", "state": "pending",
             "created_at": time.time() - hands.ELEVATE_TTL_SECONDS - 1}
    hands._elevations["e1"] = stale
    relay._hands_module = hands
    relay._handle_elevation_request(hands, dict(stale))
    assert relay._elevation_dialog_open is True
    relay._expire_shown_elevations(hands)  # the agent never polled it
    assert relay._pending_elevations == {} and relay._elevation_dialog_open is False
    assert relay._skip_restart("hands") is False  # used to stay True forever


def test_a_fresh_pending_request_still_blocks_self_heal(appmod, relay, hands):
    hands._elevations["e2"] = {"id": "e2", "name": "np", "state": "pending", "created_at": time.time()}
    relay._hands_module = hands
    assert relay._skip_restart("hands") is True


# ---------------------------------------------------------- E4 lockouts

def test_gateway_log_lines_reach_the_window(appmod, relay):
    gateway_log = queue.Queue()
    gateway_log.put("LOCKOUT: 10 bad gateway tokens from 10.0.0.7")
    relay._gateway_module = types.SimpleNamespace(log_queue=gateway_log)
    relay._drain_queue()
    lines = [entry["line"] for entry in relay.view()["log"]]
    assert any("gateway: LOCKOUT" in line for line in lines)


def test_gateway_alerts_go_through_the_current_hands_module(appmod, relay):
    sent = []
    relay._hands_module = types.SimpleNamespace(alert_phone=lambda title, message: sent.append(title))
    relay._alert_via_hands("HandsRelay gateway LOCKOUT", "m")
    assert sent == ["HandsRelay gateway LOCKOUT"]


# --------------------------------------------- E8 parser loader, I1 host

def test_parser_start_kicks_off_the_background_loader(appmod, relay, monkeypatch):
    started = []
    module = types.SimpleNamespace(app=object(), start_background_loader=lambda: started.append(True))
    monkeypatch.setattr(appmod, "_load_service", lambda tag, rel: module)

    class _Probe:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def settimeout(self, _t):
            pass

        def connect_ex(self, _addr):
            return 111  # nothing is answering on the port

    monkeypatch.setattr(appmod, "socket", types.SimpleNamespace(socket=_Probe))
    monkeypatch.setattr(appmod.waitress, "create_server",
                        lambda app, host, port, threads: types.SimpleNamespace(run=lambda: None))
    relay._start_one_locked("parser")
    assert started == [True] and relay._parser_module is module


def test_gateway_bind_host_is_configurable(appmod, monkeypatch):
    monkeypatch.delenv("RELAY_GATEWAY_HOST", raising=False)
    assert appmod._service_host("gateway") == "0.0.0.0"  # default unchanged
    monkeypatch.setenv("RELAY_GATEWAY_HOST", "127.0.0.1")
    assert appmod._service_host("gateway") == "127.0.0.1"
    assert appmod._service_host("hands") == "127.0.0.1"  # never configurable
