"""Monday.com boards: kanban snapshot + weekly-update staleness."""
import html
import json
import time
from datetime import datetime

import httpx

from ..models import NORMAL, Item, SyncResult

API = "https://api.monday.com/v2"

ITEM_FIELDS = """
  id name url created_at
  group { title }
  column_values { id type text ... on PeopleValue { persons_and_teams { id kind } } }
  updates(limit: 10) { id created_at text_body creator { id name } replies { created_at creator { id } } }
"""

BOARDS = """
query($ids: [ID!]) {
  me { id }
  boards(ids: $ids) {
    id name
    columns { id title type settings_str }
    items_page(limit: 100) { cursor items { %s } }
  }
}
""" % ITEM_FIELDS

NEXT_PAGE = """
query($cursor: String!) {
  next_items_page(limit: 100, cursor: $cursor) { cursor items { %s } }
}
""" % ITEM_FIELDS

STATUS_TYPES = {"status", "color"}
# Pings posted from the dashboard start with this, so they don't count as a project update.
PING_PREFIX = "🔔 Update reminder"
PEOPLE_TYPES = {"people", "multiple-person"}


def _gql(token, query, variables):
    r = httpx.post(
        API,
        json={"query": query, "variables": variables},
        headers={"Authorization": token},
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("errors") or data.get("error_message"):
        err = (data.get("errors") or [{}])[0].get("message") or data.get("error_message")
        raise RuntimeError(f"Monday: {err}")
    return data["data"]


def _ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() if iso else None


def stale_info(last_update_ts, created_ts, now, stale_days):
    """None if fresh; otherwise how stale, plus a reopen marker that advances each period
    so a dismissed reminder comes back a week later."""
    base = last_update_ts or created_ts or now
    period = stale_days * 86400
    age = now - base
    if age < period:
        return None
    return {
        "days": int(age // 86400),
        "never": last_update_ts is None,
        "marker": base + int(age // period) * period,
    }


def _lane_order(column):
    try:
        labels = json.loads(column.get("settings_str") or "{}").get("labels", {})
    except ValueError:
        return []
    if isinstance(labels, dict):
        pairs = sorted(labels.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 999)
        names = [v for _, v in pairs]
    else:
        names = [l.get("name") if isinstance(l, dict) else l for l in labels]
    seen, out = set(), []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def is_ping(update):
    return (update.get("text_body") or "").lstrip().startswith(PING_PREFIX)


def split_updates(updates):
    """(latest real update, latest dashboard ping) from newest-first updates."""
    real = next((u for u in updates or [] if not is_ping(u)), None)
    ping = next((u for u in updates or [] if is_ping(u)), None)
    return real, ping


def owner_ids(value):
    return [str(p["id"]) for p in (value or {}).get("persons_and_teams") or [] if p.get("kind") == "person"]


def unanswered_updates(updates, me_id, since):
    """Updates by others (newest first) that you haven't replied to or followed with your own update."""
    mine = [_ts(u["created_at"]) for u in updates or [] if str((u.get("creator") or {}).get("id")) == me_id]
    out = []
    for u in updates or []:
        author = str((u.get("creator") or {}).get("id"))
        ts = _ts(u["created_at"])
        if author == me_id or is_ping(u) or ts < since:
            continue
        replied = any(str((r.get("creator") or {}).get("id")) == me_id for r in u.get("replies") or [])
        if replied or any(t > ts for t in mine):
            continue
        out.append(u)
    return out


def build_board(board, items, cfg, now, me_id=None):
    """Returns (kanban board dict, list of Items: stale projects + updates awaiting your reply)."""
    columns = board["columns"]
    status_col = next(
        (c for c in columns if c["id"] == cfg["status_column_id"]), None
    ) or next((c for c in columns if c["type"] in STATUS_TYPES), None)
    people_col = next((c for c in columns if c["type"] in PEOPLE_TYPES), None)
    done = {d.lower() for d in cfg["done_labels"]}
    watched = {s.lower() for s in cfg["stale_labels"]}
    ignored = set(cfg["ignored_groups"])

    lanes = {name: [] for name in (_lane_order(status_col) if status_col else [])}
    stale_items = []
    for it in items:
        group = (it.get("group") or {}).get("title", "")
        if group in ignored:
            continue
        values = {v["id"]: v for v in it["column_values"]}
        status = (values.get(status_col["id"], {}).get("text") if status_col else None) or "No status"
        people = values.get(people_col["id"], {}) if people_col else {}
        owner = people.get("text") or ""
        upd, ping = split_updates(it.get("updates"))
        last_ts = _ts(upd["created_at"]) if upd else None
        ping_ts = _ts(ping["created_at"]) if ping else None
        watch = status.lower() in watched if watched else status.lower() not in done
        info = stale_info(last_ts, _ts(it.get("created_at")), now, cfg["stale_days"]) if watch else None
        if watch and me_id:
            for u in unanswered_updates(it.get("updates"), me_id, now - cfg["reply_lookback_days"] * 86400):
                who = (u.get("creator") or {}).get("name") or "someone"
                text = " ".join((u.get("text_body") or "").split())
                stale_items.append(Item(
                    id=f"monday:reply:{u['id']}", source="monday", kind="reply",
                    title=it["name"], body=f"{who}: {text[:280]}{'…' if len(text) > 280 else ''}",
                    url=f"{it.get('url', '')}/posts/{u['id']}", author=who,
                    ts=_ts(u["created_at"]), updated_ts=_ts(u["created_at"]), priority=NORMAL,
                    meta={"board": board["name"], "status": status, "owner": owner},
                ))
        card = {
            "id": it["id"], "name": it["name"], "url": it.get("url", ""), "owner": owner,
            "group": group, "status": status,
            "last_update_days": int((now - last_ts) // 86400) if last_ts else None,
            "last_update_by": (upd.get("creator") or {}).get("name") if upd else None,
            "stale": info is not None,
        }
        lanes.setdefault(status, []).append(card)
        if info:
            if info["never"]:
                body = "No updates ever posted"
            else:
                who = card["last_update_by"] or "someone"
                body = f"No update in {info['days']}d (last by {who})"
            stale_items.append(Item(
                id=f"monday:{it['id']}", source="monday", kind="stale",
                title=it["name"], body=body, url=card["url"], author=owner,
                ts=last_ts or 0.0, updated_ts=info["marker"], priority=NORMAL,
                meta={"board": board["name"], "status": status, "owner": owner, "days": info["days"],
                      "item_id": it["id"],
                      "owner_ids": [i for i in owner_ids(people) if i != me_id],  # people to ping
                      "mine": me_id in owner_ids(people),
                      "pinged_ts": ping_ts},
            ))

    kanban = {
        "id": board["id"], "name": board["name"],
        "lanes": [{"label": k, "done": k.lower() in done, "cards": v} for k, v in lanes.items()],
    }
    return kanban, stale_items


def fetch(cfg, token):
    mcfg = cfg["monday"]
    if not mcfg["board_ids"]:
        raise ValueError("set monday.board_ids in config.yaml")
    now = time.time()
    data = _gql(token, BOARDS, {"ids": [str(b) for b in mcfg["board_ids"]]})
    boards, me_id = data["boards"], str(data["me"]["id"])
    kanbans, items = [], []
    for board in boards:
        page = board["items_page"]
        rows = list(page["items"])
        cursor = page["cursor"]
        while cursor:
            page = _gql(token, NEXT_PAGE, {"cursor": cursor})["next_items_page"]
            rows += page["items"]
            cursor = page["cursor"]
        kanban, stale = build_board(board, rows, mcfg, now, me_id)
        kanbans.append(kanban)
        items += stale
    return SyncResult(items=items, snapshot={"boards": kanbans})


PING_ITEM = """
query($id: ID!) {
  me { id }
  items(ids: [$id]) {
    id name url
    column_values { id type ... on PeopleValue { persons_and_teams { id kind } } }
    updates(limit: 10) { id created_at text_body creator { id name } replies { created_at creator { id } } }
  }
}
"""

USERS = "query($ids: [ID!]) { users(ids: $ids) { id name } }"

CREATE_UPDATE = """
mutation($item: ID!, $body: String!, $mentions: [UpdateMention]) {
  create_update(item_id: $item, body: $body, mentions_list: $mentions) { id }
}
"""


def ping_body(names_by_id, item_name, last_ts, now, account_url):
    mentions = " ".join(
        f'<a class="user_mention_editor router" href="{account_url}/users/{uid}" '
        f'data-mention-type="User" data-mention-id="{uid}" target="_blank" '
        f'rel="noopener noreferrer">@{html.escape(name)}</a>'
        for uid, name in names_by_id.items()
    )
    last = (f"the last update was {int((now - last_ts) // 86400)} days ago"
            if last_ts else "there hasn't been an update yet")
    return (f"<p>{PING_PREFIX}: Hi {mentions}, could you post a quick update on where "
            f"<b>{html.escape(item_name)}</b> is at? Right now {last}. Thanks!</p>")


def ping(cfg, token, item_id):
    """Post an update on the item @mentioning its owners. Returns the names pinged."""
    data = _gql(token, PING_ITEM, {"id": str(item_id)})
    if not data["items"]:
        raise ValueError("item not found")
    it = data["items"][0]
    ids = [i for v in it["column_values"] if v["type"] in PEOPLE_TYPES for i in owner_ids(v)]
    ids = [i for i in dict.fromkeys(ids) if i != str(data["me"]["id"])]  # don't ping yourself
    if not ids:
        raise ValueError("no owner to ping (People column is empty or only you)")
    names = {str(u["id"]): u["name"] for u in _gql(token, USERS, {"ids": ids})["users"]}
    upd, _ = split_updates(it.get("updates"))
    account_url = (it.get("url") or "").split("/boards/")[0]
    body = ping_body(names, it["name"], _ts(upd["created_at"]) if upd else None, time.time(), account_url)
    _gql(token, CREATE_UPDATE, {"item": str(item_id), "body": body,
                                "mentions": [{"id": i, "type": "User"} for i in names]})
    return list(names.values())
