from app.sources.github import classify

CFG = {"my_pr_no_review_days": 3, "team_pr_stale_days": 5}
NOW = 1_000_000.0
DAY = 86400


def pr(**kw):
    base = dict(id="P1", number=7, title="Fix CAN", url="u", repo="UBC-Solar/sunlink", author="other",
                draft=False, created=NOW - DAY, updated=NOW - DAY, decision=None, reviews=0,
                reviewers=[], ci="SUCCESS")
    return {**base, **kw}


def kind(p, requested=False):
    r = classify(p, "me", requested, NOW, CFG)
    return r.kind if r else None


def test_review_request():
    assert kind(pr(), requested=True) == "review"


def test_my_pr_states():
    assert kind(pr(author="me", ci="FAILURE")) == "ci_failing"
    assert kind(pr(author="me", decision="CHANGES_REQUESTED")) == "changes_requested"
    assert kind(pr(author="me", decision="APPROVED")) == "approved"
    assert kind(pr(author="me")) is None  # waiting, but not long enough
    assert kind(pr(author="me", created=NOW - 4 * DAY)) == "needs_review"
    assert kind(pr(author="me", draft=True, created=NOW - 9 * DAY)) is None


def test_team_pr_staleness():
    assert kind(pr()) is None
    assert kind(pr(updated=NOW - 6 * DAY)) == "stale"
    assert kind(pr(updated=NOW - 6 * DAY, draft=True)) is None


def test_own_pr_is_never_a_review_request():
    assert kind(pr(author="me", decision="APPROVED"), requested=True) == "approved"


def test_normalize_tolerates_null_fields():
    from app.sources.github import normalize
    raw = {"id": "P", "number": 1, "title": "t", "url": "u", "repository": {"nameWithOwner": "o/r"},
           "author": None, "isDraft": False, "createdAt": "2026-01-01T00:00:00Z",
           "updatedAt": "2026-01-02T00:00:00Z", "reviewDecision": None,
           "reviews": None, "reviewRequests": None, "commits": {"nodes": [{"commit": None}]}}
    pr = normalize(raw)
    assert pr["ci"] is None and pr["reviews"] == 0 and pr["reviewers"] == [] and pr["author"] == "ghost"
