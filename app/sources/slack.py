"""Slack todos: unanswered DMs, @mentions, and new replies in threads you posted in.

One item per DM conversation and one per thread. An item exists while the newest
messages there come from someone else; once you reply it disappears (resolved).
"""
import re
import time
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

from .. import db
from ..models import HIGH, NORMAL, Item, SyncResult

IGNORED_SUBTYPES = {
    "channel_join", "channel_leave", "channel_topic", "channel_purpose", "channel_name",
    "group_join", "group_leave", "bot_add", "bot_remove", "pinned_item", "unpinned_item",
    "reminder_add", "channel_archive",
}
DM_LIST_TTL = 1800


# ---------- pure helpers (unit tested) ----------

def actionable(msg, me, include_bots=False):
    if msg.get("user") == me or msg.get("subtype") in IGNORED_SUBTYPES:
        return False
    if not include_bots and (msg.get("bot_id") or msg.get("subtype") == "bot_message"):
        return False
    return True


def unreplied(messages, me, since=0.0, include_bots=False):
    """Messages after your last message (and at/after `since`), oldest first."""
    my_last = max((float(m["ts"]) for m in messages if m.get("user") == me), default=0.0)
    out = [
        m for m in messages
        if float(m["ts"]) > my_last and float(m["ts"]) >= since and actionable(m, me, include_bots)
    ]
    return sorted(out, key=lambda m: float(m["ts"]))


def thread_ts_from_permalink(permalink):
    qs = parse_qs(urlparse(permalink or "").query)
    return qs.get("thread_ts", [None])[0]


def format_text(text, name_of, limit=280):
    text = re.sub(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>", lambda m: "@" + name_of(m.group(1)), text or "")
    text = re.sub(r"<#[A-Z0-9]+\|([^>]*)>", r"#\1", text)
    text = re.sub(r"<!subteam\^[A-Z0-9]+(?:\|([^>]*))?>", lambda m: m.group(1) or "@group", text)
    text = re.sub(r"<!(here|channel|everyone)(?:\|[^>]*)?>", r"@\1", text)
    text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def mpim_label(name):
    # "mpdm-alice--bob--carol-1" -> "alice, bob, carol"
    core = re.sub(r"^mpdm-|-\d+$", "", name or "")
    return ", ".join(p for p in core.split("--") if p)


# ---------- API-backed source ----------

class SlackSource:
    def __init__(self, token):
        self.token = token
        self.client = WebClient(token=token, timeout=30)
        self.client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=4))
        auth = self.client.auth_test()
        self.me = auth["user_id"]
        self.team_url = auth["url"].rstrip("/")
        self.users = {}
        self._dms = None
        self._dms_at = 0.0

    def name(self, uid):
        if not uid:
            return "someone"
        if uid not in self.users:
            try:
                u = self.client.users_info(user=uid)["user"]
                p = u.get("profile", {})
                self.users[uid] = p.get("display_name") or p.get("real_name") or u.get("name") or uid
            except SlackApiError:
                self.users[uid] = uid
        return self.users[uid]

    def link(self, channel, ts, thread_ts=None):
        url = f"{self.team_url}/archives/{channel}/p{ts.replace('.', '')}"
        if thread_ts and thread_ts != ts:
            url += f"?thread_ts={thread_ts}&cid={channel}"
        return url

    def _paginate(self, method, key, **kw):
        out, cursor = [], None
        while True:
            resp = method(cursor=cursor, **kw) if cursor else method(**kw)
            out += resp.get(key, [])
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                return out

    def _search(self, query, max_pages=3):
        out = []
        for page in range(1, max_pages + 1):
            resp = self.client.search_messages(
                query=query, sort="timestamp", sort_dir="desc", count=100, page=page
            )
            msgs = resp["messages"]
            out += msgs.get("matches", [])
            if page >= msgs.get("paging", {}).get("pages", 1):
                break
        return out

    # --- DMs ---

    def dm_channels(self):
        if self._dms is None or time.time() - self._dms_at > DM_LIST_TTL:
            chans = self._paginate(
                self.client.users_conversations, "channels",
                types="im,mpim", exclude_archived=True, limit=200,
            )
            self._dms = [c for c in chans if c.get("user") != self.me]
            self._dms_at = time.time()
        return self._dms

    def dm_items(self, cfg, now):
        chans = self.dm_channels()
        latest = db.kv_get("slack:dm_latest", {})
        open_now = db.open_ids("slack")
        hot_cutoff = now - cfg["dm_hot_days"] * 86400

        hot = [c for c in chans
               if latest.get(c["id"], 0) >= hot_cutoff or f"slack:dm:{c['id']}" in open_now]
        hot_ids = {c["id"] for c in hot}
        cold = [c for c in chans if c["id"] not in hot_ids]
        ptr = db.kv_get("slack:dm_cold_ptr", 0) % max(len(cold), 1)
        n = min(cfg["dm_cold_checks_per_poll"], len(cold))
        cold_batch = (cold[ptr:] + cold[:ptr])[:n]
        db.kv_set("slack:dm_cold_ptr", ptr + n)

        items, checked = [], set()
        for ch in hot + cold_batch:
            cid = ch["id"]
            try:
                msgs = self.client.conversations_history(
                    channel=cid, limit=15, oldest=str(now - cfg["dm_lookback_days"] * 86400)
                )["messages"]
            except SlackApiError:
                continue
            checked.add(cid)
            if msgs:
                latest[cid] = max(float(m["ts"]) for m in msgs)
            pending = unreplied(msgs, self.me, include_bots=cfg["include_bots"])
            if not pending:
                continue
            last = pending[-1]
            if ch.get("is_mpim"):
                title = f"Group DM: {mpim_label(ch.get('name'))}"
            else:
                title = f"DM from {self.name(ch.get('user'))}"
            items.append(Item(
                id=f"slack:dm:{cid}", source="slack", kind="dm", priority=HIGH,
                title=title, body=format_text(last.get("text"), self.name),
                url=self.link(cid, last["ts"]), author=self.name(last.get("user")),
                ts=float(pending[0]["ts"]), updated_ts=float(last["ts"]),
                meta={"count": len(pending)},
            ))
        db.kv_set("slack:dm_latest", latest)
        preserve = {f"slack:dm:{c['id']}" for c in chans if c["id"] not in checked}
        return items, preserve

    # --- threads and mentions ---

    def thread_candidates(self, cfg, now):
        """(channel, thread_ts) -> {since, channel_name, recency}."""
        cands = {}

        def add(match, since, thread_ts):
            ch = match["channel"]
            key = (ch["id"], thread_ts)
            c = cands.setdefault(key, {"since": since, "channel": ch.get("name", ch["id"]), "recency": 0.0})
            c["since"] = min(c["since"], since)
            c["recency"] = max(c["recency"], float(match["ts"]))

        def is_dm(match):
            ch = match.get("channel", {})
            return ch.get("is_im") or ch.get("is_mpim")

        after = (date.today() - timedelta(days=cfg["thread_lookback_days"])).isoformat()
        parent_cutoff = now - cfg["parent_lookback_days"] * 86400
        for m in self._search(f"{cfg['own_query'].format(me=self.me)} after:{after}"):
            if is_dm(m):
                continue
            tts = thread_ts_from_permalink(m.get("permalink"))
            if tts:
                add(m, 0.0, tts)
            elif float(m["ts"]) >= parent_cutoff:
                add(m, 0.0, m["ts"])  # your top-level post; watch it for replies

        after = (date.today() - timedelta(days=cfg["mention_lookback_days"])).isoformat()
        for m in self._search(f"{cfg['mention_query'].format(me=self.me)} after:{after}"):
            if is_dm(m) or m.get("user") == self.me:
                continue
            tts = thread_ts_from_permalink(m.get("permalink")) or m["ts"]
            add(m, float(m["ts"]), tts)

        ranked = sorted(cands.items(), key=lambda kv: kv[1]["recency"], reverse=True)
        return dict(ranked[: cfg["max_threads"]])

    def replies(self, channel, thread_ts, max_pages=5):
        out, cursor = [], None
        for _ in range(max_pages):
            kw = {"channel": channel, "ts": thread_ts, "limit": 200}
            if cursor:
                kw["cursor"] = cursor
            resp = self.client.conversations_replies(**kw)
            out += resp.get("messages", [])
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return out

    def thread_items(self, cfg, now):
        items, preserve = [], set()
        mention_tag = f"<@{self.me}>"
        muted = {i["id"] for i in db.list_items("slack", states=("muted",))}
        for (cid, tts), info in self.thread_candidates(cfg, now).items():
            item_id = f"slack:thread:{cid}:{tts}"
            if item_id in muted:  # left the thread; don't spend API calls on it
                preserve.add(item_id)
                continue
            try:
                msgs = self.replies(cid, tts)
            except SlackApiError:
                preserve.add(item_id)
                continue
            pending = unreplied(msgs, self.me, since=info["since"], include_bots=cfg["include_bots"])
            if not pending:
                continue
            last = pending[-1]
            mentioned = any(mention_tag in (m.get("text") or "") for m in pending)
            parent = format_text(msgs[0].get("text"), self.name, limit=120) if msgs else ""
            items.append(Item(
                id=item_id, source="slack",
                kind="mention" if mentioned else "thread",
                priority=HIGH if mentioned else NORMAL,
                title=f"{'Mention' if mentioned else 'Thread'} in #{info['channel']}",
                body=format_text(last.get("text"), self.name),
                url=self.link(cid, last["ts"], tts), author=self.name(last.get("user")),
                ts=float(pending[0]["ts"]), updated_ts=float(last["ts"]),
                meta={"count": len(pending), "parent": parent, "channel": info["channel"]},
            ))
        return items, preserve


_source = None


def fetch(cfg, token):
    global _source
    if _source is None or _source.token != token:
        _source = SlackSource(token)
    scfg = cfg["slack"]
    now = time.time()
    dm, dm_keep = _source.dm_items(scfg, now)
    th, th_keep = _source.thread_items(scfg, now)
    return SyncResult(items=dm + th, preserve=dm_keep | th_keep)
