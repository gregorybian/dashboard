import pytest
from cryptography.exceptions import InvalidTag

from app import db, publish


@pytest.fixture
def pushes(monkeypatch):
    sent = []
    monkeypatch.setenv("PUBLISH_PASSWORD", "correct horse battery staple")
    monkeypatch.setattr(publish, "push", sent.append)
    return sent


def test_round_trip_and_wrong_password():
    payload = {"todos": [{"text": "a", "description": "x\ny"}]}
    blob = publish.encrypt(payload, "pw", iterations=1000)
    assert publish.decrypt(blob, "pw") == payload
    with pytest.raises(InvalidTag):
        publish.decrypt(blob, "not the pw")


def test_unchanged_snapshot_is_not_republished(pushes):
    assert publish.publish()
    assert not publish.publish()
    db.todo_add("new task")
    assert publish.publish()
    assert len(pushes) == 2
    html = publish.decrypt(pushes[-1], "correct horse battery staple")["html"]
    assert "new task" in html
    # Read-only: the buttons that call the local API are left out.
    assert "data-todo=" not in html and 'id="todo-add"' not in html and "/api/" not in html


def test_disabled_without_password(pushes, monkeypatch):
    monkeypatch.delenv("PUBLISH_PASSWORD")
    assert not publish.publish(force=True)
    assert pushes == []


def test_push_failure_is_reported(pushes, monkeypatch):
    def boom(blob):
        raise RuntimeError("git push failed: denied")
    monkeypatch.setattr(publish, "push", boom)
    assert not publish.publish()
    assert "denied" in db.status("publish")["last_error"]
