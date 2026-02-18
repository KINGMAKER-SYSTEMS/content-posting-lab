"""Caption burning router."""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.get("/videos")
async def list_videos():
    """List available videos from output directory."""
    # TODO: Implement video listing
    return {"videos": []}


@router.get("/captions")
async def list_captions():
    """List available caption CSVs."""
    # TODO: Implement caption listing
    return {"captions": []}


@router.get("/burned")
async def list_burned_videos():
    """List completed burn batches."""
    # TODO: Implement burned video listing
    return {"batches": []}


@router.websocket("/ws/burn")
async def websocket_burn(websocket: WebSocket):
    """WebSocket endpoint for real-time caption burning progress."""
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            # TODO: Implement caption burning logic
            await websocket.send_text(f"Received: {data}")
    except WebSocketDisconnect:
        pass
