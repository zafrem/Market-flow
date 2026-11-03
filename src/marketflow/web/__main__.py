"""python -m marketflow.web  -> serve the API + dashboard (binds to localhost by default)."""
import argparse

import uvicorn

from ..settings import load_settings
from .app import create_app

ap = argparse.ArgumentParser()
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=8000)
a = ap.parse_args()
uvicorn.run(create_app(load_settings()), host=a.host, port=a.port)
