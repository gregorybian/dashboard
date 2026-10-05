"""Dry run: python -m app.sources <slack|github|monday> — prints what would be collected."""
import json
import sys

from .. import db
from ..config import CONFIG, TOKEN_ENV, token
from . import github, monday, slack

SOURCES = {"slack": slack, "github": github, "monday": monday}

name = sys.argv[1] if len(sys.argv) > 1 else ""
if name not in SOURCES:
    sys.exit(f"usage: python -m app.sources {{{'|'.join(SOURCES)}}}")
tok = token(name)
if not tok:
    sys.exit(f"{TOKEN_ENV[name]} is not set in .env")
db.init()  # slack keeps small cursors in the kv table
res = SOURCES[name].fetch(CONFIG, tok)
for it in sorted(res.items, key=lambda i: -i.priority):
    print(f"[p{it.priority} {it.kind}] {it.title}\n    {it.body}\n    {it.url}")
print(f"\n{len(res.items)} items, {len(res.preserve)} preserved")
if res.snapshot and "-v" in sys.argv:
    print(json.dumps(res.snapshot, indent=2)[:5000])
