from datetime import date, timedelta

from fastapi.testclient import TestClient

from app import db, main

client = TestClient(main.app)  # no lifespan: the scheduler isn't started


def test_add_and_change_due_date():
    assert client.post("/api/todos", json={"text": "t", "due": "2026-10-12"}).status_code == 204
    [t] = db.todo_list()
    assert t["due"] == "2026-10-12"
    assert client.put(f"/api/todos/{t['id']}/due", json={"due": ""}).status_code == 204
    assert db.todo_list()[0]["due"] is None


def test_bad_due_date_is_rejected():
    assert client.post("/api/todos", json={"text": "t", "due": "next friday"}).status_code == 400
    assert db.todo_list() == []


def test_due_labels():
    today = date.today()
    assert main.due_label(today.isoformat()) == ("Today", "today")
    assert main.due_label((today + timedelta(days=1)).isoformat()) == ("Tomorrow", "soon")
    assert main.due_label((today - timedelta(days=3)).isoformat())[1] == "overdue"
    assert main.due_label((today + timedelta(days=30)).isoformat())[1] == ""


def test_tasks_partial_shows_due_chip():
    db.todo_add("dated", due=date.today().isoformat())
    db.todo_add("undated")
    html = client.get("/partials/tasks").text
    assert "📅 Today" in html and 'class="due today"' in html and 'class="due unset"' in html
