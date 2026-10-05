"""Polling scheduler: each source fetches, syncs into the DB, then alerts."""
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from . import db, notify, publish
from .config import CONFIG, TOKEN_ENV, token
from .sources import github, monday, slack

log = logging.getLogger(__name__)
SOURCES = {"slack": slack, "github": github, "monday": monday}
scheduler = BackgroundScheduler()


def run(source):
    tok = token(source)
    if not tok:
        db.set_error(source, f"not configured: set {TOKEN_ENV[source]} in .env")
        return
    first_sync = not (db.status(source) or {}).get("last_ok")
    try:
        res = SOURCES[source].fetch(CONFIG, tok)
    except Exception as e:  # keep the last good data; show the error in the UI
        log.exception("%s sync failed", source)
        db.set_error(source, f"{type(e).__name__}: {e}")
        return
    db.sync_source(source, res.items, res.preserve)
    if res.snapshot is not None:
        db.kv_set(f"snapshot:{source}", res.snapshot)
    db.set_ok(source)
    notify.notify_new(source, first_sync)


def start():
    for source in CONFIG["sources"]:
        seconds = CONFIG["poll_seconds"][source]
        scheduler.add_job(
            run, "interval", seconds=seconds, args=[source], id=source,
            next_run_time=datetime.now(), max_instances=1, coalesce=True,
        )
    if publish.enabled():
        scheduler.add_job(
            publish.publish, "interval", seconds=CONFIG["publish"]["interval_seconds"], id="publish",
            next_run_time=datetime.now() + timedelta(seconds=30), max_instances=1, coalesce=True,
        )
    scheduler.start()


def refresh(source=None):
    for s in [source] if source else CONFIG["sources"]:
        job = scheduler.get_job(s)
        if job:
            job.modify(next_run_time=datetime.now())


def publish_soon():
    """Publish shortly after a task edit; a burst of edits collapses into one push."""
    job = scheduler.get_job("publish")
    if job:
        job.modify(next_run_time=datetime.now() + timedelta(seconds=10))
