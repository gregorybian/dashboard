from app.review import build_body

REPO, HEAD = "UBC-Solar/firmware_v4", "abc123"


def f(sev, title, line=1):
    return {"severity": sev, "title": title, "path": "src/can.c", "line": line, "details": f"{title} details"}


def test_single_comment_layout_sorted_by_severity():
    body = build_body([f("Low", "nit"), f("Critical", "overflow", 42), f("Medium", "leak")],
                      "Adds CAN stuff. Fix the overflow.", REPO, HEAD)
    assert body.startswith("## Greg's AI Review")
    heads = [l for l in body.splitlines() if l.startswith("### ")]
    assert heads == ["### 🔴 Critical — overflow", "### 🟡 Medium — leak", "### 🔵 Low — nit", "### Summary"]
    assert f"https://github.com/{REPO}/blob/{HEAD}/src/can.c#L42" in body
    assert body.rstrip().endswith("Adds CAN stuff. Fix the overflow.")


def test_no_findings():
    body = build_body([], "Looks good.", REPO, HEAD)
    assert "No issues found." in body and "### Summary" in body
