"""Render the lab's local generation jobs on this machine's ComfyUI server.

Run this next to ComfyUI (for example on the studio Mac). It only makes outbound
requests: it asks the lab for the next job, renders it on the local server and
uploads the finished video. Nothing on this machine is exposed.

    CONTENT_LAB_URL=https://lab.example.com \\
    LOCAL_WORKER_KEY=... \\
    python scripts/local_comfy_worker.py

Environment:
    CONTENT_LAB_URL     Base URL of the lab (required).
    LOCAL_WORKER_KEY    Shared secret, the same value set on the lab (required).
    COMFYUI_SERVER_URL  Local ComfyUI server (default http://127.0.0.1:8188).
    LOCAL_WORKER_MAX_LOAD  Optional: take no job while the 1-minute load average
                        is above this (the lab then sends new jobs to their
                        hosted twin when CONTENT_LAB_BACKEND_ORDER is on).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers import comfyui  # noqa: E402

log = logging.getLogger("local_comfy_worker")

_CLAIM_WAIT_SECONDS = 25
_HEARTBEAT_SECONDS = 30
_RETRY_SECONDS = 10


class Worker:
    def __init__(self, lab_url: str, key: str, comfy_url: str, client: httpx.AsyncClient):
        self.lab = lab_url.rstrip("/") + "/api/local-worker"
        self.comfy = comfy_url.rstrip("/")
        self.key = key
        self.client = client
        self.last_busy: str | None = None

    def _headers(self, job: dict | None = None) -> dict:
        headers = {"X-Worker-Key": self.key}
        if job is not None:
            headers["X-Claim-Token"] = job["claim_token"]
        return headers

    async def busy(self) -> str | None:
        """Why this machine should take no job right now, or None when it is free."""
        limit = os.getenv("LOCAL_WORKER_MAX_LOAD", "").strip()
        try:
            if limit and os.getloadavg()[0] > float(limit):
                return "load"
        except (OSError, ValueError):
            pass
        try:
            resp = await self.client.get(f"{self.comfy}/queue", timeout=10)
            body = resp.json() if resp.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return "comfyui_unreachable"
        if not isinstance(body, dict):
            return "comfyui_unreachable"
        if body.get("queue_running") or body.get("queue_pending"):
            return "comfyui_busy"   # another user's render; ours run one at a time
        return None

    async def check_in_busy(self) -> None:
        resp = await self.client.post(
            f"{self.lab}/claim", params={"busy": "true"}, headers=self._headers(), timeout=30)
        resp.raise_for_status()

    async def claim(self) -> dict | None:
        resp = await self.client.post(
            f"{self.lab}/claim", params={"wait": _CLAIM_WAIT_SECONDS},
            headers=self._headers(), timeout=_CLAIM_WAIT_SECONDS + 15)
        resp.raise_for_status()
        return resp.json().get("job")

    async def _heartbeat(self, job: dict) -> None:
        while True:
            await asyncio.sleep(_HEARTBEAT_SECONDS)
            try:
                await self.client.post(
                    f"{self.lab}/jobs/{job['id']}/heartbeat", headers=self._headers(job), timeout=30)
            except httpx.HTTPError as exc:
                log.warning("heartbeat failed: %s", exc)

    async def _render(self, job: dict) -> bytes:
        params = {**job.get("params", {}), "model_id": job["model_id"], "entry": {}}
        video_url = await comfyui.run_on_server(self.comfy, job["prompt"], params, self.client)
        resp = await self.client.get(video_url, timeout=300)
        resp.raise_for_status()
        return resp.content

    async def run_job(self, job: dict) -> bool:
        """Render one claimed job and report the outcome. True when uploaded."""
        beat = asyncio.create_task(self._heartbeat(job))
        try:
            try:
                video = await self._render(job)
            except Exception as exc:  # noqa: BLE001 - every failure is reported to the lab
                log.error("job %s failed: %s", job["id"], exc)
                await self.client.post(
                    f"{self.lab}/jobs/{job['id']}/fail", headers=self._headers(job),
                    json={"error": str(exc) or repr(exc)}, timeout=30)
                return False
            resp = await self.client.post(
                f"{self.lab}/jobs/{job['id']}/result", headers=self._headers(job),
                content=video, timeout=600)
            resp.raise_for_status()
            log.info("job %s uploaded (%d bytes)", job["id"], len(video))
            return True
        finally:
            beat.cancel()

    async def run_forever(self) -> None:
        log.info("worker online: lab=%s comfyui=%s", self.lab, self.comfy)
        while True:
            try:
                reason = await self.busy()
                if reason != self.last_busy:
                    log.info("not taking jobs: %s" % reason if reason else "taking jobs again")
                    self.last_busy = reason
                if reason:
                    await self.check_in_busy()
                    await asyncio.sleep(_RETRY_SECONDS)
                    continue
                job = await self.claim()
            except httpx.HTTPError as exc:
                log.warning("claim failed: %s; retrying in %ss", exc, _RETRY_SECONDS)
                await asyncio.sleep(_RETRY_SECONDS)
                continue
            if job is None:
                continue
            log.info("job %s claimed: %s", job["id"], job["model_id"])
            try:
                await self.run_job(job)
            except httpx.HTTPError as exc:
                log.error("job %s could not be reported: %s", job["id"], exc)


async def main() -> None:
    lab = os.getenv("CONTENT_LAB_URL", "").strip()
    key = os.getenv("LOCAL_WORKER_KEY", "").strip()
    if not lab or not key:
        raise SystemExit("CONTENT_LAB_URL and LOCAL_WORKER_KEY are required")
    comfy = os.getenv("COMFYUI_SERVER_URL", "").strip() or "http://127.0.0.1:8188"
    async with httpx.AsyncClient() as client:
        await Worker(lab, key, comfy, client).run_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per poll otherwise
    asyncio.run(main())
