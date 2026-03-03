# Railway's Nixpacks auto-detects FastAPI and runs `uvicorn main:app`.
# This shim re-exports the real app from app.py.
from app import app  # noqa: F401
