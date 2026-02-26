# Recreate Tab Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a "Recreate" tab that downloads a TikTok video, extracts first/last frames, removes burned-in text via FLUX.1 Kontext on Replicate, and saves clean reference frames to the project directory.

**Architecture:** WebSocket-based pipeline (mirrors Captions tab pattern). New router `routers/recreate.py` streams progress events. Frontend `pages/Recreate.tsx` renders a 2x2 frame preview grid. Replicate text removal uses the existing polling pattern from `providers/replicate.py`.

**Tech Stack:** FastAPI WebSocket, yt-dlp, ffmpeg/ffprobe, Replicate API (flux-kontext-apps/text-removal), React + Zustand + useWebSocket hook

---

### Task 1: Add `get_project_recreate_dir` helper to project_manager.py

**Files:**
- Modify: `project_manager.py:205-220`
- Modify: `project_manager.py:87-91` (add recreate dir to project creation)

**Step 1: Add the helper function**

After `get_project_burn_dir` (line 220), add:

```python
def get_project_recreate_dir(name: str) -> Path:
    """Get the recreate subdirectory for a project."""
    sanitized_name = sanitize_project_name(name)
    return PROJECTS_DIR / sanitized_name / "recreate"
```

**Step 2: Add recreate dir to project creation**

In `create_project()` (line 91), after `(project_path / "burned").mkdir(exist_ok=True)`, add:

```python
    (project_path / "recreate").mkdir(exist_ok=True)
```

**Step 3: Verify**

Run: `source venv/bin/activate && python -m pytest tests/test_projects_api.py -v`
Expected: All 3 tests PASS (existing tests don't check subdirectory count)

**Step 4: Commit**

```bash
git add project_manager.py
git commit -m "feat: add recreate directory to project structure"
```

---

### Task 2: Add `remove_text` function to providers/replicate.py

**Files:**
- Modify: `providers/replicate.py:121-165` (add new function after `generate`)
- Test: `tests/test_replicate_text_removal.py` (create)

**Step 1: Write the failing test**

Create `tests/test_replicate_text_removal.py`:

```python
import pytest
from providers.replicate import remove_text


def test_remove_text_rejects_empty_image():
    """remove_text should raise ValueError when image_data_uri is empty."""
    with pytest.raises(ValueError, match="image"):
        import asyncio
        asyncio.run(remove_text("", None))
```

**Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && python -m pytest tests/test_replicate_text_removal.py -v`
Expected: FAIL — `ImportError: cannot import name 'remove_text'`

**Step 3: Implement remove_text**

Add to `providers/replicate.py` after the `generate` function (after line 165):

```python
async def remove_text(image_data_uri: str, client: httpx.AsyncClient | None = None) -> str:
    """Send an image to FLUX.1 Kontext text-removal and return the cleaned image URL."""
    if not image_data_uri:
        raise ValueError("remove_text requires a non-empty image data URI")

    key = API_KEYS["replicate"]
    if not key:
        raise RuntimeError("REPLICATE_API_TOKEN not set")

    headers = {"Authorization": f"Token {key}", "Content-Type": "application/json"}
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient()

    try:
        resp = await client.post(
            f"{REPLICATE_API}/models/flux-kontext-apps/text-removal/predictions",
            headers=headers,
            json={"input": {"input_image": image_data_uri}},
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Replicate text-removal start failed: {resp.text}")

        prediction = resp.json()
        pred_id = prediction["id"]
        poll_url = f"{REPLICATE_API}/predictions/{pred_id}"

        deadline = time.time() + 120
        while time.time() < deadline:
            r = await client.get(poll_url, headers=headers, timeout=30)
            data = r.json()
            status = data.get("status", "")
            if status == "succeeded":
                output = data.get("output")
                if isinstance(output, str):
                    return output
                if isinstance(output, list) and output:
                    return output[0]
                raise RuntimeError(f"Unexpected text-removal output: {output}")
            if status in ("failed", "canceled"):
                raise RuntimeError(f"Text removal {status}: {data.get('error', 'unknown')}")
            await asyncio.sleep(3)

        raise RuntimeError("Text removal timed out (120s)")
    finally:
        if own_client:
            await client.aclose()
```

**Step 4: Run test to verify it passes**

Run: `source venv/bin/activate && python -m pytest tests/test_replicate_text_removal.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add providers/replicate.py tests/test_replicate_text_removal.py
git commit -m "feat: add remove_text function for FLUX Kontext text removal"
```

---

### Task 3: Create backend router — routers/recreate.py

**Files:**
- Create: `routers/recreate.py`
- Test: `tests/test_recreate_api.py` (create)

**Step 1: Write the failing test**

Create `tests/test_recreate_api.py`:

```python
import pytest
from httpx import ASGITransport, AsyncClient
from app import app


@pytest.mark.anyio
async def test_list_recreate_jobs_empty():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/recreate/jobs", params={"project": "quick-test"})
        assert r.status_code == 200
        data = r.json()
        assert data["jobs"] == []


@pytest.mark.anyio
async def test_delete_nonexistent_job_returns_404():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.delete("/api/recreate/jobs/fake-id", params={"project": "quick-test"})
        assert r.status_code == 404
```

**Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && python -m pytest tests/test_recreate_api.py -v`
Expected: FAIL — router not mounted, 404s

**Step 3: Create the router**

Create `routers/recreate.py`:

```python
"""Recreate router — extract frames from TikTok videos and remove text overlays."""

import asyncio
import base64
import json
import shutil
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from project_manager import get_project_recreate_dir

router = APIRouter()

_ws_clients: dict[str, list[WebSocket]] = {}


async def _send(job_id: str, event: str, data: dict):
    """Send an event to all WebSocket clients for a job."""
    clients = _ws_clients.get(job_id, [])
    msg = json.dumps({"event": event, **data})
    dead: list[WebSocket] = []
    for ws in clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.remove(ws)


def _image_to_data_uri(path: Path) -> str:
    """Convert an image file to a base64 data URI."""
    raw = path.read_bytes()
    b64 = base64.b64encode(raw).decode()
    suffix = path.suffix.lower()
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(
        suffix.lstrip("."), "image/jpeg"
    )
    return f"data:{mime};base64,{b64}"


async def _get_video_duration(video_path: Path) -> float:
    """Get video duration in seconds using ffprobe."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {stderr.decode(errors='replace')[-200:]}")
    return float(stdout.decode().strip())


async def _run_pipeline(job_id: str, video_url: str, project: str):
    """Full recreate pipeline: download → extract frames → remove text → save."""
    from scraper.frame_extractor import download_video, extract_frame
    from providers.replicate import remove_text

    recreate_dir = get_project_recreate_dir(project)
    job_dir = recreate_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Stage 1: Download video
        await _send(job_id, "downloading", {"text": "Downloading TikTok video..."})
        video_path = job_dir / "source_video.mp4"
        await download_video(video_url, video_path)
        await _send(job_id, "downloaded", {"text": "Video downloaded"})

        # Stage 2: Extract first and last frames
        await _send(job_id, "extracting_frames", {"text": "Extracting first and last frames..."})
        duration = await _get_video_duration(video_path)

        first_frame = job_dir / "first_frame_original.jpg"
        last_frame = job_dir / "last_frame_original.jpg"

        await extract_frame(video_path, first_frame, timestamp=0.0)
        last_ts = max(0.0, duration - 0.1)
        await extract_frame(video_path, last_frame, timestamp=last_ts)

        # Send original frames as base64 previews
        first_b64 = base64.b64encode(first_frame.read_bytes()).decode()
        last_b64 = base64.b64encode(last_frame.read_bytes()).decode()
        await _send(job_id, "frames_ready", {
            "text": "Frames extracted",
            "first_frame": first_b64,
            "last_frame": last_b64,
            "duration": round(duration, 1),
        })

        # Stage 3: Remove text from both frames
        async with httpx.AsyncClient() as client:
            # First frame
            await _send(job_id, "removing_text", {"text": "Removing text from first frame...", "frame": "first"})
            first_uri = _image_to_data_uri(first_frame)
            first_clean_url = await remove_text(first_uri, client)

            # Download cleaned first frame
            first_clean_path = job_dir / "first_frame_clean.png"
            r = await client.get(first_clean_url, timeout=30)
            first_clean_path.write_bytes(r.content)
            first_clean_b64 = base64.b64encode(r.content).decode()
            await _send(job_id, "text_removed", {
                "text": "First frame cleaned",
                "frame": "first",
                "clean_b64": first_clean_b64,
            })

            # Last frame
            await _send(job_id, "removing_text", {"text": "Removing text from last frame...", "frame": "last"})
            last_uri = _image_to_data_uri(last_frame)
            last_clean_url = await remove_text(last_uri, client)

            # Download cleaned last frame
            last_clean_path = job_dir / "last_frame_clean.png"
            r = await client.get(last_clean_url, timeout=30)
            last_clean_path.write_bytes(r.content)
            last_clean_b64 = base64.b64encode(r.content).decode()
            await _send(job_id, "text_removed", {
                "text": "Last frame cleaned",
                "frame": "last",
                "clean_b64": last_clean_b64,
            })

        # Stage 4: Complete
        await _send(job_id, "complete", {
            "text": "All frames processed",
            "job_id": job_id,
            "first_clean": f"/projects/{project}/recreate/{job_id}/first_frame_clean.png",
            "last_clean": f"/projects/{project}/recreate/{job_id}/last_frame_clean.png",
            "first_original": f"/projects/{project}/recreate/{job_id}/first_frame_original.jpg",
            "last_original": f"/projects/{project}/recreate/{job_id}/last_frame_original.jpg",
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        await _send(job_id, "error", {"error": str(e)})


# ── WebSocket endpoint ─────────────────────────────────────────────


@router.websocket("/ws/{job_id}")
async def websocket_recreate(ws: WebSocket, job_id: str):
    """WebSocket endpoint for real-time recreate pipeline progress."""
    await ws.accept()
    _ws_clients.setdefault(job_id, []).append(ws)
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            if msg.get("action") == "start":
                asyncio.create_task(
                    _run_pipeline(
                        job_id,
                        msg["video_url"],
                        msg.get("project", "quick-test"),
                    )
                )
    except WebSocketDisconnect:
        pass
    finally:
        clients = _ws_clients.get(job_id, [])
        if ws in clients:
            clients.remove(ws)


# ── REST endpoints ─────────────────────────────────────────────────


@router.get("/jobs")
async def list_recreate_jobs(project: str = Query(default="quick-test")):
    """List completed recreate jobs for a project."""
    recreate_dir = get_project_recreate_dir(project)
    if not recreate_dir.exists():
        return {"jobs": []}

    jobs = []
    for job_dir in sorted(recreate_dir.iterdir(), reverse=True):
        if not job_dir.is_dir():
            continue
        first_clean = job_dir / "first_frame_clean.png"
        if not first_clean.exists():
            continue  # incomplete job
        jobs.append({
            "job_id": job_dir.name,
            "first_clean": f"/projects/{project}/recreate/{job_dir.name}/first_frame_clean.png",
            "last_clean": f"/projects/{project}/recreate/{job_dir.name}/last_frame_clean.png",
            "first_original": f"/projects/{project}/recreate/{job_dir.name}/first_frame_original.jpg",
            "last_original": f"/projects/{project}/recreate/{job_dir.name}/last_frame_original.jpg",
        })
    return {"jobs": jobs}


@router.delete("/jobs/{job_id}")
async def delete_recreate_job(
    job_id: str, project: str = Query(default="quick-test")
):
    """Delete a recreate job and its files."""
    recreate_dir = get_project_recreate_dir(project)
    job_dir = recreate_dir / job_id
    if not job_dir.exists():
        raise HTTPException(404, "Job not found")
    shutil.rmtree(job_dir)
    return {"status": "deleted"}
```

**Step 4: Mount the router in app.py**

In `app.py`, add import (after line 19):

```python
from routers.recreate import router as recreate_router
```

Add router mount (after line 113):

```python
app.include_router(recreate_router, prefix="/api/recreate", tags=["recreate"])
```

**Step 5: Run tests to verify**

Run: `source venv/bin/activate && python -m pytest tests/test_recreate_api.py -v`
Expected: 2 tests PASS

Run: `source venv/bin/activate && python -m pytest tests/ -v`
Expected: All tests PASS (existing + new)

**Step 6: Commit**

```bash
git add routers/recreate.py app.py tests/test_recreate_api.py
git commit -m "feat: add recreate router with WebSocket pipeline and REST endpoints"
```

---

### Task 4: Add `recreateJobActive` to Zustand store

**Files:**
- Modify: `frontend/src/stores/workflowStore.ts:62-95` (interface), `frontend/src/stores/workflowStore.ts:97-111` (defaults)

**Step 1: Add to interface**

In `WorkflowState` interface (after `captionJobActive: boolean;` on line 70), add:

```typescript
  recreateJobActive: boolean;
```

After `setCaptionJobActive: (active: boolean) => void;` (line 85), add:

```typescript
  setRecreateJobActive: (active: boolean) => void;
```

**Step 2: Add default and action**

In the store defaults (after `captionJobActive: false,` around line 104), add:

```typescript
  recreateJobActive: false,
```

After the `setCaptionJobActive` action (around line 194), add:

```typescript
  setRecreateJobActive: (active) => {
    set({ recreateJobActive: active });
  },
```

**Step 3: Verify frontend builds**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

**Step 4: Commit**

```bash
git add frontend/src/stores/workflowStore.ts
git commit -m "feat: add recreateJobActive state to workflow store"
```

---

### Task 5: Create Recreate.tsx page component

**Files:**
- Create: `frontend/src/pages/Recreate.tsx`

**Step 1: Create the page**

Create `frontend/src/pages/Recreate.tsx`:

```tsx
import { useState, useCallback, useRef } from 'react';
import { useWorkflowStore } from '../stores/workflowStore';
import type { WebSocketStatus } from '../hooks/useWebSocket';
import { useWebSocket } from '../hooks/useWebSocket';
import { EmptyState } from '../components';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { ScrollArea } from '@/components/ui/scroll-area';

interface RecreateJob {
  job_id: string;
  first_clean: string;
  last_clean: string;
  first_original: string;
  last_original: string;
}

interface FrameState {
  firstOriginal: string | null;
  lastOriginal: string | null;
  firstClean: string | null;
  lastClean: string | null;
  duration: number | null;
}

type LogEntry = { text: string; timestamp: number };

function statusBadgeVariant(status: WebSocketStatus): 'warning' | 'info' | 'success' | 'error' | 'secondary' {
  switch (status) {
    case 'connecting': return 'warning';
    case 'connected': return 'info';
    case 'reconnecting': return 'warning';
    case 'error': return 'error';
    default: return 'secondary';
  }
}

export function RecreatePage() {
  const { activeProjectName, addNotification, setRecreateJobActive } = useWorkflowStore();

  const [videoUrl, setVideoUrl] = useState('');
  const [running, setRunning] = useState(false);
  const [complete, setComplete] = useState(false);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [frames, setFrames] = useState<FrameState>({
    firstOriginal: null, lastOriginal: null,
    firstClean: null, lastClean: null,
    duration: null,
  });
  const [resultPaths, setResultPaths] = useState<{
    firstClean: string | null; lastClean: string | null;
    firstOriginal: string | null; lastOriginal: string | null;
  }>({ firstClean: null, lastClean: null, firstOriginal: null, lastOriginal: null });

  const [pastJobs, setPastJobs] = useState<RecreateJob[]>([]);
  const jobIdRef = useRef<string>('');
  const logEndRef = useRef<HTMLDivElement>(null);

  const addLog = useCallback((text: string) => {
    setLogs((prev) => [...prev, { text, timestamp: Date.now() }]);
    setTimeout(() => logEndRef.current?.scrollIntoView({ behavior: 'smooth' }), 50);
  }, []);

  const fetchPastJobs = useCallback(() => {
    if (!activeProjectName) return;
    fetch(`/api/recreate/jobs?project=${encodeURIComponent(activeProjectName)}`)
      .then((r) => (r.ok ? r.json() : { jobs: [] }))
      .then((data: { jobs: RecreateJob[] }) => setPastJobs(data.jobs))
      .catch(() => {});
  }, [activeProjectName]);

  // Fetch past jobs on mount / project change
  useState(() => { fetchPastJobs(); });

  const wsUrl = running && jobIdRef.current
    ? `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/recreate/ws/${jobIdRef.current}`
    : null;

  const pipelineCompleteRef = useRef(false);

  const { status: wsStatus, sendMessage } = useWebSocket(wsUrl, {
    onOpen: () => {
      sendMessage({
        action: 'start',
        video_url: videoUrl,
        project: activeProjectName,
      });
    },
    onMessage: (event) => {
      const msg = JSON.parse(event.data);
      const ev = msg.event;

      if (msg.text) addLog(msg.text);

      if (ev === 'frames_ready') {
        setFrames((prev) => ({
          ...prev,
          firstOriginal: `data:image/jpeg;base64,${msg.first_frame}`,
          lastOriginal: `data:image/jpeg;base64,${msg.last_frame}`,
          duration: msg.duration,
        }));
      } else if (ev === 'text_removed') {
        if (msg.frame === 'first') {
          setFrames((prev) => ({ ...prev, firstClean: `data:image/png;base64,${msg.clean_b64}` }));
        } else {
          setFrames((prev) => ({ ...prev, lastClean: `data:image/png;base64,${msg.clean_b64}` }));
        }
      } else if (ev === 'complete') {
        pipelineCompleteRef.current = true;
        setRunning(false);
        setComplete(true);
        setRecreateJobActive(false);
        setResultPaths({
          firstClean: msg.first_clean,
          lastClean: msg.last_clean,
          firstOriginal: msg.first_original,
          lastOriginal: msg.last_original,
        });
        addNotification('success', 'Frames extracted and cleaned!');
        fetchPastJobs();
      } else if (ev === 'error') {
        pipelineCompleteRef.current = true;
        setRunning(false);
        setRecreateJobActive(false);
        addNotification('error', msg.error || 'Pipeline failed');
      }
    },
    shouldReconnect: () => !pipelineCompleteRef.current,
  });

  const handleStart = () => {
    if (!videoUrl.trim() || !activeProjectName) return;

    const id = crypto.randomUUID();
    jobIdRef.current = id;
    pipelineCompleteRef.current = false;

    setRunning(true);
    setComplete(false);
    setRecreateJobActive(true);
    setLogs([]);
    setFrames({ firstOriginal: null, lastOriginal: null, firstClean: null, lastClean: null, duration: null });
    setResultPaths({ firstClean: null, lastClean: null, firstOriginal: null, lastOriginal: null });
  };

  const handleDelete = async (jobId: string) => {
    if (!activeProjectName) return;
    await fetch(`/api/recreate/jobs/${jobId}?project=${encodeURIComponent(activeProjectName)}`, { method: 'DELETE' });
    setPastJobs((prev) => prev.filter((j) => j.job_id !== jobId));
  };

  if (!activeProjectName) {
    return (
      <div className="h-full flex items-center justify-center">
        <EmptyState icon="📁" title="No Project Selected" description="Select or create a project to start recreating." />
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col lg:flex-row overflow-hidden">
      {/* Left panel — input + log */}
      <div className="w-full lg:w-[380px] border-r-2 border-border bg-card p-6 overflow-y-auto flex-shrink-0">
        <h2 className="text-xl font-heading text-foreground mb-1">Recreate</h2>
        <p className="text-xs text-muted-foreground mb-6">Extract & clean reference frames from TikTok videos</p>

        <div className="space-y-4">
          <div>
            <Label htmlFor="rec-url">TikTok Video URL</Label>
            <Input
              id="rec-url"
              value={videoUrl}
              onChange={(e) => setVideoUrl(e.target.value)}
              placeholder="https://www.tiktok.com/@user/video/..."
              className="mt-1"
              disabled={running}
            />
          </div>

          <Button onClick={handleStart} disabled={running || !videoUrl.trim()} className="w-full">
            {running ? 'Processing...' : 'Extract & Clean'}
          </Button>

          {running && (
            <div className="flex items-center gap-2">
              <div className="w-3 h-3 border-2 border-muted border-t-primary rounded-full animate-spin" />
              <span className="text-xs text-muted-foreground">Pipeline running</span>
              <Badge variant={statusBadgeVariant(wsStatus)} className="ml-auto text-[10px]">
                {wsStatus.toUpperCase()}
              </Badge>
            </div>
          )}
        </div>

        {/* Log */}
        {logs.length > 0 && (
          <div className="mt-6 border-t-2 border-border pt-4">
            <div className="text-sm font-bold text-foreground mb-2">Activity Log</div>
            <ScrollArea className="max-h-[300px]">
              <div className="space-y-1 font-mono text-xs text-muted-foreground">
                {logs.map((log, i) => (
                  <div key={i} className="flex gap-2">
                    <span className="text-primary/50 flex-shrink-0">{new Date(log.timestamp).toLocaleTimeString()}</span>
                    <span>{log.text}</span>
                  </div>
                ))}
                <div ref={logEndRef} />
              </div>
            </ScrollArea>
          </div>
        )}

        {/* Past Jobs */}
        <div className="mt-6 border-t-2 border-border pt-4">
          <div className="text-sm font-bold text-foreground mb-2">
            Previous Jobs
            {pastJobs.length > 0 && <span className="ml-1 text-[11px] text-muted-foreground font-normal">({pastJobs.length})</span>}
          </div>
          {pastJobs.length === 0 ? (
            <p className="text-xs text-muted-foreground italic">No completed jobs yet.</p>
          ) : (
            <div className="space-y-2">
              {pastJobs.map((job) => (
                <div
                  key={job.job_id}
                  className="flex items-center gap-2 rounded-[var(--border-radius)] border-2 border-border p-2 bg-card hover:bg-muted transition-colors cursor-pointer"
                  onClick={() => {
                    setFrames({
                      firstOriginal: job.first_original,
                      lastOriginal: job.last_original,
                      firstClean: job.first_clean,
                      lastClean: job.last_clean,
                      duration: null,
                    });
                    setResultPaths({
                      firstClean: job.first_clean,
                      lastClean: job.last_clean,
                      firstOriginal: job.first_original,
                      lastOriginal: job.last_original,
                    });
                    setComplete(true);
                  }}
                >
                  <img src={job.first_clean} alt="thumb" className="w-10 h-10 rounded object-cover border border-border" />
                  <div className="flex-1 min-w-0">
                    <div className="text-xs text-foreground font-bold truncate">{job.job_id.slice(0, 8)}</div>
                  </div>
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); handleDelete(job.job_id); }}
                    className="text-xs text-muted-foreground hover:text-destructive transition-colors"
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Right panel — frame preview */}
      <div className="flex-1 p-6 overflow-y-auto bg-background">
        <div className="max-w-4xl mx-auto">
          {!frames.firstOriginal && !complete ? (
            <EmptyState icon="🎬" title="No Frames Yet" description="Paste a TikTok video URL and click Extract & Clean." />
          ) : (
            <div className="space-y-6">
              <div className="flex items-center justify-between">
                <h3 className="text-lg font-heading text-foreground">Frame Preview</h3>
                {frames.duration && <Badge variant="secondary">{frames.duration}s video</Badge>}
              </div>

              <div className="grid grid-cols-2 gap-4">
                {/* First frame column */}
                <div className="space-y-3">
                  <div className="text-sm font-bold text-muted-foreground uppercase tracking-wider">First Frame</div>

                  <Card>
                    <CardContent className="p-2">
                      <div className="text-[10px] text-muted-foreground mb-1">Original</div>
                      {frames.firstOriginal ? (
                        <img src={frames.firstOriginal} alt="First frame original" className="w-full rounded border border-border" />
                      ) : (
                        <div className="aspect-[9/16] bg-muted rounded flex items-center justify-center text-muted-foreground text-xs">Extracting...</div>
                      )}
                    </CardContent>
                  </Card>

                  <Card>
                    <CardContent className="p-2">
                      <div className="text-[10px] text-muted-foreground mb-1">Cleaned</div>
                      {frames.firstClean ? (
                        <>
                          <img src={frames.firstClean} alt="First frame cleaned" className="w-full rounded border border-border" />
                          {resultPaths.firstClean && (
                            <a href={resultPaths.firstClean} download className="block mt-1 text-xs text-primary hover:underline font-bold text-center">
                              Download
                            </a>
                          )}
                        </>
                      ) : running ? (
                        <div className="aspect-[9/16] bg-muted rounded flex items-center justify-center">
                          <div className="w-5 h-5 border-2 border-muted border-t-primary rounded-full animate-spin" />
                        </div>
                      ) : (
                        <div className="aspect-[9/16] bg-muted rounded flex items-center justify-center text-muted-foreground text-xs">—</div>
                      )}
                    </CardContent>
                  </Card>
                </div>

                {/* Last frame column */}
                <div className="space-y-3">
                  <div className="text-sm font-bold text-muted-foreground uppercase tracking-wider">Last Frame</div>

                  <Card>
                    <CardContent className="p-2">
                      <div className="text-[10px] text-muted-foreground mb-1">Original</div>
                      {frames.lastOriginal ? (
                        <img src={frames.lastOriginal} alt="Last frame original" className="w-full rounded border border-border" />
                      ) : (
                        <div className="aspect-[9/16] bg-muted rounded flex items-center justify-center text-muted-foreground text-xs">Extracting...</div>
                      )}
                    </CardContent>
                  </Card>

                  <Card>
                    <CardContent className="p-2">
                      <div className="text-[10px] text-muted-foreground mb-1">Cleaned</div>
                      {frames.lastClean ? (
                        <>
                          <img src={frames.lastClean} alt="Last frame cleaned" className="w-full rounded border border-border" />
                          {resultPaths.lastClean && (
                            <a href={resultPaths.lastClean} download className="block mt-1 text-xs text-primary hover:underline font-bold text-center">
                              Download
                            </a>
                          )}
                        </>
                      ) : running ? (
                        <div className="aspect-[9/16] bg-muted rounded flex items-center justify-center">
                          <div className="w-5 h-5 border-2 border-muted border-t-primary rounded-full animate-spin" />
                        </div>
                      ) : (
                        <div className="aspect-[9/16] bg-muted rounded flex items-center justify-center text-muted-foreground text-xs">—</div>
                      )}
                    </CardContent>
                  </Card>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
```

**Step 2: Verify TypeScript compiles**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

**Step 3: Commit**

```bash
git add frontend/src/pages/Recreate.tsx
git commit -m "feat: add Recreate page component with frame preview grid"
```

---

### Task 6: Wire Recreate tab into App.tsx

**Files:**
- Modify: `frontend/src/App.tsx:3-6` (imports), `frontend/src/App.tsx:44-50` (tabs array), `frontend/src/App.tsx:276-288` (page mount), `frontend/src/App.tsx:28-38` (zustand destructure)

**Step 1: Add import**

After the existing page imports (around line 6), add:

```tsx
import { RecreatePage } from './pages/Recreate';
```

**Step 2: Add to zustand destructure**

In the `useWorkflowStore()` destructure (around line 28-38), add `recreateJobActive`:

```tsx
    recreateJobActive,
```

**Step 3: Add tab to nav array**

In the `tabs` useMemo (around line 44-50), add the Recreate tab between Captions and Burn:

```tsx
      { path: '/recreate', label: 'Recreate', badge: recreateJobActive ? 'LIVE' : undefined },
```

Update the useMemo dependency array to include `recreateJobActive`.

**Step 4: Add page mount**

In the main content area (around line 283-288), after the Captions div and before the Burn div, add:

```tsx
        <div style={{ display: location.pathname === '/recreate' ? 'block' : 'none' }}>
          <RecreatePage />
        </div>
```

**Step 5: Verify build**

Run: `cd frontend && npm run build`
Expected: Build succeeds

**Step 6: Commit**

```bash
git add frontend/src/App.tsx
git commit -m "feat: wire Recreate tab into app shell"
```

---

### Task 7: Add frontend tests for Recreate page

**Files:**
- Create: `frontend/src/pages/Recreate.test.tsx`

**Step 1: Create the test file**

```tsx
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { RecreatePage } from './Recreate';
import { useWorkflowStore } from '../stores/workflowStore';
import type { WebSocketStatus } from '../hooks/useWebSocket';

const wsState: {
  status: WebSocketStatus;
  error: string | null;
  sendMessage: ReturnType<typeof vi.fn>;
  reconnect: ReturnType<typeof vi.fn>;
} = {
  status: 'disconnected',
  error: null,
  sendMessage: vi.fn(),
  reconnect: vi.fn(),
};

let latestOptions: {
  onOpen?: (event: Event) => void;
  onMessage?: (event: MessageEvent) => void;
} | null = null;

vi.mock('../hooks/useWebSocket', () => ({
  useWebSocket: vi.fn((_url: string | null, options: unknown) => {
    latestOptions = options as typeof latestOptions;
    return {
      ws: null,
      error: wsState.error,
      status: wsState.status,
      isConnected: wsState.status === 'connected',
      reconnectAttempts: 0,
      sendMessage: wsState.sendMessage,
      reconnect: wsState.reconnect,
      clearStartPayload: vi.fn(),
    };
  }),
}));

beforeEach(() => {
  wsState.status = 'disconnected';
  wsState.error = null;
  wsState.sendMessage.mockClear();
  latestOptions = null;

  useWorkflowStore.setState({
    activeProjectName: null,
    recreateJobActive: false,
  });

  global.fetch = vi.fn().mockResolvedValue({
    ok: true,
    json: () => Promise.resolve({ jobs: [] }),
  });
});

afterEach(() => { cleanup(); });

describe('RecreatePage', () => {
  it('shows empty state when no project is active', () => {
    render(<MemoryRouter><RecreatePage /></MemoryRouter>);
    expect(screen.getByText('No Project Selected')).toBeTruthy();
  });

  it('renders input and button when project is active', () => {
    useWorkflowStore.setState({ activeProjectName: 'quick-test' });
    render(<MemoryRouter><RecreatePage /></MemoryRouter>);
    expect(screen.getByPlaceholderText(/tiktok/i)).toBeTruthy();
    expect(screen.getByRole('button', { name: /Extract & Clean/i })).toBeTruthy();
  });

  it('disables button when URL is empty', () => {
    useWorkflowStore.setState({ activeProjectName: 'quick-test' });
    render(<MemoryRouter><RecreatePage /></MemoryRouter>);
    const btn = screen.getByRole('button', { name: /Extract & Clean/i });
    expect(btn).toHaveProperty('disabled', true);
  });

  it('shows frame previews after frames_ready event', async () => {
    useWorkflowStore.setState({ activeProjectName: 'quick-test' });
    wsState.status = 'connected';

    render(<MemoryRouter><RecreatePage /></MemoryRouter>);

    fireEvent.change(screen.getByPlaceholderText(/tiktok/i), {
      target: { value: 'https://www.tiktok.com/@artist/video/123' },
    });
    fireEvent.click(screen.getByRole('button', { name: /Extract & Clean/i }));

    await waitFor(() => { expect(latestOptions).toBeTruthy(); });

    act(() => {
      latestOptions?.onMessage?.({
        data: JSON.stringify({
          event: 'frames_ready',
          text: 'Frames extracted',
          first_frame: 'dGVzdA==',
          last_frame: 'dGVzdA==',
          duration: 15.2,
        }),
      } as MessageEvent);
    });

    await waitFor(() => {
      expect(screen.getByText('15.2s video')).toBeTruthy();
      expect(screen.getAllByAltText(/original/i)).toHaveLength(2);
    });
  });
});
```

**Step 2: Run tests**

Run: `cd frontend && npx vitest run src/pages/Recreate.test.tsx`
Expected: 4 tests PASS

**Step 3: Run all frontend tests**

Run: `cd frontend && npx vitest run`
Expected: All tests PASS (existing 18 + new 4 = 22)

**Step 4: Commit**

```bash
git add frontend/src/pages/Recreate.test.tsx
git commit -m "test: add Recreate page tests"
```

---

### Task 8: Integration test — full stack verification

**Step 1: Build frontend**

Run: `cd frontend && npm run build`
Expected: Build succeeds

**Step 2: Start server**

Run: `source venv/bin/activate && python app.py &`
Expected: Server starts on port 8000, startup log shows `recreate` in routes

**Step 3: Verify endpoints**

Run:
```bash
curl -s http://localhost:8000/api/health | python3 -m json.tool
curl -s "http://localhost:8000/api/recreate/jobs?project=quick-test" | python3 -m json.tool
```

Expected:
- Health: status ok
- Jobs: `{"jobs": []}`

**Step 4: Verify frontend loads Recreate tab**

Open `http://localhost:8000/recreate` — should show the Recreate page with URL input.

**Step 5: Run all backend tests**

Run: `source venv/bin/activate && python -m pytest tests/ -v`
Expected: All tests PASS

**Step 6: Run all frontend tests**

Run: `cd frontend && npx vitest run`
Expected: All tests PASS

**Step 7: Commit final state**

```bash
git add -A
git commit -m "feat: Recreate tab complete — frame extraction + text removal pipeline"
```
