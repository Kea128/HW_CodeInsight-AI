import asyncio
import json
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.routers.remote import manager as remote_manager
from api.services.ssh_client import RemoteProjectError
from api.services.terminal import (
    TerminalSessionManager,
    authorized_terminal_request,
    relay_terminal,
)

router = APIRouter(tags=["terminal"])
manager = TerminalSessionManager(remote_manager.store)


def _authorized(websocket: WebSocket) -> bool:
    return authorized_terminal_request(
        os.environ.get("CODEINSIGHT_DESKTOP_TOKEN"),
        websocket.query_params.get("token", ""),
        websocket.headers.get("origin"),
        production=os.environ.get("NODE_ENV") == "production",
    )


@router.websocket("/ws/terminal")
async def terminal_socket(websocket: WebSocket):
    if not _authorized(websocket):
        await websocket.close(code=4401, reason="Unauthorized desktop terminal")
        return
    await websocket.accept()
    session = None
    try:
        request = json.loads(await websocket.receive_text())
        if request.get("type") != "open":
            raise RemoteProjectError("终端握手无效")
        session = await asyncio.to_thread(
            manager.open,
            str(request.get("project_id", "")),
            int(request.get("columns", 120)),
            int(request.get("rows", 32)),
        )
        await websocket.send_json(
            {
                "type": "ready",
                "session_id": session.id,
                "project_id": session.project_id,
            }
        )
        await relay_terminal(websocket, session)
    except WebSocketDisconnect:
        pass
    except (RemoteProjectError, ValueError, json.JSONDecodeError) as error:
        await websocket.send_json({"type": "error", "message": str(error)})
    finally:
        if session:
            manager.close(session.id)
