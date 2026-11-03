"""python -m marketflow.web  -> serve the API + dashboard (binds to localhost by default)."""
import argparse

import uvicorn

from ..settings import load_settings
from .app import create_app
from .serve import uvicorn_kwargs

ap = argparse.ArgumentParser()
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=8000)
a = ap.parse_args()
settings = load_settings()
uvicorn.run(create_app(settings), **uvicorn_kwargs(settings, a.host, a.port))
