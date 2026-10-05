from app import db
from app.models import Item


def it(id_, updated=100.0):
    return Item(id=id_, source="slack", kind="dm", title=id_, updated_ts=updated)


def states():
    return {r["id"]: r["state"] for r in db.list_items(states=("open", "done", "muted", "resolved"))}


def test_new_items_open_and_missing_ones_resolve():
    db.sync_source("slack", [it("a"), it("b")])
    db.sync_source("slack", [it("a")])
    assert states() == {"a": "open", "b": "resolved"}


def test_preserved_items_are_untouched():
    db.sync_source("slack", [it("a"), it("b")])
    db.sync_source("slack", [it("a")], preserve={"b"})
    assert states()["b"] == "open"


def test_done_reopens_only_on_new_activity():
    db.sync_source("slack", [it("a", 100)])
    db.set_state("a", "done")
    db.sync_source("slack", [it("a", 100)])
    assert states()["a"] == "done"
    db.sync_source("slack", [it("a", 200)])
    assert states()["a"] == "open"


def test_mute_is_permanent():
    db.sync_source("slack", [it("a", 100)])
    db.set_state("a", "mute")
    db.sync_source("slack", [it("a", 999)])
    db.sync_source("slack", [])
    assert states()["a"] == "muted"


def test_resolved_item_reopens_when_it_reappears():
    db.sync_source("slack", [it("a")])
    db.sync_source("slack", [])
    db.sync_source("slack", [it("a", 150)])
    assert states()["a"] == "open"


def test_notifications_dedupe_per_activity():
    db.sync_source("slack", [it("a", 100.123456789)])
    rows = db.unnotified("slack")
    assert [r["id"] for r in rows] == ["a"]
    db.mark_notified([db.notify_key(r) for r in rows])
    assert db.unnotified("slack") == []
    db.sync_source("slack", [it("a", 200.5)])
    assert [r["id"] for r in db.unnotified("slack")] == ["a"]


def test_sources_are_isolated():
    db.sync_source("slack", [it("a")])
    db.sync_source("github", [])
    assert states()["a"] == "open"


def test_todos_archive_restore_delete():
    db.todo_add("a")
    db.todo_add("b")
    a, b = db.todo_list()
    db.todo_set(a["id"], "archive")
    assert [t["text"] for t in db.todo_list()] == ["b"]
    assert [t["text"] for t in db.todo_list(archived=True)] == ["a"]
    db.todo_set(a["id"], "restore")
    assert [t["text"] for t in db.todo_list()] == ["a", "b"]
    db.todo_set(b["id"], "delete")
    assert [t["text"] for t in db.todo_list() + db.todo_list(archived=True)] == ["a"]


def test_todo_description():
    db.todo_add("a", "first line\nsecond")
    (t,) = db.todo_list()
    assert t["description"] == "first line\nsecond"
    db.todo_describe(t["id"], "")
    assert db.todo_list()[0]["description"] is None


def test_init_adds_description_to_old_todos_table(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    sqlite3.connect(path).executescript(
        "create table todos(id integer primary key autoincrement, text text not null,"
        " created real not null, archived real); insert into todos(text, created) values('x', 1);")
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init()
    assert db.todo_list()[0]["description"] is None
