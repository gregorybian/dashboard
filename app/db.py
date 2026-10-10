"""SQLite store for todo items, personal tasks (synced with Google Tasks), their done/muted state, and sync status."""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from .config import ROOT

DB_PATH = ROOT / "data" / "dashboard.db"

SCHEMA = """
create table if not exists items(
  id text primary key,
  source text not null,
  kind text not null,
  title text not null,
  body text, url text, author text,
  ts real, updated_ts real,
  priority integer not null default 1,
  meta_json text,
  state text not null default 'open',  -- open | done | muted | resolved
  state_ts real,                       -- done: updated_ts at the time; resolved: when
  first_seen real, last_seen real
);
create index if not exists items_source_state on items(source, state);
create table if not exists notified(key text primary key, at real);
create table if not exists sync_status(
  source text primary key, last_ok real, last_error text, last_error_at real
);
create table if not exists kv(key text primary key, value text);
create table if not exists reviews(
  id integer primary key autoincrement,
  repo text not null, number integer not null, head_sha text,
  state text not null,                 -- queued | running | posted | failed
  started real, finished real, url text, error text, comments integer
);
create table if not exists todos(
  id integer primary key autoincrement,
  text text not null,
  description text,
  created real not null,
  archived real,                       -- null while active
  due text,                            -- YYYY-MM-DD or null
  -- Google Tasks sync (app/gtasks.py)
  google_id text,
  g_updated text,                      -- Google's `updated` stamp when last synced
  dirty integer not null default 1,    -- bumped on every local edit; 0 once pushed
  deleted integer not null default 0   -- tombstone until the delete reaches Google
);
"""

TODO_COLUMNS = "id, text, description, created, archived, due"

CLOSED_RETENTION = 30 * 86400
_lock = threading.RLock()


@contextmanager
def connect():
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.executescript(SCHEMA)
        # Databases created before todos had descriptions.
        if "description" not in {r[1] for r in c.execute("pragma table_info(todos)")}:
            c.execute("alter table todos add column description text")
        # Databases created before Google Tasks sync: existing todos start dirty, so they get pushed.
        cols = {r[1] for r in c.execute("pragma table_info(todos)")}
        for col, decl in (("due", "text"), ("google_id", "text"), ("g_updated", "text"),
                          ("dirty", "integer not null default 1"), ("deleted", "integer not null default 0")):
            if col not in cols:
                c.execute(f"alter table todos add column {col} {decl}")


def _row(r):
    d = dict(r)
    d["meta"] = json.loads(d.pop("meta_json") or "{}")
    return d


def sync_source(source, items, preserve=(), now=None):
    """Replace a source's open set with `items`.

    - New items are opened.
    - A done item reopens when its updated_ts moves past the value at the time
      it was marked done; muted items never reopen.
    - Open items that are no longer reported (and not in `preserve`) are resolved.
    """
    now = now or time.time()
    with connect() as c:
        existing = {
            r["id"]: r
            for r in c.execute("select id, state, state_ts from items where source=?", (source,))
        }
        seen = set()
        for it in items:
            seen.add(it.id)
            state, state_ts = "open", None
            old = existing.get(it.id)
            if old:
                state, state_ts = old["state"], old["state_ts"]
                if state == "resolved" or (state == "done" and it.updated_ts > (state_ts or 0)):
                    state, state_ts = "open", None
            c.execute(
                """insert into items(id, source, kind, title, body, url, author, ts, updated_ts,
                                     priority, meta_json, state, state_ts, first_seen, last_seen)
                   values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   on conflict(id) do update set
                     kind=excluded.kind, title=excluded.title, body=excluded.body,
                     url=excluded.url, author=excluded.author, ts=excluded.ts,
                     updated_ts=excluded.updated_ts, priority=excluded.priority,
                     meta_json=excluded.meta_json, state=excluded.state,
                     state_ts=excluded.state_ts, last_seen=excluded.last_seen""",
                (it.id, source, it.kind, it.title, it.body, it.url, it.author, it.ts,
                 it.updated_ts, it.priority, json.dumps(it.meta), state, state_ts, now, now),
            )
        for id_, old in existing.items():
            if id_ not in seen and id_ not in preserve and old["state"] == "open":
                c.execute("update items set state='resolved', state_ts=? where id=?", (now, id_))
        c.execute(
            "delete from items where source=? and state in ('done','resolved') and last_seen < ?",
            (source, now - CLOSED_RETENTION),
        )


def set_state(item_id, action):
    with connect() as c:
        if action == "done":
            c.execute("update items set state='done', state_ts=updated_ts where id=?", (item_id,))
        elif action == "mute":
            c.execute("update items set state='muted', state_ts=? where id=?", (time.time(), item_id))
        elif action == "reopen":
            c.execute("update items set state='open', state_ts=null where id=?", (item_id,))
        else:
            raise ValueError(action)


def list_items(source=None, states=("open",), since=None):
    sql = f"select * from items where state in ({','.join('?' * len(states))})"
    args = list(states)
    if source:
        sql += " and source=?"
        args.append(source)
    if since is not None:
        sql += " and coalesce(state_ts, last_seen) >= ?"
        args.append(since)
    sql += " order by priority desc, updated_ts desc"
    with connect() as c:
        return [_row(r) for r in c.execute(sql, args)]


def open_ids(source):
    with connect() as c:
        return {r[0] for r in c.execute("select id from items where source=? and state='open'", (source,))}


def open_counts():
    with connect() as c:
        rows = c.execute("select source, count(*) from items where state='open' group by source")
        return {r[0]: r[1] for r in rows}


def notify_key(item):
    return f"{item['id']}@{float(item['updated_ts'] or 0)!r}"


def unnotified(source):
    """Open items whose current (id, updated_ts) hasn't produced an alert yet."""
    with connect() as c:
        rows = [_row(r) for r in c.execute(
            "select * from items where source=? and state='open' order by priority desc, updated_ts desc",
            (source,),
        )]
        sent = {r[0] for r in c.execute("select key from notified")}
    return [r for r in rows if notify_key(r) not in sent]


def mark_notified(keys):
    now = time.time()
    with connect() as c:
        c.executemany("insert or ignore into notified(key, at) values(?,?)", [(k, now) for k in keys])
        c.execute("delete from notified where at < ?", (now - 90 * 86400,))


def status(source=None):
    with connect() as c:
        rows = {r["source"]: dict(r) for r in c.execute("select * from sync_status")}
    return rows.get(source) if source else rows


def set_ok(source):
    with connect() as c:
        c.execute(
            """insert into sync_status(source, last_ok, last_error) values(?,?,null)
               on conflict(source) do update set last_ok=excluded.last_ok, last_error=null""",
            (source, time.time()),
        )


def set_error(source, message):
    with connect() as c:
        c.execute(
            """insert into sync_status(source, last_error, last_error_at) values(?,?,?)
               on conflict(source) do update set last_error=excluded.last_error,
                 last_error_at=excluded.last_error_at""",
            (source, message[:500], time.time()),
        )


def kv_get(key, default=None):
    with connect() as c:
        r = c.execute("select value from kv where key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else default


def kv_set(key, value):
    with connect() as c:
        c.execute(
            "insert into kv(key, value) values(?,?) on conflict(key) do update set value=excluded.value",
            (key, json.dumps(value)),
        )


REVIEW_FIELDS = {"state", "head_sha", "finished", "url", "error", "comments"}


def review_create(repo, number):
    with connect() as c:
        cur = c.execute("insert into reviews(repo, number, state, started) values(?,?,'queued',?)",
                        (repo, number, time.time()))
        return cur.lastrowid


def review_update(review_id, **fields):
    assert fields.keys() <= REVIEW_FIELDS
    sets = ", ".join(f"{k}=?" for k in fields)
    with connect() as c:
        c.execute(f"update reviews set {sets} where id=?", (*fields.values(), review_id))


def latest_reviews():
    """{"owner/repo#123": latest review row}"""
    with connect() as c:
        rows = c.execute("select * from reviews where id in (select max(id) from reviews group by repo, number)")
        return {f"{r['repo']}#{r['number']}": dict(r) for r in rows}


def fail_interrupted_reviews():
    with connect() as c:
        c.execute("update reviews set state='failed', error='interrupted by restart', finished=? "
                  "where state in ('queued','running')", (time.time(),))


def todo_add(text, description=None, due=None):
    with connect() as c:
        c.execute("insert into todos(text, description, created, due) values(?,?,?,?)",
                  (text, description or None, time.time(), due or None))


def todo_describe(todo_id, description):
    with connect() as c:
        c.execute("update todos set description=?, dirty=dirty+1 where id=?", (description or None, todo_id))


def todo_set_due(todo_id, due):
    with connect() as c:
        c.execute("update todos set due=?, dirty=dirty+1 where id=?", (due or None, todo_id))


def todo_set(todo_id, action):
    with connect() as c:
        if action == "archive":
            c.execute("update todos set archived=?, dirty=dirty+1 where id=?", (time.time(), todo_id))
        elif action == "restore":
            c.execute("update todos set archived=null, dirty=dirty+1 where id=?", (todo_id,))
        elif action == "delete":
            # Keep a tombstone until the sync deletes it from Google too.
            c.execute("update todos set deleted=1, dirty=dirty+1 where id=? and google_id is not null", (todo_id,))
            c.execute("delete from todos where id=? and google_id is null", (todo_id,))
        else:
            raise ValueError(action)


def todo_list(archived=False):
    sql = (f"select {TODO_COLUMNS} from todos where not deleted and archived is not null order by archived desc"
           if archived else f"select {TODO_COLUMNS} from todos where not deleted and archived is null "
                "order by due is null, due, created")  # dated tasks first, soonest on top
    with connect() as c:
        return [dict(r) for r in c.execute(sql)]


# Google Tasks sync (app/gtasks.py). Each push is guarded by the row's `dirty` value when it was read,
# so an edit made while a request is in flight stays dirty and goes out on the next sync.

def todo_dirty():
    with connect() as c:
        return [dict(r) for r in c.execute("select * from todos where dirty > 0 order by id")]


def todo_linked():
    with connect() as c:
        return [dict(r) for r in c.execute("select * from todos where google_id is not null")]


def todo_pushed(todo_id, dirty, google_id, g_updated):
    with connect() as c:
        c.execute("update todos set google_id=?, g_updated=? where id=?", (google_id, g_updated, todo_id))
        c.execute("update todos set dirty=0 where id=? and dirty=?", (todo_id, dirty))


def todo_purge(todo_id, dirty=None):
    """Drop a row for good (its delete reached Google, or it was deleted there)."""
    with connect() as c:
        if dirty is None:
            c.execute("delete from todos where id=? and dirty=0", (todo_id,))
        else:
            c.execute("delete from todos where id=? and dirty=?", (todo_id, dirty))


def todo_unlink(todo_id):
    """Forget the Google task (it's gone there) but keep the local row."""
    with connect() as c:
        c.execute("update todos set google_id=null, g_updated=null where id=? and dirty=0", (todo_id,))


def todo_pull(todo_id, text, description, archived, due, g_updated):
    """Apply a Google-side change unless there's an unpushed local edit (local wins until pushed)."""
    with connect() as c:
        cur = c.execute("update todos set text=?, description=?, archived=?, due=?, g_updated=? "
                        "where id=? and dirty=0", (text, description, archived, due, g_updated, todo_id))
        return cur.rowcount > 0


def todo_import(google_id, text, description, due, g_updated, created=None):
    with connect() as c:
        c.execute("insert into todos(text, description, created, due, google_id, g_updated, dirty) "
                  "values(?,?,?,?,?,?,0)", (text, description, created or time.time(), due, google_id, g_updated))
