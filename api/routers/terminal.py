import asyncio
import json
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.routers.remote import manager as remote_manager
from api.services.ssh_client import RemoteProjectError
from api.services.terminal import (
    TerminalSessionManager,
    authorize_terminal_websocket,
    relay_terminal,
)

router = APIRouter(tags=["terminal"])
manager = TerminalSessionManager(remote_manager.store)
remote_manager.add_remove_listener(manager.close_project)


@router.websocket("/ws/terminal")
async def terminal_socket(websocket: WebSocket):
    # Browser clients cannot set arbitrary headers. A URL-safe ephemeral token
    # is carried in a WebSocket subprotocol so it is available before accept
    # without leaking into URLs, histories, or proxy access logs.
    supplied_token = await authorize_terminal_websocket(
        websocket,
        os.environ.get("CODEINSIGHT_DESKTOP_TOKEN"),
        production=os.environ.get("NODE_ENV") == "production",
    )
    if supplied_token is None:
        return
    await websocket.accept(subprotocol=f"codeinsight.{supplied_token}")
    session = None
    try:
        request = json.loads(await websocket.receive_text())
        if request.get("type") != "open":
            raise RemoteProjectError("终端握手无效")
        origin = websocket.headers.get("origin") or ""
        session = await asyncio.to_thread(
            manager.open,
            str(request.get("project_id", "")),
            int(request.get("columns", 120)),
            int(request.get("rows", 32)),
            origin=origin,
            token=supplied_token,
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
