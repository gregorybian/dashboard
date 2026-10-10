# solar_dashboard

Local FastAPI dashboard (http://127.0.0.1:8765) for Greg's UBC Solar work: a personal task sidebar,
plus Monday/GitHub items, and an encrypted read-only snapshot published to the portfolio site.
README.md covers features, setup and operating; this file holds what isn't obvious from it.

## Running / testing

- Runs as a systemd **user** service: `systemctl --user restart solar-dashboard` after any code,
  `.env` or `config.yaml` change. It doesn't auto-reload. Open tabs reload themselves when the
  templates/static mtimes change (`version` in `app/main.py`).
- Tests: `.venv/bin/python -m pytest -q` (temp DB per test via `tests/conftest.py`). The routes are
  tested with `TestClient(main.app)` and no lifespan, so the scheduler never starts.
- No `sqlite3` CLI on this machine; inspect `data/dashboard.db` with `.venv/bin/python -c "import sqlite3 ..."`.
- `data/` and `.env` are gitignored and private (chmod 600). Never print `.env` values or
  `data/google_tasks_token.json`; show key names only.

### Previewing UI changes without touching real services
Don't run the app on the real DB to try things out: the lifespan starts jobs that sync with Google
Tasks, poll Monday/GitHub and publish to the portfolio. Instead, copy `data/dashboard.db` to the
scratchpad, then in a script set `db.DB_PATH` to the copy, set `jobs.start = lambda: None`, seed
data and `uvicorn.run(main.app, port=8799)`. Screenshot with
`google-chrome --headless=new --window-size=1400,900 --virtual-time-budget=4000 --screenshot=out.png URL`.
The sidebar is about 340px wide, so check that form controls don't squeeze the task input.

## Task sidebar (`todos` table)

Columns: `text`, `description`, `created`, `archived` (null = active), `due` (YYYY-MM-DD or null),
plus sync fields `google_id`, `g_updated`, `dirty`, `deleted`. `db.init()` adds missing columns in
place. Existing rows start with `dirty=1`, so they get pushed on the first sync.

- `todo_list()` returns only `TODO_COLUMNS` (no sync fields), so publish fingerprints don't change on
  sync bookkeeping. Active order: dated tasks first, soonest due on top, then undated by created.
- Every local edit (`todo_describe`, `todo_set_due`, `todo_set`) bumps `dirty`. Deleting a linked
  task leaves a tombstone (`deleted=1`) until the delete reaches Google; unlinked rows are deleted
  immediately.
- API handlers call `jobs.tasks_changed()`, which runs the Google sync now and publishes about 10s later.
  APScheduler *skips* a run requested while one is in progress (a sync takes about 5s because of the
  ~700-task list), so `tasks_changed()` also sets `_tasks_again` and `sync_tasks()` loops once more.
  Without that, an edit made during a sync waited up to 60s (seen 2026-10-09). A dashboard edit
  reaches Google in about 7s.
- Due dates: the API takes `""` (clear) or `YYYY-MM-DD` (`parse_due`, 400 otherwise). The UI is a
  📅 chip with an invisible `<input type=date>` laid over it; a click calls `showPicker()`, and the
  picker's Clear removes the date. The `due_label` filter gives Today/Tomorrow/Yesterday, a weekday
  within 7 days, else "Mon D". CSS classes: overdue (red), today (orange), soon (blue).

## Google Tasks two-way sync (`app/gtasks.py`, added 2026-10-06)

- No Google client libraries: plain `httpx` against `tasks.googleapis.com/tasks/v1`. OAuth is the
  installed-app loopback flow with PKCE (`python -m app.gtasks auth`, which opens the browser,
  catches the redirect on a random 127.0.0.1 port, saves the refresh token to
  `data/google_tasks_token.json`, then syncs once). `python -m app.gtasks sync` syncs once.
- Enabled when `google_tasks.enabled` is set, `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` are in
  `.env` and the token file exists. The scheduler job (`id="gtasks"`, every `poll_seconds`=60) is
  only added at startup, so restart the service after first sign-in. Status bar key: `"google tasks"`.
- Algorithm per sync: **push** dirty rows (insert / patch / delete), then **pull** the whole list.
  - Mapping: title ↔ text, notes ↔ description, completed ↔ archived, `due` ↔ due (Google keeps
    only the date and stores it as `YYYY-MM-DDT00:00:00.000Z`).
  - Pull re-applies a non-dirty row when Google's `updated` changed **or** any mapped field
    differs. This self-heals fields added after a task was first synced: the 5 tasks imported before
    due dates existed got their Google due dates this way on 2026-10-09.
  - Conflicts: an unpushed local edit wins (`todo_pull` only updates `where dirty=0`). Pushes clear
    `dirty` only if it still equals the value read, so an edit made mid-request isn't lost.
  - Patching a task deleted in Google (404/410) re-creates it, because the local edit is newer.
  - New Google tasks are imported only if not completed, so old completed history doesn't flood the
    archive.
  - A linked task missing from Google: active rows are deleted locally; archived rows are only
    unlinked (Google purges old completed tasks, and the local archive should keep them).
- Tests: `tests/test_gtasks.py` uses an in-memory `FakeGoogle` patched over `gtasks._api`.

### Current Google setup state (as of 2026-10-09)
- Google Cloud project "Google Tasks Project": Tasks API enabled, Desktop-app OAuth client in `.env`.
- The OAuth consent screen is still in **Testing** (branding unfinished), with Greg's Gmail added
  as a test user. Testing-mode refresh tokens **expire after 7 days**: the status bar then shows
  "Google login expired…" and the fix is re-running `python -m app.gtasks auth`. Publishing the app
  (name + support/developer emails, no logo, so no verification) removes the expiry.
  "Access blocked … Error 403: access_denied" at sign-in means the account isn't a test user.
- Syncs with "My Tasks" (`list: "@default"`). That list has about 700 tasks (mostly old completed),
  so each pull is about 7 paged GETs a minute (about 10k requests/day, under the 50k/day default
  quota). Possible improvements: `updatedMin` incremental pulls, or a dedicated list via
  `google_tasks.list: "Solar"`.

## Gotchas hit while building this
- Running `auth` from a background shell: pipe output through nothing, or use `python -u`; the URL
  print is `flush=True`, but a `| sed` pipe still buffers it.
- `pkill -f 'app.gtasks auth'` also matches (and kills) the shell running the pkill. Use a pattern
  like `pgrep -f 'm app.gtask[s]'`.
- When adding a secret, add the placeholder to the real `.env` too, not just `.env.example`.
