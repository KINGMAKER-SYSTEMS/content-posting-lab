# Railway's Nixpacks auto-detects FastAPI and runs `uvicorn main:app`.
# This shim re-exports the real app from app.py and provides a __main__ entrypoint.
import os

from app import app  # noqa: F401

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        timeout_keep_alive=120,  # Must exceed Railway's 60s proxy keep-alive
    )
