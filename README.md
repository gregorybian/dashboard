# UBC Solar Dashboard

A local web dashboard at **http://127.0.0.1:8765** that puts everything waiting on you in one place:

- **Task sidebar** (every tab) — your own tasks with optional descriptions (**✎** to edit). **✓** archives a
  task; archived tasks sit in a collapsed list at the bottom with **Restore** and **Delete** (click twice).
  **«** collapses the sidebar; drag its right edge to resize (double-click resets). Both are remembered.
- **Todo** — every open Monday/GitHub item in one list.
- **Monday** — read-only kanban of your project boards. Projects in a watched column
  (`monday.stale_labels`, default "Working on it") with no *Update* in 7 days are flagged, and
  re-flagged weekly until someone posts. **📣 Ping owner** (click twice) posts an update on the item
  @mentioning the People-column owner(s). Pings start with "🔔 Update reminder" and don't count as
  a project update.
- **GitHub** (org `UBC-Solar`) — PRs where your review is requested; your own PRs that have failing CI,
  requested changes, approval waiting to merge, or no review after 3 days; team PRs idle 5+ days.

Items have **Done** (hidden until there's new activity) and **Mute** (hidden for good).
**🔍 Review** on any PR (click twice) runs Claude Code headless on a fresh checkout of the PR (read-only tools:
Read/Grep/Glob and `git diff/log/show/blame`) and posts **one** `COMMENT` review as you, titled
"Greg's AI Review": findings as subheadings rated 🔴 Critical / 🟠 High / 🟡 Medium / 🔵 Low (each linked
to the file and line, capped at `review.max_findings`, default 6), then a summary. It never approves or
requests changes. One review runs at a time; you get a desktop notification when it's posted or fails.
Repo mirrors are cached in `data/repos/`. Settings: `review:` in `config.yaml`.

New items trigger a desktop notification (click **Open** to jump to it).

## Portfolio page (gregorybian.github.io/solar-dashboard)
Every 5 minutes (and ~10 s after a task edit) the dashboard renders itself read-only (the same
templates and `style.css` via `templates/snapshot.html`, with every button that calls the local API
left out), encrypts that page with AES-256-GCM under `PUBLISH_PASSWORD` and
force-pushes it as a single commit to the `dash-data` branch of the portfolio repo. The portfolio page
fetches it from raw.githubusercontent.com, decrypts it in the browser and shows it full-screen, so it
looks exactly like this page. The password is never
stored anywhere public. It only pushes when something changed (or every 30 min, to keep the
"snapshot from …" stamp fresh). The status bar shows `portfolio · published Xm ago`.

1. In `.env`, set `PUBLISH_PASSWORD` (a long passphrase) and `PUBLISH_GITHUB_TOKEN` (fine-grained PAT,
   owner gregorybian, only `gregorybian.github.io`, **Contents: Read and write**).
2. `systemctl --user restart solar-dashboard`, or publish once by hand: `.venv/bin/python -m app.publish`
   (`--dry-run` writes `data/snapshot-preview.html` and checks the encryption round trip without pushing).

Changing the password just takes a restart; the page asks for the new one on its next fetch.
Settings: `publish:` in `config.yaml`.

## Setup

```bash
./install.sh            # venv + deps, creates .env, installs & starts the systemd user service
```

Then fill in `.env` and `config.yaml`, and `systemctl --user restart solar-dashboard`.

### Slack (disabled)
Slack support (unanswered DMs, @mentions, thread replies) is built but off: the UBC Solar workspace
is at its app limit, and every Slack token that can read messages has to come from an installed app.
To enable it later, get a user token as below, then add `slack` to `sources` in `config.yaml`.

1. https://api.slack.com/apps → **Create New App** → **From a manifest** → pick the UBC Solar
   workspace → paste `slack-app-manifest.yaml`.
2. **Install to Workspace** (a workspace admin may need to approve it).
3. Copy **OAuth & Permissions → User OAuth Token** (`xoxp-…`) into `SLACK_USER_TOKEN`.

All scopes are read-only. The token can read what *you* can read, so keep `.env` private.

### Monday token + boards
- Token: avatar (bottom-left) → **Developers** → **My access tokens** → copy into `MONDAY_TOKEN`.
- Board ids: the number in the board URL (`…/boards/1234567890`) → `monday.board_ids` in `config.yaml`.
- If your "finished" lane isn't called `Done`, update `monday.done_labels`.

### GitHub token
Fine-grained PAT at https://github.com/settings/personal-access-tokens/new:
resource owner **UBC-Solar**, all repositories (or the ones in `config.yaml`),
permissions **Pull requests: Read and write** (write is only used to post reviews),
**Contents: Read** (to check out PRs for review), **Commit statuses: Read**, **Actions: Read**, **Metadata: Read**.
(The org may need to approve fine-grained tokens; a classic PAT with `repo` + `read:org` also works.)

## Checking a source

```bash
.venv/bin/python -m app.sources github     # prints what would be collected; add -v for the snapshot
.venv/bin/python -m app.sources monday
```

If Slack mentions or threads come back empty, the search syntax may differ in your workspace —
override `slack.mention_query` / `slack.own_query` in `config.yaml` (e.g. `"@yourhandle"`).

## Operating

```bash
systemctl --user status solar-dashboard
journalctl --user -u solar-dashboard -f     # logs
systemctl --user restart solar-dashboard     # after editing .env / config.yaml
.venv/bin/python -m pytest -q                # tests
```

Sync errors (bad token, missing scope, rate limit) show in the status bar under the header.
State lives in `data/dashboard.db`; delete it to start fresh.

## Layout

```
app/main.py           FastAPI routes + page partials
app/jobs.py           polling scheduler (APScheduler)
app/db.py             SQLite: items, done/muted state, sync status
app/notify.py         notify-send alerts, deduped per item + activity
app/sources/*.py      slack, monday, github fetchers (pure classify logic is unit tested)
app/templates, static UI (vanilla JS, auto-refresh every 30s)
```
