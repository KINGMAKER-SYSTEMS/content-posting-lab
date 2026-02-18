"""Video generation router."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/providers")
async def list_providers():
    """List available video generation providers."""
    # TODO: Implement provider listing
    return {"providers": []}


@router.post("/generate")
async def generate_video():
    """Submit a video generation job."""
    # TODO: Implement video generation
    return {"job_id": ""}


@router.get("/jobs/{job_id}")
async def get_job_status(job_id: str):
    """Poll job status."""
    # TODO: Implement job polling
    return {"status": "pending"}


@router.get("/jobs/{job_id}/download-all")
async def download_all_videos(job_id: str):
    """Download all completed videos as ZIP."""
    # TODO: Implement ZIP download
    return {"message": "not implemented"}
