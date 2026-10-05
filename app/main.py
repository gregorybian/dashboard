import logging
import time
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, jobs, publish, review
from .config import token
from .sources import monday
from .config import CONFIG, ROOT

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
APP_DIR = ROOT / "app"
SOURCES = tuple(CONFIG["sources"])
TABS = ("todo",) + SOURCES


@asynccontextmanager
async def lifespan(app):
    db.init()
    db.fail_interrupted_reviews()
    jobs.start()
    yield
    if jobs.scheduler.running:
        jobs.scheduler.shutdown(wait=False)


app = FastAPI(title="UBC Solar Dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")


def ago(ts):
    if not ts:
        return "never"
    s = max(0, time.time() - ts)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{int(s // size)}{unit}"
    return "now"


templates.env.filters["ago"] = ago
templates.env.globals["readonly"] = False  # True when publish.py renders the portfolio snapshot
# Changes whenever the UI files change; open tabs compare it and reload themselves.
templates.env.globals["version"] = str(int(max(
    p.stat().st_mtime for d in ("static", "templates") for p in (APP_DIR / d).iterdir()
)))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"tabs": TABS})


# Page contexts, shared with publish.py, which renders the same templates read-only.
def header_context():
    counts = {s: n for s, n in db.open_counts().items() if s in SOURCES}
    return {
        "counts": counts, "total": sum(counts.values()),
        "status": db.status(), "sources": SOURCES, "publishing": publish.enabled(),
    }


def tasks_context():
    return {"todos": db.todo_list(), "archived": db.todo_list(archived=True)}


def tab_context(tab):
    ctx = {"tab": tab, "reviews": db.latest_reviews()}
    if tab == "todo":
        ctx["items"] = [i for i in db.list_items() if i["source"] in SOURCES]
    else:
        ctx["items"] = db.list_items(tab)
        ctx["closed"] = db.list_items(tab, states=("done", "resolved", "muted"), since=time.time() - 86400)
        ctx["snapshot"] = db.kv_get(f"snapshot:{tab}")
    return ctx


@app.get("/partials/header", response_class=HTMLResponse)
def header(request: Request):
    return templates.TemplateResponse(request, "header.html", header_context())


@app.get("/partials/tasks", response_class=HTMLResponse)
def tasks(request: Request):
    return templates.TemplateResponse(request, "tasks.html", tasks_context())


@app.get("/partials/{tab}", response_class=HTMLResponse)
def partial(request: Request, tab: str):
    if tab not in TABS:
        raise HTTPException(404)
    return templates.TemplateResponse(request, f"{tab}.html", tab_context(tab))


@app.post("/api/items/{action}/{item_id:path}", status_code=204)
def item_action(action: str, item_id: str):
    if action not in ("done", "mute", "reopen"):
        raise HTTPException(400)
    db.set_state(item_id, action)
    return Response(status_code=204)


@app.post("/api/todos", status_code=204)
def todo_add(text: str = Body(), description: str = Body("")):
    text = text.strip()
    if not text:
        raise HTTPException(400, "empty task")
    db.todo_add(text[:500], description.strip()[:5000])
    jobs.publish_soon()
    return Response(status_code=204)


@app.put("/api/todos/{todo_id}/description", status_code=204)
def todo_describe(todo_id: int, description: str = Body(embed=True)):
    db.todo_describe(todo_id, description.strip()[:5000])
    jobs.publish_soon()
    return Response(status_code=204)


@app.post("/api/todos/{todo_id}/{action}", status_code=204)
def todo_action(todo_id: int, action: str):
    if action not in ("archive", "restore", "delete"):
        raise HTTPException(400)
    db.todo_set(todo_id, action)
    jobs.publish_soon()
    return Response(status_code=204)


@app.post("/api/review", status_code=202)
def start_review(repo: str, number: int):
    org = CONFIG["github"]["org"]
    if not repo.startswith(f"{org}/") or "github" not in CONFIG["sources"]:
        raise HTTPException(400, "unknown repo")
    if not review.start(repo, number):
        raise HTTPException(409, "a review is already running for this PR")
    return Response(status_code=202)


@app.post("/api/monday/ping")
def monday_ping(item_id: int):
    tok = token("monday")
    if not tok or "monday" not in CONFIG["sources"]:
        raise HTTPException(400, "Monday isn't configured")
    try:
        names = monday.ping(CONFIG, tok, item_id)
    except Exception as e:
        raise HTTPException(502, str(e)[:200])
    jobs.refresh("monday")
    return {"message": f"pinged {', '.join(names)}"}


@app.post("/api/refresh", status_code=204)
def refresh(source: str | None = None):
    if source and source not in SOURCES:
        raise HTTPException(400)
    jobs.refresh(source)
    return Response(status_code=204)


@app.get("/api/items")
def api_items(source: str | None = None):
    return db.list_items(source)
