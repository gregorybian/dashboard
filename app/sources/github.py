"""GitHub PR reminders via the GraphQL search API."""
import logging
import time
from datetime import datetime

import httpx

from ..models import HIGH, LOW, NORMAL, URGENT, Item, SyncResult

API = "https://api.github.com/graphql"
log = logging.getLogger(__name__)

PR_FIELDS = """
  ... on PullRequest {
    id number title url isDraft createdAt updatedAt reviewDecision
    repository { nameWithOwner }
    author { login }
    reviews { totalCount }
    reviewRequests(first: 10) {
      nodes { requestedReviewer { ... on User { login } ... on Team { name } } }
    }
    commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
  }
"""

SEARCH = """
query($q: String!, $cursor: String) {
  search(query: $q, type: ISSUE, first: 50, after: $cursor) {
    nodes { %s }
    pageInfo { hasNextPage endCursor }
  }
}
""" % PR_FIELDS

CI_FAILING = {"FAILURE", "ERROR"}


def _gql(token, query, variables=None):
    r = httpx.post(
        API,
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    errors = data.get("errors")
    if errors and not data.get("data"):
        raise RuntimeError(f"GitHub: {errors[0].get('message')}")
    if errors:
        # Partial result, e.g. the token can't see some CI details; those fields come back null.
        log.warning("GitHub partial errors: %s", "; ".join(e.get("message", "") for e in errors[:3]))
    return data["data"]


def _search(token, q, max_pages=4):
    out, cursor = [], None
    for _ in range(max_pages):
        res = _gql(token, SEARCH, {"q": q, "cursor": cursor})["search"]
        out += [n for n in res["nodes"] if n]
        if not res["pageInfo"]["hasNextPage"]:
            break
        cursor = res["pageInfo"]["endCursor"]
    return out


def _ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def normalize(pr):
    commits = (pr.get("commits") or {}).get("nodes") or []
    commit = (commits[0] or {}).get("commit") or {} if commits else {}
    rollup = commit.get("statusCheckRollup")
    reviewers = []
    for n in (pr.get("reviewRequests") or {}).get("nodes") or []:
        rr = (n or {}).get("requestedReviewer") or {}
        if rr.get("login") or rr.get("name"):
            reviewers.append(rr.get("login") or rr.get("name"))
    return {
        "id": pr["id"],
        "number": pr["number"],
        "title": pr["title"],
        "url": pr["url"],
        "repo": pr["repository"]["nameWithOwner"],
        "author": (pr.get("author") or {}).get("login", "ghost"),
        "draft": pr["isDraft"],
        "created": _ts(pr["createdAt"]),
        "updated": _ts(pr["updatedAt"]),
        "decision": pr.get("reviewDecision"),
        "reviews": (pr.get("reviews") or {}).get("totalCount", 0),
        "reviewers": reviewers,
        "ci": rollup["state"] if rollup else None,
    }


def classify(pr, me, review_requested, now, cfg):
    """Turn a normalized PR into a reminder, or None if nothing to do."""
    age = (now - pr["created"]) / 86400
    idle = (now - pr["updated"]) / 86400
    ref = f"{pr['repo'].split('/')[-1]}#{pr['number']}"

    def item(kind, priority, label, body):
        return Item(
            id=f"github:{pr['id']}", source="github", kind=kind, priority=priority,
            title=f"{label}: {pr['title']}", body=body, url=pr["url"], author=pr["author"],
            ts=pr["created"], updated_ts=pr["updated"],
            meta={"repo": pr["repo"], "number": pr["number"], "ci": pr["ci"],
                  "decision": pr["decision"], "draft": pr["draft"]},
        )

    if review_requested and pr["author"] != me:
        return item("review", HIGH, "Review requested", f"{ref} by {pr['author']} · open {age:.0f}d")

    if pr["author"] == me:
        if pr["ci"] in CI_FAILING:
            return item("ci_failing", URGENT, "CI failing", f"{ref} · fix the build")
        if pr["decision"] == "CHANGES_REQUESTED":
            return item("changes_requested", URGENT, "Changes requested", f"{ref} · address review")
        if pr["draft"]:
            return None
        if pr["decision"] == "APPROVED":
            return item("approved", HIGH, "Approved", f"{ref} · ready to merge")
        if pr["reviews"] == 0 and age >= cfg["my_pr_no_review_days"]:
            who = ", ".join(pr["reviewers"]) or "no reviewers assigned"
            return item("needs_review", NORMAL, "No review yet",
                        f"{ref} · waiting {age:.0f}d · nudge {who}")
        return None

    if not pr["draft"] and idle >= cfg["team_pr_stale_days"]:
        return item("stale", LOW, "Stale PR", f"{ref} by {pr['author']} · no activity {idle:.0f}d")
    return None


def fetch(cfg, token):
    gh = cfg["github"]
    org = gh["org"]
    me = _gql(token, "{ viewer { login } }")["viewer"]["login"]
    base = f"is:pr is:open archived:false org:{org}"

    requested = {p["id"]: p for p in _search(token, f"{base} review-requested:{me}")}
    mine = {p["id"]: p for p in _search(token, f"{base} author:{me}")}
    team = {}
    if gh["repos"]:
        repos = " ".join(f"repo:{org}/{r}" for r in gh["repos"])
        team = {p["id"]: p for p in _search(token, f"is:pr is:open {repos}")}

    now = time.time()
    all_prs = {k: normalize(v) for k, v in {**team, **mine, **requested}.items()}
    items = []
    for pid, pr in all_prs.items():
        it = classify(pr, me, pid in requested, now, gh)
        if it:
            items.append(it)

    snapshot = {
        "me": me,
        "prs": sorted(all_prs.values(), key=lambda p: p["updated"], reverse=True),
    }
    return SyncResult(items=items, snapshot=snapshot)
