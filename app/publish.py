"""Publish an encrypted dashboard snapshot for gregorybian.github.io/solar-dashboard.

The snapshot is the dashboard itself: the same templates and style.css rendered read-only into one
self-contained HTML page (snapshot.html), so the portfolio shows exactly what the local page does,
minus the buttons that call the local API. It is gzipped, then encrypted with
AES-256-GCM under a key derived from PUBLISH_PASSWORD (PBKDF2-HMAC-SHA256). The result is
force-pushed as a single orphan commit to a branch of the portfolio repo, so the branch never
grows history. The portfolio page fetches it from raw.githubusercontent.com and decrypts it
in the browser with WebCrypto.
"""
import base64
import gzip
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from . import db
from .config import CONFIG, ROOT

log = logging.getLogger(__name__)
ITERATIONS = 600_000
FILENAME = "data.enc.json"
MAX_AGE = 30 * 60  # re-push an unchanged snapshot this often, so the page's "as of" stays fresh


def password():
    return (os.environ.get("PUBLISH_PASSWORD") or "").strip() or None


def push_token():
    return (os.environ.get("PUBLISH_GITHUB_TOKEN") or "").strip() or None


def enabled():
    return bool(CONFIG["publish"]["enabled"] and password())


def build_payload():
    sources = CONFIG["sources"]
    return {
        "generated_at": time.time(),
        "sources": sources,
        "todos": db.todo_list(),
        "archived": db.todo_list(archived=True),
        "items": [i for i in db.list_items() if i["source"] in sources],
        "snapshots": {s: db.kv_get(f"snapshot:{s}") for s in sources},
        "status": {s: v for s, v in db.status().items() if s in sources},
    }


def render_html(generated_at):
    from .main import SOURCES, TABS, header_context, tab_context, tasks_context, templates  # main imports us

    env = templates.env
    render = lambda name, ctx: env.get_template(name).render(readonly=True, **ctx)
    header = header_context()
    tasks = tasks_context()
    static = ROOT / "app" / "static"
    return render("snapshot.html", {
        "css": (static / "style.css").read_text(),
        "sidebar_js": (static / "sidebar.js").read_text(),
        "tabs": TABS,
        "tab_counts": {"todo": header["total"], **{s: header["counts"].get(s, 0) for s in SOURCES}},
        "header_html": render("header.html", header),
        "tasks_html": render("tasks.html", tasks),
        "todo_count": len(tasks["todos"]),
        "panes": {t: render(f"{t}.html", tab_context(t)) for t in TABS},
        "generated_at": generated_at,
    })


def _b64(b):
    return base64.b64encode(b).decode()


def _key(pw, salt, iterations):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive(pw.encode())


def encrypt(payload, pw, iterations=ITERATIONS):
    salt, iv = os.urandom(16), os.urandom(12)
    plain = gzip.compress(json.dumps(payload, separators=(",", ":")).encode())
    ct = AESGCM(_key(pw, salt, iterations)).encrypt(iv, plain, None)  # ciphertext || 16-byte tag
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": iterations, "zip": "gzip",
            "salt": _b64(salt), "iv": _b64(iv), "ct": _b64(ct)}


def decrypt(blob, pw):
    """Inverse of encrypt (the browser does the same with WebCrypto). Raises on a wrong password."""
    d = base64.b64decode
    key = _key(pw, d(blob["salt"]), blob["iter"])
    plain = AESGCM(key).decrypt(d(blob["iv"]), d(blob["ct"]), None)
    return json.loads(gzip.decompress(plain))


def _fingerprint(payload):
    body = {k: v for k, v in payload.items() if k != "generated_at"}
    # Sync times change every poll; only the content matters for "has anything changed".
    body["status"] = {s: (v or {}).get("last_error") for s, v in payload["status"].items()}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def _git(cwd, *args, stdin=None, env=None):
    r = subprocess.run(["git", *args], cwd=cwd, input=stdin, capture_output=True, text=True,
                       env={**os.environ, **(env or {})}, timeout=120)
    if r.returncode:
        raise RuntimeError(f"git {args[0]} failed: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def push(blob):
    """Force-push `blob` as the only file of a fresh orphan commit on the publish branch."""
    cfg, tok = CONFIG["publish"], push_token()
    if not tok:
        raise RuntimeError("PUBLISH_GITHUB_TOKEN is not set in .env")
    url = f"https://x-access-token:{tok}@github.com/{cfg['repo']}.git"
    who = {"GIT_AUTHOR_NAME": "solar-dashboard", "GIT_AUTHOR_EMAIL": "solar-dashboard@localhost",
           "GIT_COMMITTER_NAME": "solar-dashboard", "GIT_COMMITTER_EMAIL": "solar-dashboard@localhost"}
    with tempfile.TemporaryDirectory() as tmp:
        _git(tmp, "init", "-q")
        sha = _git(tmp, "hash-object", "-w", "--stdin", stdin=json.dumps(blob))
        tree = _git(tmp, "mktree", stdin=f"100644 blob {sha}\t{FILENAME}\n")
        commit = _git(tmp, "commit-tree", tree, "-m", "dashboard snapshot", env=who)
        try:
            _git(tmp, "-c", "credential.helper=", "push", "-q", "-f", url,
                 f"{commit}:refs/heads/{cfg['branch']}")
        except RuntimeError as e:
            raise RuntimeError(str(e).replace(tok, "***")) from None


def publish(force=False):
    if not enabled():
        return False
    try:
        payload = build_payload()
        fp, last = _fingerprint(payload), db.kv_get("publish:last") or {}
        if not force and fp == last.get("hash") and time.time() - last.get("at", 0) < MAX_AGE:
            return False
        now = time.time()
        push(encrypt({"v": 2, "generated_at": now, "html": render_html(now)}, password()))
    except Exception as e:
        log.exception("publish failed")
        db.set_error("publish", f"{type(e).__name__}: {e}")
        return False
    db.kv_set("publish:last", {"hash": fp, "at": time.time()})
    db.set_ok("publish")
    return True


if __name__ == "__main__":
    import sys
    if not password():
        sys.exit("PUBLISH_PASSWORD is not set in .env")
    db.init()
    if "--dry-run" in sys.argv:
        html = render_html(time.time())
        blob = encrypt({"v": 2, "html": html}, password())
        out = ROOT / "data" / "snapshot-preview.html"
        out.write_text(html)
        print(f"encrypted snapshot: {len(json.dumps(blob))} bytes; round-trip ok: "
              f"{decrypt(blob, password())['html'] == html}; preview: {out}")
    elif publish(force=True):
        print(f"published to {CONFIG['publish']['repo']}@{CONFIG['publish']['branch']}")
    else:
        sys.exit(f"publish failed: {(db.status('publish') or {}).get('last_error')}")
