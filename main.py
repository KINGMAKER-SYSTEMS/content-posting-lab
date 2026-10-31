# Railway's Nixpacks auto-detects FastAPI and runs `uvicorn main:app`.
# This shim re-exports the real app from app.py and provides a __main__ entrypoint.
import os

from app import app  # noqa: F401

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    # Railway terminates public TLS before forwarding to this private service.
    # Respect an explicit proxy allowlist (including an empty one). Other
    # runtimes retain Uvicorn's own default trust boundary.
    forwarded_allow_ips = os.getenv("FORWARDED_ALLOW_IPS")
    if forwarded_allow_ips is None and os.getenv("RAILWAY_ENVIRONMENT_ID", "").strip():
        forwarded_allow_ips = "*"
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        workers=1,  # Source-store startup reconciliation requires one service process.
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips,
        timeout_keep_alive=120,  # Must exceed Railway's 60s proxy keep-alive
    )
