import threading
import urllib.parse

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, gtasks, main

client = TestClient(main.app)  # no lifespan: the scheduler isn't started


@pytest.fixture
def oauth(tmp_path, monkeypatch):
    """A fake OAuth client and Google token endpoint; the token file goes to tmp_path."""
    monkeypatch.setattr(gtasks, "client", lambda: ("cid", "secret"))
    monkeypatch.setattr(gtasks, "TOKEN_PATH", tmp_path / "token.json")
    posted = []

    def fake_post(url, data, timeout):
        posted.append(data)
        return httpx.Response(200, json={"refresh_token": "new-refresh"})
    monkeypatch.setattr(gtasks.httpx, "post", fake_post)
    monkeypatch.setattr(gtasks, "_login", {"url": None, "thread": None})
    return posted


def redirect(url, **params):
    """What the browser does after Google's consent page: hit the loopback redirect."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    params.setdefault("state", q["state"][0])
    return httpx.get(q["redirect_uri"][0], params=params)


def test_web_login_saves_token_and_calls_back(oauth):
    done = threading.Event()
    url = gtasks.web_login(done.set)
    assert gtasks.web_login(done.set) == url  # a second click reuses the flow still waiting
    assert redirect(url, code="abc").status_code == 200
    assert done.wait(5)
    assert gtasks.refresh_token() == "new-refresh"
    assert oauth[0]["code"] == "abc"


def test_denied_sign_in_is_reported(oauth):
    url = gtasks.web_login(lambda: pytest.fail("shouldn't run"))
    redirect(url, error="access_denied")
    gtasks._login["thread"].join(5)
    assert db.status(gtasks.STATUS)["last_error"] == "AuthError: Sign-in failed: access_denied"
    assert gtasks.refresh_token() is None


def test_wrong_state_is_ignored(oauth):
    done = threading.Event()
    url = gtasks.web_login(done.set)
    assert redirect(url, code="evil", state="forged").status_code == 400
    assert redirect(url, code="abc").status_code == 200
    assert done.wait(5) and oauth[0]["code"] == "abc"


def test_login_route(monkeypatch):
    monkeypatch.setattr(gtasks, "client", lambda: None)
    assert client.post("/api/gtasks/login").status_code == 400
    monkeypatch.setattr(gtasks, "client", lambda: ("cid", "secret"))
    monkeypatch.setattr(gtasks, "web_login", lambda on_done: "https://accounts.google.com/x")
    assert client.post("/api/gtasks/login").json() == {"url": "https://accounts.google.com/x"}


def test_sign_in_button_only_on_auth_errors(monkeypatch):
    monkeypatch.setattr(gtasks, "enabled", lambda: True)
    db.set_error(gtasks.STATUS, "RuntimeError: Google Tasks 503: backend error")
    assert "gtasks-login" not in client.get("/partials/header").text
    db.set_error(gtasks.STATUS, "AuthError: Google login expired or revoked (invalid_grant)")
    assert "gtasks-login" in client.get("/partials/header").text
