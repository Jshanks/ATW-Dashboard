"""ATW Dashboard -- FastAPI application entry point."""

import asyncio
import json
import logging
import os
import time
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
    PauseRequest,
    ResumeRequest,
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

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
PAUSE_FILE = os.path.join(DATA_DIR, "pause.json")
APP_VERSION = os.environ.get("APP_VERSION", "dev")

config = None
clients = {}
ws_connections = []
broadcast_task = None
tracker_task = None
auto_resume_task = None

# Pause state: {instance_name: {"project_slug": str, "paused_at": float, "resume_at": float|None}}
_pause_state = {}

# Supplementary WS broadcast intervals (seconds)
_HISTORY_BROADCAST_INTERVAL = 30
_TRACKER_BROADCAST_INTERVAL = 60
_PAUSE_BROADCAST_INTERVAL = 30


# ------------------------------------------------------------------
# Tracker baseline persistence (per-project)
# ------------------------------------------------------------------
TRACKER_BASELINE_FILE = os.path.join(DATA_DIR, "tracker_baseline.json")
# Maximum reasonable items-done delta per 60s poll cycle.
# Anything above this is treated as a baseline reset (project switch
# residue, tracker API anomaly, container restart, etc.)
MAX_TRACKER_DELTA_PER_POLL = 10000

_tracker_baselines = {}  # {project_slug: last_known_items_done}


def _load_tracker_baselines():
    global _tracker_baselines
    if not os.path.exists(TRACKER_BASELINE_FILE):
        return
    try:
        with open(TRACKER_BASELINE_FILE, "r") as fh:
            _tracker_baselines = json.load(fh)
        logger.info("Loaded tracker baselines for %d project(s)", len(_tracker_baselines))
    except Exception as exc:
        logger.warning("Failed to load tracker baselines: %s", exc)


def _save_tracker_baselines():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        temporary_path = TRACKER_BASELINE_FILE + ".tmp"
        with open(temporary_path, "w") as fh:
            json.dump(_tracker_baselines, fh)
        os.replace(temporary_path, TRACKER_BASELINE_FILE)
    except Exception as exc:
        logger.warning("Failed to save tracker baselines: %s", exc)

# ------------------------------------------------------------------
# Pause state persistence
# ------------------------------------------------------------------
def _load_pause_state():
    global _pause_state
    if not os.path.exists(PAUSE_FILE):
        return
    try:
        with open(PAUSE_FILE, "r") as f:
            _pause_state = json.load(f)
        logger.info("Loaded pause state: %d paused instance(s)", len(_pause_state))
    except Exception as e:
        logger.warning("Failed to load pause state: %s", e)


def _save_pause_state():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = PAUSE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_pause_state, f)
        os.replace(tmp, PAUSE_FILE)
    except Exception as e:
        logger.warning("Failed to save pause state: %s", e)


# ------------------------------------------------------------------
# Background tasks
# ------------------------------------------------------------------
def _get_tracker_pair():
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
    """Poll the ArchiveTeam tracker for per-project item deltas.

    Iterates ALL active project/downloader pairs (not just the first),
    tracks baselines per-project to avoid cross-project delta inflation,
    and caps deltas to catch anomalies.
    """
    while True:
        try:
            await asyncio.sleep(60)

            # Gather every unique project_slug → downloader pair across
            # all online warriors (replaces _get_tracker_pair's single-pair approach)
            active_pairs = {}
            for client in clients.values():
                warrior_status = client.status
                if warrior_status.connection_state != ConnectionState.ONLINE:
                    continue
                if not warrior_status.downloader or not warrior_status.project_slug:
                    continue
                if warrior_status.project_slug not in active_pairs:
                    active_pairs[warrior_status.project_slug] = warrior_status.downloader

            if not active_pairs:
                continue

            baselines_changed = False

            for project_slug, downloader_name in active_pairs.items():
                project_stats = await tracker.get_project_data(project_slug)
                if not project_stats:
                    continue

                user_stats = tracker.build_user_stats(
                    project_stats, downloader_name, project_slug
                )
                items_done = user_stats.get("user_items_done", 0)

                if items_done <= 0:
                    continue

                previous_items = _tracker_baselines.get(project_slug, 0)

                if previous_items > 0 and items_done >= previous_items:
                    delta = items_done - previous_items
                    if 0 < delta <= MAX_TRACKER_DELTA_PER_POLL:
                        history.record_tracker(delta)
                        logger.debug(
                            "Recorded tracker delta: %d items for %s (total: %d)",
                            delta, project_slug, items_done,
                        )
                    elif delta > MAX_TRACKER_DELTA_PER_POLL:
                        logger.warning(
                            "Tracker delta %d for project '%s' exceeds sanity cap %d "
                            "— resetting baseline without recording",
                            delta, project_slug, MAX_TRACKER_DELTA_PER_POLL,
                        )
                elif previous_items > 0 and items_done < previous_items:
                    # Tracker count went backwards (project reset / data correction)
                    logger.info(
                        "Tracker items for '%s' decreased (%d → %d) — resetting baseline",
                        project_slug, previous_items, items_done,
                    )

                # Always update baseline to latest value
                _tracker_baselines[project_slug] = items_done
                baselines_changed = True

            if baselines_changed:
                _save_tracker_baselines()

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Tracker poll error: %s", exc)
            await asyncio.sleep(30)


async def _auto_resume_loop():
    """Check every 30s if any paused instances should be auto-resumed."""
    while True:
        try:
            await asyncio.sleep(30)
            now = time.time()
            to_resume = []
            for name, state in list(_pause_state.items()):
                resume_at = state.get("resume_at")
                if resume_at and now >= resume_at:
                    to_resume.append(name)

            for name in to_resume:
                slug = _pause_state[name].get("project_slug", "")
                if name in clients and slug:
                    success = await clients[name].select_project(slug)
                    if success:
                        logger.info("Auto-resumed %s with project %s", name, slug)
                    else:
                        logger.warning("Auto-resume failed for %s", name)
                del _pause_state[name]

            if to_resume:
                _save_pause_state()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Auto-resume error: %s", e)


# ------------------------------------------------------------------
# Lifespan
# ------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app):
    global config, broadcast_task, tracker_task, auto_resume_task

    config = DashboardConfig.load()
    logger.info('Dashboard "%s" starting', config.title)

    history.load()
    _load_pause_state()
    _load_tracker_baselines()

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
    auto_resume_task = asyncio.create_task(_auto_resume_loop())
    yield

    for task in [broadcast_task, tracker_task, auto_resume_task]:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    for client in clients.values():
        await client.stop()

    history.force_save()
    _save_pause_state()
    _save_tracker_baselines(

    logger.info("Dashboard shut down cleanly.")


app = FastAPI(
    title="ATW Dashboard",
    description="Monitoring & control dashboard for ArchiveTeam Warrior instances",
    version=APP_VERSION,
    lifespan=lifespan,
)

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


async def _broadcast_loop():
    last_history_time = 0.0
    last_tracker_time = 0.0
    last_pause_time = 0.0
    while True:
        try:
            await asyncio.sleep(2)
            for client in clients.values():
                s = client.status
                if s.connection_state == ConnectionState.ONLINE:
                    total_bytes = s.bytes_downloaded + s.bytes_uploaded
                    history.record(s.name, total_bytes)
            history.save()
            if not ws_connections:
                continue
            now = time.monotonic()
            state = _build_dashboard_state()
            state_dict = state.model_dump(mode="json")
            # Attach supplementary data when their intervals elapse
            if now - last_history_time >= _HISTORY_BROADCAST_INTERVAL:
                state_dict["history"] = history.get_bucketed()
                last_history_time = now
            if now - last_tracker_time >= _TRACKER_BROADCAST_INTERVAL:
                try:
                    tracker_data = await _build_tracker_stats()
                    state_dict["tracker_stats"] = tracker_data.get("tracker_stats", [])
                except Exception:
                    pass
                last_tracker_time = now
            if now - last_pause_time >= _PAUSE_BROADCAST_INTERVAL:
                state_dict["pause_status"] = _build_pause_status()
                last_pause_time = now
            message = json.dumps(state_dict)
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
    """Dashboard config — rarely changes, safe to cache 5 minutes."""
    response_data = {
        "title": config.title,
        "poll_interval": config.poll_interval,
        "instance_count": len(clients),
        "version": APP_VERSION,
    }
    return JSONResponse(
        content=response_data,
        headers={"Cache-Control": "public, max-age=300"},
    )


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
    if request.host is not None:
        update_fields["host"] = request.host
    if request.port is not None:
        update_fields["port"] = request.port
    if request.http_username is not None:
        update_fields["http_username"] = request.http_username
    if request.http_password is not None:
        update_fields["http_password"] = request.http_password
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
    _pause_state.pop(name, None)
    _save_pause_state()
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
    """Active project list — upstream caches 30min, we cache response 10min."""
    project_list = await projects.get_projects()
    return JSONResponse(
        content=project_list,
        headers={"Cache-Control": "public, max-age=600"},
    )


@app.post("/api/project/bulk")
async def bulk_change_project(request: BulkProjectRequest):
    results = {}
    for name in request.instance_names:
        if name not in clients:
            results[name] = {"status": "error", "detail": "Not found"}
            continue

        # If paused, update the saved project instead of sending to warrior
        if name in _pause_state:
            _pause_state[name]["project_slug"] = request.project_name
            _save_pause_state()
            results[name] = {"status": "ok", "note": "Resume project updated (instance is paused)"}
            continue

        success = await clients[name].change_project(request.project_name)
        results[name] = {"status": "ok" if success else "error"}
    return {"results": results}


# -- Pause / Resume --
def _build_pause_status():
    """Build pause status dict (shared by endpoint and WS broadcast)."""
    now = time.time()
    paused = {}
    for name, state in _pause_state.items():
        resume_at = state.get("resume_at")
        remaining = None
        if resume_at:
            remaining = max(0, resume_at - now)
        paused[name] = {
            "project_slug": state.get("project_slug", ""),
            "paused_at": state.get("paused_at", 0),
            "resume_at": resume_at,
            "remaining_seconds": remaining,
        }
    return {"paused": paused, "count": len(paused)}

@app.get("/api/pause-status")
async def get_pause_status():
    return _build_pause_status()


@app.post("/api/pause")
async def pause_instances(request: PauseRequest):
    now = time.time()
    resume_at = None
    if request.duration_hours is not None:
        resume_at = now + (request.duration_hours * 3600)

    results = {}
    for name in request.instance_names:
        if name not in clients:
            results[name] = {"status": "error", "detail": "Not found"}
            continue
        if name in _pause_state:
            results[name] = {"status": "error", "detail": "Already paused"}
            continue

        client = clients[name]
        slug = client.status.project_slug

        if not slug:
            results[name] = {"status": "error", "detail": "No project selected"}
            continue

        success = await client.deselect_project()
        if success:
            _pause_state[name] = {
                "project_slug": slug,
                "paused_at": now,
                "resume_at": resume_at,
            }
            results[name] = {"status": "ok"}
            dur_str = "indefinite" if not resume_at else str(request.duration_hours) + "h"
            logger.info("Paused %s (project: %s, resume: %s)", name, slug, dur_str)
        else:
            results[name] = {"status": "error", "detail": "Failed to deselect project"}

    _save_pause_state()
    return {"results": results}


@app.post("/api/resume")
async def resume_instances(request: ResumeRequest):
    results = {}
    for name in request.instance_names:
        if name not in _pause_state:
            results[name] = {"status": "error", "detail": "Not paused"}
            continue
        if name not in clients:
            results[name] = {"status": "error", "detail": "Not found"}
            del _pause_state[name]
            continue

        slug = _pause_state[name].get("project_slug", "")
        if not slug:
            results[name] = {"status": "error", "detail": "No project to resume"}
            del _pause_state[name]
            continue

        success = await clients[name].select_project(slug)
        if success:
            del _pause_state[name]
            results[name] = {"status": "ok"}
            logger.info("Resumed %s with project %s", name, slug)
        else:
            results[name] = {"status": "error", "detail": "Failed to select project"}

    _save_pause_state()
    return {"results": results}


# -- Tracker stats --
async def _build_tracker_stats():
    """Build tracker stats dict (shared by endpoint and WS broadcast)."""
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

@app.get("/api/tracker")
async def get_tracker_stats_endpoint():
    return await _build_tracker_stats()


# -- History --
@app.get("/api/history")
async def get_history():
    return history.get_bucketed()


# -- WebSocket --
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_connections.append(ws)
    logger.info("WebSocket client connected (%d total)", len(ws_connections))
    try:
        init_state = _build_dashboard_state().model_dump(mode="json")
        init_state["history"] = history.get_bucketed()
        try:
            tracker_data = await _build_tracker_stats()
            init_state["tracker_stats"] = tracker_data.get("tracker_stats", [])
        except Exception:
            pass
        init_state["pause_status"] = _build_pause_status()
        await ws.send_text(json.dumps(init_state))
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
