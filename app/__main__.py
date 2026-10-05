import uvicorn

from .config import CONFIG

uvicorn.run("app.main:app", host=CONFIG["server"]["host"], port=CONFIG["server"]["port"])
