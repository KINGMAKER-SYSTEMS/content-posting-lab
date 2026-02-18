"""Caption scraping router."""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.websocket("/ws/{job_id}")
async def websocket_scrape(websocket: WebSocket, job_id: str):
    """WebSocket endpoint for real-time caption scraping progress."""
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            # TODO: Implement caption scraping logic
            await websocket.send_text(f"Received: {data}")
    except WebSocketDisconnect:
        pass


@router.get("/export/{username}")
async def export_captions(username: str):
    """Download captions CSV for a username."""
    # TODO: Implement CSV export
    return {"message": "not implemented"}
