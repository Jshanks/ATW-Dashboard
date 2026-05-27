"""ATW Dashboard -- FastAPI application entry point."""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.config import DashboardConfig
from backend.models import (
    AddInstanceRequest,
    BulkProjectRequest,
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
from backend import projects
from backend import tracker
from backend import history

log_level = os.environ.get("LOG_LEVEL", "info").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("atw-dashboard")

config = None
clients = {}
ws_connections = []
broadcast_task = None
tracker_task = None


def _get_tracker_pair():
    """Find the first online instance with a downloader and project slug."""
    for client in clients.values():
        s = client.status
        if s.connection_state != ConnectionState.ONLINE:
            continue
        if not s.downloader:
            continue
        if s.project_slug:
            return s.project_slug, s.downloader
    return None, None


async def _tracker_poll_loop():
    """Poll tracker stats every 60s and record items to history."""
    while True:
        try:
            await asyncio.sleep(60)
            slug, downloader = _get_tracker_pair()
            if not slug or not downloader:
                continue
            stats = await tracker.get_project_data(slug)
            if not stats:
                continue
            user_stats = tracker.build_user_stats(stats, downloader, slug)
            items_done = user_stats.get("user_items_done", 0)
            if items_done > 0:
                history.record_tracker(items_done)
                logger.debug("Recorded tracker items: %d for %s", items_done, downloader)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Tracker poll error: %s", e)
            await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app):
    global config, broadcast_task, tracker_task

    config = DashboardConfig.load()
    logger.info('Dashboard "%s" starting', config.title)

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

    asyncio.create_task(projects.get_projects(force=True))

    broadcast_task = asyncio.create_task(_broadcast_loop())
    tracker_task = asyncio.create_task(_tracker_poll_loop())
    yield

    if broadcast_task:
        broadcast_task.cancel()
        try:
            await broadcast_task
        except asyncio.CancelledError:
            pass

    if tracker_task:
        tracker_task.cancel()
        try:
            await tracker_task
        except asyncio.CancelledError:
            pass

    for client in clients.values():
        await client.stop()

    logger.info("Dashboard shut down cleanly.")


app = FastAPI(
    title="ATW Dashboard",
    description="Monitoring & control dashboard for ArchiveTeam Warrior instances",
    version="2.5.0",
    lifespan=lifespan,
)

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


async def _broadcast_loop():
    while True:
        try:
            await asyncio.sleep(2)

            for client in clients.values():
                s = client.status
                if s.connection_state == ConnectionState.ONLINE:
                    total_bytes = s.bytes_downloaded + s.bytes_uploaded
                    history.record(s.name, total_bytes)

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


def _build_dashboard_state():
    instances = []
    total_online = 0
    total_offline = 0
    total_items_active = 0
    for client in clients.values():
        s = client.status
        instances.append(s)
        if s.connection_state == ConnectionState.ONLINE:
            total_online += 1
            total_items_active += len(
                [i for i in s.items if i.state.value not in ("waiting", "done", "unknown")]
            )
        else:
            total_offline += 1
    return DashboardState(
        instances=instances,
        total_online=total_online,
        total_offline=total_offline,
        total_items_active=total_items_active,
    )


# -- Frontend --
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    with open(os.path.join(FRONTEND_DIR, "index.html"), "r") as fh:
        return HTMLResponse(content=fh.read())


# -- API --
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
    return _build_dashboard_state().model_dump()


@app.get("/api/instances/{name}")
async def get_instance(name):
    if name not in clients:
        raise HTTPException(status_code=404, detail="Instance not found: " + name)
    return clients[name].status.model_dump()


@app.post("/api/instances")
async def add_instance(request: AddInstanceRequest):
    if request.name in clients:
        raise HTTPException(status_code=409, detail="Instance already exists: " + request.name)
    inst_config = WarriorInstanceConfig(
        name=request.name, host=request.host, port=request.port,
        http_username=request.http_username, http_password=request.http_password,
    )
    client = WarriorClient(inst_config, reconnect_base=config.reconnect_base, reconnect_max=config.reconnect_max)
    clients[request.name] = client
    await client.start(poll_interval=config.poll_interval)
    store.add(inst_config.model_dump())
    return {"status": "ok", "instance": request.name}


@app.put("/api/instances/{name}")
async def edit_instance(name, request: EditInstanceRequest):
    if name not in clients:
        raise HTTPException(status_code=404, detail="Instance not found: " + name)
    old = clients[name]
    await old.stop()
    new_host = request.host if request.host is not None else old.config.host
    new_port = request.port if request.port is not None else old.config.port
    new_user = request.http_username if request.http_username is not None else old.config.http_username
    new_pass = request.http_password if request.http_password is not None else old.config.http_password
    update_fields = {}
    if request.host is not None: update_fields["host"] = request.host
    if request.port is not None: update_fields["port"] = request.port
    if request.http_username is not None: update_fields["http_username"] = request.http_username
    if request.http_password is not None: update_fields["http_password"] = request.http_password
    store.update(name, update_fields)
    new_config = WarriorInstanceConfig(name=name, host=new_host, port=new_port, http_username=new_user, http_password=new_pass)
    new_client = WarriorClient(new_config, reconnect_base=config.reconnect_base, reconnect_max=config.reconnect_max)
    clients[name] = new_client
    await new_client.start(poll_interval=config.poll_interval)
    return {"status": "ok", "instance": name}


@app.delete("/api/instances/{name}")
async def remove_instance(name):
    if name not in clients:
        raise HTTPException(status_code=404, detail="Instance not found: " + name)
    await clients[name].stop()
    del clients[name]
    store.remove(name)
    history.remove(name)
    return {"status": "ok", "instance": name}


@app.post("/api/instances/{name}/settings")
async def update_instance_settings(name, settings: WarriorSettings):
    if name not in clients:
        raise HTTPException(status_code=404, detail="Instance not found: " + name)
    success = await clients[name].update_settings(settings)
    if success:
        return {"status": "ok", "instance": name}
    return JSONResponse(status_code=502, content={"status": "error", "detail": "Failed to update settings on " + name})


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


# -- Projects --
@app.get("/api/projects")
async def list_projects():
    return await projects.get_projects()


@app.post("/api/project/bulk")
async def bulk_change_project(request: BulkProjectRequest):
    results = {}
    for name in request.instance_names:
        if name not in clients:
            results[name] = {"status": "error", "detail": "Not found"}
            continue
        success = await clients[name].change_project(request.project_name)
        results[name] = {"status": "ok" if success else "error"}
    return {"results": results}


# -- Tracker stats --
@app.get("/api/tracker")
async def get_tracker_stats_endpoint():
    """Get tracker leaderboard stats for the user across active projects."""
    seen = {}
    for client in clients.values():
        s = client.status
        if s.connection_state != ConnectionState.ONLINE:
            continue
        if not s.downloader:
            continue

        slug = s.project_slug
        if not slug:
            continue

        if slug not in seen:
            seen[slug] = s.downloader
            logger.debug("Tracker pair: slug=%s downloader=%s (from %s)", slug, s.downloader, s.name)

    if not seen:
        return {"tracker_stats": [], "message": "No active project/downloader pairs found"}

    results = []
    for slug, downloader in seen.items():
        stats = await tracker.get_project_data(slug)
        if not stats:
            logger.warning("Tracker returned no data for slug: %s", slug)
            continue
        entry = tracker.build_user_stats(stats, downloader, slug)
        results.append(entry)

    return {"tracker_stats": results}


# -- History --
@app.get("/api/history")
async def get_history():
    """Return 24h activity history, bucketed and aggregated."""
    return history.get_bucketed()


# -- WebSocket --
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_connections.append(ws)
    logger.info("WebSocket client connected (%d total)", len(ws_connections))
    try:
        await ws.send_text(_build_dashboard_state().model_dump_json())
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