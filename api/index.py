"""Vercel Python entrypoint — re-exports the FastAPI ASGI app unchanged.

Vercel's Python runtime serves any `app` object it finds here; all real
routes/logic live in backend/app.py, run identically to local uvicorn.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.app import app  # noqa: E402
