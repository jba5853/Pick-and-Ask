"""Offline tests for the HTTP layer: access code, rate limits, request checks. No model calls."""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")  # app.main refuses to start without one

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.assistant import Result  # noqa: E402
from app.limits import RateLimiter  # noqa: E402

CODE = "orange-printer-42"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def api(monkeypatch):
    """A client with fresh limits, a known access code and a fake conversation (no API calls)."""
    clock = Clock()
    monkeypatch.setenv("ACCESS_CODE", CODE)
    monkeypatch.setattr(main, "session_limit", RateLimiter(main.ASKS_PER_SESSION, main.WINDOW, clock))
    monkeypatch.setattr(main, "ip_limit", RateLimiter(main.ASKS_PER_IP, main.WINDOW, clock))
    monkeypatch.setattr(main, "wrong_codes", RateLimiter(main.WRONG_CODES_PER_IP, main.WINDOW, clock))
    calls = []

    def fake_converse(session, pack, question, action, image):
        calls.append((session.id, pack.id, question, action))
        return Result("answered", "ok", None, "none", session_id=session.id)

    monkeypatch.setattr(main, "converse", fake_converse)
    client = TestClient(main.app)
    client.clock, client.calls = clock, calls
    return client


def ask(client, code=CODE, session="session-0001", question="What's wrong?", **form):
    headers = {"X-Access-Code": code} if code is not None else {}
    return client.post("/ask", data={"question": question, "pack": "3d-printing", "session_id": session, **form},
                       headers=headers)


# ---------- access code ----------

def test_ask_without_code_is_rejected(api):
    r = ask(api, code=None)
    assert r.status_code == 401 and "access code" in r.json()["detail"]
    assert api.calls == []


def test_ask_with_wrong_code_is_rejected(api):
    assert ask(api, code="nope").status_code == 401
    assert api.calls == []


def test_ask_with_right_code_works(api):
    r = ask(api)
    assert r.status_code == 200 and r.json()["status"] == "answered"
    assert api.calls == [("session-0001", "3d-printing", "What's wrong?", "message")]


def test_code_ignores_surrounding_spaces(api):
    assert ask(api, code=f"  {CODE} ").status_code == 200


def test_access_endpoint_checks_the_code(api):
    assert api.post("/access", data={"code": CODE}).status_code == 204
    assert api.post("/access", data={"code": "wrong"}).status_code == 401
    assert api.post("/access", data={}).status_code == 401


def test_config_says_whether_a_code_is_needed(api, monkeypatch):
    assert api.get("/config").json() == {"access_code_required": True}
    monkeypatch.delenv("ACCESS_CODE")
    assert api.get("/config").json() == {"access_code_required": False}


def test_no_code_configured_means_open_for_local_use(api, monkeypatch):
    monkeypatch.delenv("ACCESS_CODE")
    assert ask(api, code=None).status_code == 200


def test_too_many_wrong_codes_locks_out_even_the_right_one(api):
    for _ in range(main.WRONG_CODES_PER_IP):
        assert api.post("/access", data={"code": "guess"}).status_code == 401
    r = api.post("/access", data={"code": CODE})
    assert r.status_code == 429 and "Too many wrong access codes" in r.json()["detail"]
    assert ask(api).status_code == 429
    api.clock.now += main.WINDOW + 1                     # the lockout ends with the window
    assert ask(api).status_code == 200


def test_page_and_packs_load_without_a_code(api):
    assert api.get("/").status_code == 200
    assert api.get("/packs").status_code == 200


# ---------- rate limits ----------

def test_session_limit_is_20_asks_per_10_minutes(api):
    for _ in range(20):
        assert ask(api).status_code == 200
    r = ask(api)
    assert r.status_code == 429
    assert "asked a lot" in r.json()["detail"] and "10 minutes" in r.json()["detail"]
    assert r.headers["Retry-After"] == str(main.WINDOW)
    assert len(api.calls) == 20                          # the 21st never reached the model
    assert ask(api, session="session-0002").status_code == 200  # other sessions are unaffected
    api.clock.now += main.WINDOW
    assert ask(api).status_code == 200                   # the window slides


def test_every_action_counts_towards_the_limit(api):
    for action in ["walkthrough", "done", "failed", "repeat", "summary"] * 4:
        assert ask(api, question="", action=action).status_code == 200
    assert ask(api, question="", action="done").status_code == 429


def test_ip_limit_backs_up_the_session_limit(api):
    # A new session id per request can't get around the per-IP limit.
    for i in range(main.ASKS_PER_IP):
        assert ask(api, session=f"session-{i:04d}").status_code == 200
    r = ask(api, session="session-9999")
    assert r.status_code == 429 and "this network" in r.json()["detail"]


def test_rejected_requests_do_not_use_up_the_limit(api):
    for _ in range(5):
        ask(api, code="wrong")
        ask(api, question="")                            # empty question: 400
        ask(api, action="hack")                          # unknown action: 400
    for _ in range(20):
        assert ask(api).status_code == 200


def test_rate_limiter_window_and_retry_after():
    clock = Clock()
    limiter = RateLimiter(3, 60, clock)
    assert [limiter.hit("a") for _ in range(4)] == [True, True, True, False]
    assert limiter.blocked("a") and not limiter.blocked("b")
    clock.now += 20
    assert limiter.retry_after("a") == 40
    clock.now += 40
    assert not limiter.blocked("a") and limiter.retry_after("a") == 0
    assert limiter.hit("a")


def test_rate_limiter_forgets_idle_keys():
    clock = Clock()
    limiter = RateLimiter(2, 60, clock)
    for i in range(100):
        limiter.hit(f"k{i}")
    clock.now += 61
    for i in range(100):
        limiter.blocked(f"k{i}")
    assert limiter._events == {}


# ---------- client IP behind the tunnel ----------

def fake_request(peer, cf=None):
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers={"cf-connecting-ip": cf} if cf else {})


def test_tunnel_header_is_trusted_only_from_localhost():
    assert main.client_ip(fake_request("127.0.0.1", "203.0.113.9")) == "203.0.113.9"
    assert main.client_ip(fake_request("::1", "203.0.113.9")) == "203.0.113.9"
    assert main.client_ip(fake_request("192.168.1.50", "203.0.113.9")) == "192.168.1.50"  # spoofed header ignored
    assert main.client_ip(fake_request("127.0.0.1")) == "127.0.0.1"


# ---------- other request checks ----------

def test_bad_requests(api):
    assert ask(api, action="hack").status_code == 400
    assert ask(api, session="../etc").status_code == 400
    assert ask(api, question="").status_code == 400
    assert ask(api, pack="nope").status_code == 400
