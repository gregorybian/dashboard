"""Linux desktop notifications for newly opened items (deduped per id+activity)."""
import logging
import shutil
import subprocess
import threading

from . import db
from .config import CONFIG

log = logging.getLogger(__name__)
LABELS = {"slack": "Slack", "github": "GitHub", "monday": "Monday"}


def dashboard_url():
    s = CONFIG["server"]
    return f"http://{s['host']}:{s['port']}"


def send(title, body, url, urgency="normal"):
    if not shutil.which("notify-send"):
        return

    def run():
        cmd = ["notify-send", "-a", "UBC Solar Dashboard", "-u", urgency,
               "-A", "open=Open", "--wait", title, body]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=3600).stdout.strip()
            if out == "open":
                subprocess.Popen(["xdg-open", url or dashboard_url()])
        except (subprocess.SubprocessError, OSError) as e:
            log.warning("notify-send failed: %s", e)

    threading.Thread(target=run, daemon=True).start()


def notify_new(source, first_sync):
    rows = db.unnotified(source)
    if not rows:
        return
    db.mark_notified([db.notify_key(r) for r in rows])
    cfg = CONFIG["notifications"]
    if not cfg["enabled"]:
        return
    label = LABELS.get(source, source)
    if first_sync or len(rows) > cfg["max_individual"]:
        send(f"{label}: {len(rows)} open items", ", ".join(r["title"] for r in rows[:5]), dashboard_url())
        return
    for r in rows:
        urgency = "critical" if r["priority"] >= 3 else "normal"
        send(f"{label}: {r['title']}", r["body"] or "", r["url"], urgency)
