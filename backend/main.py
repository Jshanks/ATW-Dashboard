"""ATW Dashboard -- FastAPI application entry point."""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.config import DashboardConfig
from backend.models import (
    AddInstanceRequest,
    BulkSettingsRequest,
    ConnectionState,
    DashboardState,
    EditInstanceRequest,
    WarriorInstanceConfig,
    WarriorSettings,
    WarriorStatus,
)
from backend.warrior_client import WarriorClient
from backend import store

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log_level = os.environ.get("LOG_LEVEL", "info").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("atw-dashboard")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
config: Optional[DashboardConfig] = None
clients: dict[str, WarriorClient] = {}
ws_connections: list[WebSocket] = []
broadcast_task: Optional[asyncio.Task] = None


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global config, broadcast_task

    config = DashboardConfig.load()
    logger.info("Dashboard \"%s\" starting", config.title)

    # Load persisted instances from data/instances.json
    saved = store.get_all()
    logger.info("Loaded %d saved instance(s) from store", len(saved))
    for inst_dict in saved:
        try:
            inst_config = WarriorInstanceConfig(**inst_dict)
            client = WarriorClient(
                inst_config,
                reconnect_base=config.reconnect_base,
                reconnect_max=config.reconnect_max,
            )
            clients[inst_config.name] = client
            await client.start(poll_interval=config.poll_interval)
        except Exception as exc:
            logger.warning("Skipping invalid saved instance %s: %s", inst_dict, exc)

    broadcast_task = asyncio.create_task(_broadcast_loop())
    yield

    if broadcast_task:
        broadcast_task.cancel()
        try:
            await broadcast_task
        except asyncio.CancelledError:
            pass

    for client in clients.values():
        await client.stop()

    logger.info("Dashboard shut down cleanly.")


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="ATW Dashboard",
    description="Monitoring & control dashboard for ArchiveTeam Warrior instances",
    version="1.1.0",
    lifespan=lifespan,
)

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


# ---------------------------------------------------------------------------
# WebSocket broadcast
# ---------------------------------------------------------------------------
async def _broadcast_loop():
    while True:
        try:
            await asyncio.sleep(2)
            if not ws_connections:
                continue

            state = _build_dashboard_state()
            message = state.model_dump_json()

            disconnected = []
            for ws in ws_connections:
                try:
                    await ws.send_text(message)
                except Exception:
                    disconnected.append(ws)

            for ws in disconnected:
                ws_connections.remove(ws)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Broadcast error: %s", e)
            await asyncio.sleep(5)


def _build_dashboard_state() -> DashboardState:
    instances = []
    total_online = 0
    total_offline = 0
    total_items_active = 0

    for client in clients.values():
        status = client.status
        instances.append(status)
        if status.connection_state == ConnectionState.ONLINE:
            total_online += 1
            total_items_active += len(
                [i for i in status.items if i.state.value not in ("waiting", "done", "unknown")]
            )
        else:
            total_offline += 1

    return DashboardState(
        instances=instances,
        total_online=total_online,
        total_offline=total_offline,
        total_items_active=total_items_active,
    )


# ---------------------------------------------------------------------------
# Routes -- Frontend
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    with open(index_path, "r") as fh:
        return HTMLResponse(content=fh.read())


# ---------------------------------------------------------------------------
# Routes -- API
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health_check():
    return {"status": "ok", "instances": len(clients)}


@app.get("/api/config")
async def get_config():
    return {
        "title": config.title,
        "poll_interval": config.poll_interval,
        "instance_count": len(clients),
    }


@app.get("/api/instances")
async def list_instances():
    state = _build_dashboard_state()
    return state.model_dump()


@app.get("/api/instances/{name}")
async def get_instance(name: str):
    if name not in clients:
        raise HTTPException(status_code=404, detail="Instance not found: " + name)
    return clients[name].status.model_dump()


@app.post("/api/instances")
async def add_instance(request: AddInstanceRequest):
    if request.name in clients:
        raise HTTPException(status_code=409, detail="Instance already exists: " + request.name)

    inst_config = WarriorInstanceConfig(
        name=request.name,
        host=request.host,
        port=request.port,
        http_username=request.http_username,
        http_password=request.http_password,
    )
    client = WarriorClient(
        inst_config,
        reconnect_base=config.reconnect_base,
        reconnect_max=config.reconnect_max,
    )
    clients[request.name] = client
    await client.start(poll_interval=config.poll_interval)

    # Persist to store
    store.add(inst_config.model_dump())

    return {"status": "ok", "instance": request.name}


@app.put("/api/instances/{name}")
async def edit_instance(name: str, request: EditInstanceRequest):
    """Edit an existing instance's connection details (host, port, auth)."""
    if name not in clients:
        raise HTTPException(status_code=404, detail="Instance not found: " + name)

    # Stop the old client
    old_client = clients[name]
    await old_client.stop()

    # Build updated fields
    update_fields = {}
    new_host = request.host if request.host is not None else old_client.config.host
    new_port = request.port if request.port is not None else old_client.config.port
    new_user = request.http_username if request.http_username is not None else old_client.config.http_username
    new_pass = request.http_password if request.http_password is not None else old_client.config.http_password

    if request.host is not None:
        update_fields["host"] = request.host
    if request.port is not None:
        update_fields["port"] = request.port
    if request.http_username is not None:
        update_fields["http_username"] = request.http_username
    if request.http_password is not None:
        update_fields["http_password"] = request.http_password

    # Persist changes
    store.update(name, update_fields)

    # Create new client with updated config
    new_config = WarriorInstanceConfig(
        name=name,
        host=new_host,
        port=new_port,
        http_username=new_user,
        http_password=new_pass,
    )
    new_client = WarriorClient(
        new_config,
        reconnect_base=config.reconnect_base,
        reconnect_max=config.reconnect_max,
    )
    clients[name] = new_client
    await new_client.start(poll_interval=config.poll_interval)

    return {"status": "ok", "instance": name}


@app.delete("/api/instances/{name}")
async def remove_instance(name: str):
    if name not in clients:
        raise HTTPException(status_code=404, detail="Instance not found: " + name)

    await clients[name].stop()
    del clients[name]

    # Persist removal
    store.remove(name)

    return {"status": "ok", "instance": name}


@app.post("/api/instances/{name}/settings")
async def update_instance_settings(name: str, settings: WarriorSettings):
    if name not in clients:
        raise HTTPException(status_code=404, detail="Instance not found: " + name)

    success = await clients[name].update_settings(settings)
    if success:
        return {"status": "ok", "instance": name}
    else:
        return JSONResponse(
            status_code=502,
            content={"status": "error", "detail": "Failed to update settings on " + name},
        )


@app.post("/api/settings/bulk")
async def bulk_update_settings(request: BulkSettingsRequest):
    results = {}
    for name in request.instance_names:
        if name not in clients:
            results[name] = {"status": "error", "detail": "Not found"}
            continue
        success = await clients[name].update_settings(request.settings)
        results[name] = {"status": "ok" if success else "error"}

    return {"results": results}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_connections.append(ws)
    logger.info("WebSocket client connected (%d total)", len(ws_connections))

    try:
        state = _build_dashboard_state()
        await ws.send_text(state.model_dump_json())

        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
            except json.JSONDecodeError:
                pass

    except WebSocketDisconnect:
        pass
    finally:
        if ws in ws_connections:
            ws_connections.remove(ws)
        logger.info("WebSocket client disconnected (%d total)", len(ws_connections))
