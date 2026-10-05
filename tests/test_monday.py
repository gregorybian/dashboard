from app.sources.monday import (PING_PREFIX, build_board, ping_body, split_updates, stale_info,
                                unanswered_updates)

DAY = 86400
NOW = 100 * DAY
CFG = {"status_column_id": None, "done_labels": ["Done"], "stale_labels": [], "ignored_groups": ["Archive"], "stale_days": 7,
       "reply_lookback_days": 14}


def test_stale_info():
    assert stale_info(NOW - 6 * DAY, None, NOW, 7) is None
    s = stale_info(NOW - 8 * DAY, None, NOW, 7)
    assert s["days"] == 8 and not s["never"]
    assert stale_info(None, NOW - 10 * DAY, NOW, 7)["never"]


def test_marker_advances_weekly():
    a = stale_info(NOW - 8 * DAY, None, NOW, 7)["marker"]
    b = stale_info(NOW - 8 * DAY, None, NOW + 3 * DAY, 7)["marker"]
    c = stale_info(NOW - 8 * DAY, None, NOW + 7 * DAY, 7)["marker"]
    assert a == b < c


def iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def item(id_, status, update_days_ago=None, group="Active"):
    return {
        "id": id_, "name": f"proj {id_}", "url": f"u{id_}", "created_at": iso(NOW - 30 * DAY),
        "group": {"title": group},
        "column_values": [{"id": "status", "type": "status", "text": status},
                          {"id": "person", "type": "people", "text": "Sam"}],
        "updates": [{"id": f"{id_}-u", "created_at": iso(NOW - update_days_ago * DAY), "creator": {"id": "9", "name": "Sam"}}]
        if update_days_ago is not None else [],
    }


def test_build_board():
    board = {"id": "1", "name": "Projects", "columns": [
        {"id": "status", "title": "Status", "type": "status",
         "settings_str": '{"labels": {"0": "Working on it", "1": "Done", "2": "Stuck"}}'},
        {"id": "person", "title": "Owner", "type": "people", "settings_str": "{}"},
    ]}
    rows = [item("a", "Working on it", 2), item("b", "Stuck", 10), item("c", "Done", 40),
            item("d", "Working on it"), item("e", "Stuck", 20, group="Archive")]
    kanban, stale = build_board(board, rows, CFG, NOW)
    assert [l["label"] for l in kanban["lanes"]] == ["Working on it", "Done", "Stuck"]
    assert sorted(s.id for s in stale if s.kind == "stale") == ["monday:b", "monday:d"]
    assert kanban["lanes"][0]["cards"][0]["owner"] == "Sam"


def test_only_watched_columns_are_flagged():
    board = {"id": "1", "name": "P", "columns": [
        {"id": "status", "title": "Status", "type": "status", "settings_str": "{}"},
        {"id": "person", "title": "Owner", "type": "people", "settings_str": "{}"}]}
    rows = [item("a", "Working on it", 10), item("b", "Stuck", 10), item("c", "Upcoming", 10)]
    _, stale = build_board(board, rows, {**CFG, "stale_labels": ["Working on it"]}, NOW)
    assert [s.id for s in stale if s.kind == "stale"] == ["monday:a"]


def test_pings_do_not_count_as_updates():
    ups = [{"created_at": "x2", "text_body": f"{PING_PREFIX}: Hi @Sam"}, {"created_at": "x1", "text_body": "real"}]
    real, ping = split_updates(ups)
    assert real["text_body"] == "real" and ping["created_at"] == "x2"
    assert split_updates([]) == (None, None)


def test_ping_body_mentions_and_escapes():
    body = ping_body({"42": "Sam <S>"}, "MPPT & BMS", NOW - 9 * DAY, NOW, "https://x.monday.com")
    assert body.startswith(f"<p>{PING_PREFIX}:")
    assert 'data-mention-id="42"' in body and "@Sam &lt;S&gt;" in body
    assert "MPPT &amp; BMS" in body and "9 days ago" in body


def test_self_is_not_a_ping_target():
    board = {"id": "1", "name": "P", "columns": [
        {"id": "status", "title": "Status", "type": "status", "settings_str": "{}"},
        {"id": "person", "title": "Owner", "type": "people", "settings_str": "{}"}]}
    row = item("a", "Working on it", 10)
    row["column_values"][1]["persons_and_teams"] = [{"id": 7, "kind": "person"}, {"id": 8, "kind": "person"}]
    _, items = build_board(board, [row], CFG, NOW, me_id="7")
    stale = next(i for i in items if i.kind == "stale")
    assert stale.meta["owner_ids"] == ["8"] and stale.meta["mine"]


def upd(id_, days_ago, author, replies=(), text="progress"):
    return {"id": id_, "created_at": iso(NOW - days_ago * DAY), "text_body": text,
            "creator": {"id": author, "name": f"user{author}"},
            "replies": [{"created_at": iso(NOW - d * DAY), "creator": {"id": a}} for a, d in replies]}


def test_unanswered_updates():
    since = NOW - 14 * DAY
    ups = [
        upd("u1", 1, "5"),                          # needs reply
        upd("u2", 2, "5", replies=[("me", 1)]),     # I replied
        upd("u3", 3, "me"),                         # my own update...
        upd("u4", 4, "6"),                          # ...which answers older ones
        upd("u0", 30, "5"),                         # too old
        upd("p", 0, "5", text=f"{PING_PREFIX}: hi"),  # a ping, never needs a reply
    ]
    assert [u["id"] for u in unanswered_updates(ups, "me", since)] == ["u1"]
