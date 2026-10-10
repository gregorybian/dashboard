"""Two-way sync between the task sidebar and one Google Tasks list.

Local edits bump a row's `dirty` counter (deletes leave a tombstone); each sync first pushes dirty
rows to Google, then pulls the whole list:
- a task changed in Google (its `updated` stamp moved) overwrites the local row, unless the row has
  an unpushed local edit, in which case local wins and goes out on the next push;
- a new, not-completed task in Google is imported;
- a linked task missing from Google was deleted there: active rows are deleted, archived rows are
  kept (Google purges old completed tasks; the local archive shouldn't lose them);
- completed in Google <-> archived here; due date <-> due (Google keeps only the date).

One-time login: `python -m app.gtasks auth` (see README). `python -m app.gtasks sync` syncs once.
"""
import base64
import hashlib
import http.server
import json
import logging
import os
import secrets
import time
import urllib.parse
import webbrowser
from datetime import datetime

import httpx

from . import db
from .config import CONFIG, ROOT

log = logging.getLogger(__name__)
API = "https://tasks.googleapis.com/tasks/v1"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/tasks"
TOKEN_PATH = ROOT / "data" / "google_tasks_token.json"
STATUS = "google tasks"  # sync_status key, shown in the status bar

_access = {"token": None, "expires": 0.0}
_list_id = {}


class AuthError(RuntimeError):
    pass


def client():
    cid = (os.environ.get("GOOGLE_CLIENT_ID") or "").strip()
    secret = (os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()
    return (cid, secret) if cid and secret else None


def refresh_token():
    try:
        return json.loads(TOKEN_PATH.read_text())["refresh_token"]
    except (FileNotFoundError, KeyError, ValueError):
        return None


def enabled():
    return bool(CONFIG["google_tasks"]["enabled"] and client() and refresh_token())


def _access_token():
    if _access["token"] and time.time() < _access["expires"] - 60:
        return _access["token"]
    cid, secret = client()
    r = httpx.post(TOKEN_URL, data={
        "client_id": cid, "client_secret": secret,
        "refresh_token": refresh_token(), "grant_type": "refresh_token",
    }, timeout=30)
    if r.status_code in (400, 401):
        raise AuthError(f"Google login expired or revoked ({r.json().get('error')}): "
                        "run .venv/bin/python -m app.gtasks auth")
    r.raise_for_status()
    data = r.json()
    _access.update(token=data["access_token"], expires=time.time() + data.get("expires_in", 3600))
    return _access["token"]


def _api(method, path, **kw):
    r = httpx.request(method, API + path, headers={"Authorization": f"Bearer {_access_token()}"},
                      timeout=30, **kw)
    if r.status_code == 401:  # access token revoked early; retry once with a fresh one
        _access["token"] = None
        r = httpx.request(method, API + path, headers={"Authorization": f"Bearer {_access_token()}"},
                          timeout=30, **kw)
    return r


def _check(r):
    if r.is_error:
        try:
            msg = r.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            msg = r.text[:200]
        raise RuntimeError(f"Google Tasks {r.status_code}: {msg}")
    return r.json() if r.content else None


def list_id():
    """The configured list's id: "@default" (the "My Tasks" list) or a list title, created if missing."""
    name = CONFIG["google_tasks"]["list"]
    if name == "@default":
        return name
    if name not in _list_id:
        lists, page = [], None
        while True:
            data = _check(_api("GET", "/users/@me/lists", params={"maxResults": 100, "pageToken": page}))
            lists += data.get("items", [])
            page = data.get("nextPageToken")
            if not page:
                break
        match = next((l for l in lists if l["title"] == name), None)
        if not match:
            match = _check(_api("POST", "/users/@me/lists", json={"title": name}))
            log.info("created Google Tasks list %r", name)
        _list_id[name] = match["id"]
    return _list_id[name]


def _ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() if iso else None


def _iso(ts):
    return datetime.fromtimestamp(ts).astimezone().isoformat()


def _body(row):
    # Google stores only the date part of `due`, as midnight UTC.
    body = {"title": row["text"], "notes": row["description"] or "",
            "due": f"{row['due']}T00:00:00.000Z" if row["due"] else None}
    if row["archived"]:
        body.update(status="completed", completed=_iso(row["archived"]))
    else:
        body.update(status="needsAction", completed=None)
    return body


def push(lid):
    pushed = 0
    for row in db.todo_dirty():
        if row["deleted"]:
            r = _api("DELETE", f"/lists/{lid}/tasks/{row['google_id']}")
            if r.status_code not in (404, 410):
                _check(r)
            db.todo_purge(row["id"], row["dirty"])
        else:
            task = None
            if row["google_id"]:
                r = _api("PATCH", f"/lists/{lid}/tasks/{row['google_id']}", json=_body(row))
                # Deleted in Google but edited here since: the edit is newer, so re-create it.
                if r.status_code not in (404, 410):
                    task = _check(r)
            if task is None:
                task = _check(_api("POST", f"/lists/{lid}/tasks", json=_body(row)))
            db.todo_pushed(row["id"], row["dirty"], task["id"], task["updated"])
        pushed += 1
    return pushed


def fetch(lid):
    tasks, page = [], None
    while True:
        data = _check(_api("GET", f"/lists/{lid}/tasks", params={
            "maxResults": 100, "showCompleted": "true", "showHidden": "true", "pageToken": page,
        }))
        tasks += data.get("items", [])
        page = data.get("nextPageToken")
        if not page:
            return tasks


def pull(lid):
    remote = {t["id"]: t for t in fetch(lid) if not t.get("deleted")}
    local = {r["google_id"]: r for r in db.todo_linked()}
    changed = 0
    for gid, t in remote.items():
        title = (t.get("title") or "").strip()
        notes = (t.get("notes") or "").strip() or None
        due = (t.get("due") or "")[:10] or None
        row = local.get(gid)
        if row is None:
            # Only bring over open tasks; Google's completed history would flood the archive.
            if title and t.get("status") != "completed":
                db.todo_import(gid, title[:500], notes and notes[:5000], due, t["updated"])
                changed += 1
        elif not row["dirty"]:
            archived = (row["archived"] or _ts(t.get("completed")) or time.time()) \
                if t.get("status") == "completed" else None
            new = (title[:500] or row["text"], notes and notes[:5000], archived, due)
            # Also re-apply when only the content differs: fields added after a task was first
            # synced (like `due`) would otherwise wait for its next edit in Google.
            if t["updated"] != row["g_updated"] or new != (row["text"], row["description"], row["archived"], row["due"]):
                changed += db.todo_pull(row["id"], *new, t["updated"])
    for gid, row in local.items():
        if gid not in remote and not row["dirty"]:
            if row["archived"]:
                db.todo_unlink(row["id"])
            else:
                db.todo_purge(row["id"])
                changed += 1
    return changed


def sync():
    """Push local edits, then pull Google's. Returns True if the sidebar changed."""
    if not enabled():
        return False
    try:
        lid = list_id()
        pushed = push(lid)
        pulled = pull(lid)
    except Exception as e:
        log.exception("Google Tasks sync failed")
        db.set_error(STATUS, f"{type(e).__name__}: {e}")
        return False
    db.set_ok(STATUS)
    if pushed or pulled:
        log.info("Google Tasks: pushed %d, pulled %d", pushed, pulled)
    return bool(pulled)


def authorize():
    """Installed-app OAuth (loopback redirect + PKCE); saves the refresh token to TOKEN_PATH."""
    if not client():
        raise SystemExit("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env first (see README).")
    cid, secret = client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [None])[0] != state:
                self.send_error(400, "bad state")
                return
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Signed in. You can close this tab." if "code" in got else f"Failed: {got.get('error')}"
            self.wfile.write(f"<p style='font:16px sans-serif'>Solar dashboard: {msg}</p>".encode())

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    print(f"Opening the Google sign-in page. If it doesn't open, visit:\n\n{url}\n", flush=True)
    webbrowser.open(url)
    server.timeout = 5
    deadline = time.time() + 300
    while not got and time.time() < deadline:  # stray requests (favicon) are rejected by `state`
        server.handle_request()
    server.server_close()
    if "code" not in got:
        raise SystemExit(f"Sign-in failed: {got.get('error', 'timed out')}")
    r = httpx.post(TOKEN_URL, data={
        "client_id": cid, "client_secret": secret, "code": got["code"], "code_verifier": verifier,
        "redirect_uri": redirect, "grant_type": "authorization_code",
    }, timeout=30)
    _check(r)
    token = r.json().get("refresh_token")
    if not token:
        raise SystemExit("Google didn't return a refresh token; remove the app's access at "
                         "myaccount.google.com/permissions and try again.")
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.touch(mode=0o600)
    TOKEN_PATH.write_text(json.dumps({"refresh_token": token}))
    print(f"Saved {TOKEN_PATH.relative_to(ROOT)}.")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "auth":
        authorize()
        cmd = "sync"
    if cmd != "sync":
        sys.exit("usage: python -m app.gtasks <auth|sync>")
    db.init()
    if not enabled():
        sys.exit("Not configured: needs GOOGLE_CLIENT_ID/SECRET in .env and `python -m app.gtasks auth`.")
    sync()
    st = db.status(STATUS) or {}
    if st.get("last_error"):
        sys.exit(st["last_error"])
    print(f"synced with list {CONFIG['google_tasks']['list']!r}: "
          f"{len(db.todo_list())} active, {len(db.todo_list(archived=True))} archived")
