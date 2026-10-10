import itertools

import httpx
import pytest

from app import db, gtasks


class FakeGoogle:
    """In-memory Google Tasks list behind gtasks._api."""

    def __init__(self):
        self.tasks = {}
        self.ids = itertools.count(1)
        self.clock = itertools.count(1)

    def stamp(self):
        return f"2026-10-06T00:00:{next(self.clock):02d}.000Z"

    def add(self, title, notes=None, status="needsAction", due=None):
        gid = f"g{next(self.ids)}"
        self.tasks[gid] = {"id": gid, "title": title, "notes": notes, "status": status, "due": due,
                           "completed": "2026-10-05T00:00:00.000Z" if status == "completed" else None,
                           "updated": self.stamp()}
        return gid

    def edit(self, gid, **fields):
        self.tasks[gid].update(fields, updated=self.stamp())

    def __call__(self, method, path, json=None, params=None):
        parts = path.strip("/").split("/")  # lists/<lid>/tasks[/<id>]
        gid = parts[3] if len(parts) > 3 else None
        if method == "GET":
            return httpx.Response(200, json={"items": list(self.tasks.values())})
        if method == "POST":
            gid = self.add(json["title"], json["notes"], json["status"], json["due"])
            return httpx.Response(200, json=self.tasks[gid])
        if gid not in self.tasks:
            return httpx.Response(404, json={"error": {"message": "Not Found"}})
        if method == "PATCH":
            self.edit(gid, **json)
            return httpx.Response(200, json=self.tasks[gid])
        if method == "DELETE":
            del self.tasks[gid]
            return httpx.Response(204)
        raise AssertionError(method)


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(gtasks, "enabled", lambda: True)
    monkeypatch.setattr(gtasks, "list_id", lambda: "@default")
    monkeypatch.setattr(gtasks, "_api", lambda method, path, **kw: fake(method, path, **kw))
    return fake


def titles(archived=False):
    return [t["text"] for t in db.todo_list(archived=archived)]


def local(text):
    return next(t for t in db.todo_list() + db.todo_list(archived=True) if t["text"] == text)


def test_local_tasks_are_pushed_and_edits_follow(google):
    db.todo_add("wire BMS", "check pinout")
    gtasks.sync()
    [task] = google.tasks.values()
    assert (task["title"], task["notes"], task["status"]) == ("wire BMS", "check pinout", "needsAction")

    db.todo_describe(local("wire BMS")["id"], "pinout done")
    db.todo_set(local("wire BMS")["id"], "archive")
    gtasks.sync()
    assert (task["notes"], task["status"]) == ("pinout done", "completed")

    db.todo_set(local("wire BMS")["id"], "delete")
    assert titles(archived=True) == []  # hidden right away, tombstoned until pushed
    gtasks.sync()
    assert google.tasks == {}
    with db.connect() as c:
        assert c.execute("select count(*) from todos").fetchone()[0] == 0


def test_google_changes_are_pulled(google):
    gid = google.add("from phone", "notes")
    google.add("old and done", status="completed")
    assert gtasks.sync()
    assert titles() == ["from phone"] and titles(archived=True) == []  # completed history isn't imported
    assert local("from phone")["description"] == "notes"

    google.edit(gid, title="renamed", status="completed", completed="2026-10-05T00:00:00.000Z")
    gtasks.sync()
    assert titles() == [] and titles(archived=True) == ["renamed"]

    google.edit(gid, status="needsAction", completed=None)
    gtasks.sync()
    assert titles() == ["renamed"]

    del google.tasks[gid]
    gtasks.sync()
    assert titles() == []


def test_unpushed_local_edit_wins_over_google(google):
    db.todo_add("task")
    gtasks.sync()
    [gid] = google.tasks
    google.edit(gid, notes="from google")
    db.todo_describe(local("task")["id"], "from dashboard")
    gtasks.sync()
    assert google.tasks[gid]["notes"] == "from dashboard"
    assert local("task")["description"] == "from dashboard"


def test_task_deleted_in_google_but_edited_here_is_recreated(google):
    db.todo_add("keep me")
    gtasks.sync()
    google.tasks.clear()
    db.todo_describe(local("keep me")["id"], "edited")
    gtasks.sync()
    assert [t["title"] for t in google.tasks.values()] == ["keep me"]


def test_archived_task_purged_by_google_stays_archived(google):
    db.todo_add("done long ago")
    db.todo_set(local("done long ago")["id"], "archive")
    gtasks.sync()
    google.tasks.clear()
    gtasks.sync()
    assert titles(archived=True) == ["done long ago"]
    db.todo_set(local("done long ago")["id"], "restore")  # unlinked, so restoring re-creates it
    gtasks.sync()
    assert [t["title"] for t in google.tasks.values()] == ["done long ago"]


def test_edit_during_push_is_not_lost(google, monkeypatch):
    db.todo_add("racy")
    real = gtasks._api

    def edit_mid_request(method, path, **kw):
        r = real(method, path, **kw)
        if method == "POST":
            db.todo_describe(local("racy")["id"], "typed while pushing")
        return r
    monkeypatch.setattr(gtasks, "_api", edit_mid_request)
    gtasks.sync()
    monkeypatch.setattr(gtasks, "_api", real)
    gtasks.sync()
    [task] = google.tasks.values()
    assert task["notes"] == "typed while pushing"
    assert len(google.tasks) == 1  # second push patched, didn't insert again


def test_errors_are_reported_and_keep_local_state(google, monkeypatch):
    db.todo_add("offline")
    monkeypatch.setattr(gtasks, "_api", lambda *a, **kw: httpx.Response(503, json={"error": {"message": "down"}}))
    assert not gtasks.sync()
    assert "down" in db.status(gtasks.STATUS)["last_error"]
    assert titles() == ["offline"]


def test_due_dates_sync_both_ways(google):
    db.todo_add("with date", due="2026-10-12")
    gtasks.sync()
    [gid] = google.tasks
    assert google.tasks[gid]["due"] == "2026-10-12T00:00:00.000Z"

    google.edit(gid, due="2026-10-20T00:00:00.000Z")
    gtasks.sync()
    assert local("with date")["due"] == "2026-10-20"

    db.todo_set_due(local("with date")["id"], None)
    gtasks.sync()
    assert google.tasks[gid]["due"] is None

    google.add("phone task", due="2026-11-01T00:00:00.000Z")
    gtasks.sync()
    assert local("phone task")["due"] == "2026-11-01"


def test_field_missing_locally_is_backfilled_without_a_google_edit(google):
    gid = google.add("old import", due="2026-09-25T00:00:00.000Z")
    gtasks.sync()
    with db.connect() as c:  # as if imported before due dates were synced
        c.execute("update todos set due=null")
    assert gtasks.sync()
    assert local("old import")["due"] == "2026-09-25"
    assert not gtasks.sync()  # settled: nothing changes on the next pass
