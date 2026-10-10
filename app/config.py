"""Loads config.yaml (merged over defaults) and tokens from .env."""
import copy
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DEFAULTS = {
    "server": {"host": "127.0.0.1", "port": 8765},
    "sources": ["monday", "github"],
    "poll_seconds": {"slack": 120, "github": 300, "monday": 900},
    "slack": {
        "dm_lookback_days": 7,
        "thread_lookback_days": 14,
        "mention_lookback_days": 14,
        "parent_lookback_days": 3,
        "max_threads": 80,
        "dm_hot_days": 7,
        "dm_cold_checks_per_poll": 25,
        "include_bots": False,
        "mention_query": "<@{me}>",
        "own_query": "from:<@{me}>",
    },
    "monday": {
        "board_ids": [],
        "status_column_id": None,
        "done_labels": ["Done"],
        "stale_labels": [],
        "ignored_groups": [],
        "stale_days": 7,
        "reply_lookback_days": 14,
    },
    "github": {
        "org": "UBC-Solar",
        "repos": [],
        "my_pr_no_review_days": 3,
        "team_pr_stale_days": 5,
    },
    "notifications": {"enabled": True, "max_individual": 4},
    "review": {"model": None, "timeout_minutes": 20, "claude_path": None, "max_findings": 6},
    # Two-way sync of the task sidebar (needs GOOGLE_CLIENT_ID/SECRET in .env + `python -m app.gtasks auth`).
    "google_tasks": {"enabled": True, "list": "@default", "poll_seconds": 60},
    # Encrypted snapshot for gregorybian.github.io/solar-dashboard (needs PUBLISH_PASSWORD in .env).
    "publish": {
        "enabled": True,
        "interval_seconds": 300,
        "repo": "gregorybian/gregorybian.github.io",
        "branch": "dash-data",
    },
}

TOKEN_ENV = {"slack": "SLACK_USER_TOKEN", "monday": "MONDAY_TOKEN", "github": "GITHUB_TOKEN"}


def _merge(base, override):
    out = copy.deepcopy(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], val)
        else:
            out[key] = val
    return out


def load():
    path = ROOT / "config.yaml"
    user = yaml.safe_load(path.read_text()) if path.exists() else {}
    return _merge(DEFAULTS, user)


def token(source):
    return (os.environ.get(TOKEN_ENV[source]) or "").strip() or None


CONFIG = load()
