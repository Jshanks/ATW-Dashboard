"""ATW Dashboard -- FastAPI application entry point."""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from backend.config import DashboardConfig
from backend.models import (
    AddInstanceRequest,
    BulkAddRequest,
    BulkProjectRequest,
    BulkSettingsRequest,
    ConnectionState,
    DashboardState,
    LiveDashboardState,
    EditInstanceRequest,
    PauseRequest,
    ResumeRequest,
    RestartRequest,
    ProjectHealthPolicyRequest,
    ProjectHealthPreviewRequest,
    ProjectHealthRoleAssignmentRequest,
    ProjectHealthBaselineResetRequest,
    ManualFailoverDecisionRequest,
    ManualFailoverRequest,
    WarriorInstanceConfig,
    WarriorSettings,
    WarriorStatus,
)
from backend.warrior_client import WarriorClient
from backend import store
from backend.store import _safe_log
from backend import projects
from backend import tracker
from backend import history
from backend import instance_import
from backend import project_health

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
automatic_failover_task = None

# Pause state: {instance_name: {"project_slug": str, "paused_at": float, "resume_at": float|None}}
# "project_slug" here is the project to RESUME with: the warrior's selected_project,
# so a warrior on ArchiveTeam's Choice resumes as "auto", not pinned to the resolved project.
_pause_state = {}

# WebSocket cadence and supplementary broadcast intervals (seconds).
# Five-second fleet updates remain responsive while materially reducing browser
# allocation churn for large fleets.
_WS_BROADCAST_INTERVAL = 5
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
        # Migrate old format (bare int) to new format (dict with items + bytes)
        for slug, val in list(_tracker_baselines.items()):
            if isinstance(val, (int, float)):
                _tracker_baselines[slug] = {"items": val, "bytes": 0}
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
    """Poll the ArchiveTeam tracker for per-project item and byte deltas.

    Iterates ALL active project/downloader pairs, tracks baselines
    per-project, caps deltas to catch anomalies, and records aggregated
    deltas for the 24h history chart.
    """
    while True:
        try:
            await asyncio.sleep(60)

            # Keep WarriorHQ's ArchiveTeam's Choice target fresh (served from the
            # 30-minute cache; only hits WarriorHQ when the cache has expired)
            try:
                await projects.get_projects()
            except Exception as projects_error:
                logger.debug("Project list refresh failed: %s", projects_error)

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
            total_items_delta = 0
            total_bytes_delta = 0
            sample_timestamp = time.time()

            for project_slug, downloader_name in active_pairs.items():
                project_stats = await tracker.get_project_data(project_slug)
                if not project_stats:
                    # Do not convert a tracker outage into a zero-activity sample.
                    continue

                user_stats = tracker.build_user_stats(
                    project_stats, downloader_name, project_slug
                )
                items_done = user_stats.get("user_items_done", 0)
                bytes_used = user_stats.get("user_bytes", 0)
                baseline = _tracker_baselines.get(project_slug, {"items": 0, "bytes": 0})
                if isinstance(baseline, (int, float)):
                    baseline = {"items": baseline, "bytes": 0}

                previous_items = baseline.get("items", 0)
                previous_bytes = baseline.get("bytes", 0)
                items_delta = 0
                bytes_delta = 0

                if previous_items > 0 and items_done >= previous_items:
                    candidate_delta = items_done - previous_items
                    if candidate_delta <= MAX_TRACKER_DELTA_PER_POLL:
                        items_delta = candidate_delta
                        total_items_delta += items_delta
                    else:
                        logger.warning(
                            "Tracker items delta %d for '%s' exceeds cap %d â€” resetting baseline",
                            candidate_delta, project_slug, MAX_TRACKER_DELTA_PER_POLL,
                        )
                elif previous_items > 0 and items_done < previous_items:
                    logger.info(
                        "Tracker items for '%s' decreased (%d â†’ %d) â€” resetting baseline",
                        project_slug, previous_items, items_done,
                    )

                if previous_bytes > 0 and bytes_used >= previous_bytes:
                    bytes_delta = bytes_used - previous_bytes
                    total_bytes_delta += bytes_delta
                elif previous_bytes > 0 and bytes_used < previous_bytes:
                    logger.info(
                        "Tracker bytes for '%s' decreased (%d â†’ %d) â€” resetting baseline",
                        project_slug, previous_bytes, bytes_used,
                    )

                # Zero-delta samples are retained so 24-hour coverage is measurable.
                project_health.record_project_activity(
                    project_slug, items_delta, bytes_delta, timestamp=sample_timestamp
                )
                project_health.update_health_state_log(project_slug)
                _tracker_baselines[project_slug] = {
                    "items": items_done,
                    "bytes": bytes_used,
                }
                baselines_changed = True

            if baselines_changed:
                # Record the aggregate tracker deltas used by the main 24-hour
                # dashboard chart. Project Health records per-project samples
                # above, while the main history chart needs one combined sample
                # for the current polling cycle.
                history.record_tracker(
                    total_items_delta,
                    total_bytes_delta,
                )
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
                # Remove the entry first so a failure can never cause a retry loop every 30s
                paused_entry = _pause_state.pop(name, {})
                slug = paused_entry.get("project_slug", "")
                if name not in clients or not slug:
                    continue
                try:
                    success = await clients[name].select_project(slug)
                    if success:
                        logger.info("Auto-resumed %s with project %s", _safe_log(name), _safe_log(slug))
                    else:
                        logger.warning("Auto-resume failed for %s", _safe_log(name))
                except Exception as resume_error:
                    logger.warning("Auto-resume error for %s: %s", _safe_log(name), resume_error)

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
    global config, broadcast_task, tracker_task, auto_resume_task, automatic_failover_task

    config = DashboardConfig.load()
    logger.info('Dashboard "%s" starting', config.title)

    history.load()
    project_health.load()
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
    automatic_failover_task = asyncio.create_task(_automatic_failover_loop())
    yield

    for task in [broadcast_task, tracker_task, auto_resume_task, automatic_failover_task]:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    for client in clients.values():
        await client.stop()

    history.force_save()
    project_health.force_save()
    _save_pause_state()
    _save_tracker_baselines()

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
    last_broadcast_message = ""
    while True:
        try:
            await asyncio.sleep(_WS_BROADCAST_INTERVAL)
            for client in clients.values():
                s = client.status
                if s.connection_state == ConnectionState.ONLINE:
                    total_bytes = s.bytes_downloaded + s.bytes_uploaded
                    history.record(s.name, total_bytes)
            history.save()
            if not ws_connections:
                continue
            now = time.monotonic()
            state_dict = _build_live_dashboard_state().model_dump(mode="json")
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
            if message != last_broadcast_message:
                last_broadcast_message = message
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


def _build_live_dashboard_state():
    """Return the validated compact state used by WebSocket clients.

    HTTP endpoints continue to use the full DashboardState contract. The live
    contract keeps all grid-visible Warrior fields but reduces each item to its
    identifier and state, excluding large item names and task descriptions.
    """
    return LiveDashboardState.from_dashboard_state(_build_dashboard_state())


def _build_dashboard_state():
    instances = []
    total_online = 0
    total_offline = 0
    total_items_active = 0
    for client in clients.values():
        s = client.status
        instances.append(s)
        if s.restarting:
            # Expected transient state: do not count a requested restart as an outage
            continue
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
    """Dashboard config â€” rarely changes, safe to cache 5 minutes."""
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


# Must be declared BEFORE /api/instances/{name}, or "export" is treated as a name
@app.get("/api/instances/export")
async def export_instances(include_credentials: bool = False):
    """Download all instance configs as JSON (credentials omitted by default)."""
    export_document = instance_import.build_export(store.get_all(), include_credentials=include_credentials)
    file_name = "atw-instances-" + time.strftime("%Y%m%d-%H%M%S") + ".json"
    return Response(
        content=json.dumps(export_document, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="' + file_name + '"'},
    )


@app.get("/api/instances/{name}")
async def get_instance(name):
    if name not in clients:
        raise HTTPException(status_code=404, detail="Instance not found: " + name)
    return clients[name].status.model_dump()


# Bulk adds start warrior connections in small batches so 99 new warriors
# don't all open HTTP sessions in the same instant
_BULK_START_BATCH_SIZE = 10
_BULK_START_BATCH_DELAY_SECONDS = 0.5
_bulk_add_lock = asyncio.Lock()


def _create_client(instance_config):
    return WarriorClient(instance_config, reconnect_base=config.reconnect_base, reconnect_max=config.reconnect_max)


@app.post("/api/instances")
async def add_instance(request: AddInstanceRequest):
    # Validate and normalize through the same parser bulk import uses
    single_entry = {"name": request.name or "", "host": request.host, "port": request.port}
    try:
        parse_result = instance_import.parse_bulk_input(json.dumps([single_entry]), store.get_all())
    except instance_import.ImportError_ as import_error:
        raise HTTPException(status_code=400, detail=str(import_error))
    if parse_result["skipped"]:
        reason = parse_result["skipped"][0]["reason"]
        status_code = 409 if ("already" in reason or "in use" in reason) else 400
        raise HTTPException(status_code=status_code, detail=reason)

    new_entry = parse_result["to_add"][0]
    instance_name = new_entry["name"]
    if instance_name in clients:  # also guards against a stale store/clients mismatch
        raise HTTPException(status_code=409, detail="Instance already exists: " + instance_name)

    inst_config = WarriorInstanceConfig(
        name=instance_name, host=new_entry["host"], port=new_entry["port"],
        http_username=request.http_username, http_password=request.http_password,
    )
    client = _create_client(inst_config)
    clients[instance_name] = client
    await client.start(poll_interval=config.poll_interval)
    store.add(inst_config.model_dump())
    return {"status": "ok", "instance": instance_name}


@app.post("/api/instances/bulk")
async def bulk_add_instances(request: BulkAddRequest):
    """Bulk add / import. dry_run=True (default) returns the preview only."""
    async with _bulk_add_lock:  # two concurrent imports can't both pass the duplicate check
        try:
            parse_result = instance_import.parse_bulk_input(request.text, store.get_all())
        except instance_import.ImportError_ as import_error:
            raise HTTPException(status_code=400, detail=str(import_error))

        # Hide credentials in the preview/response
        preview_to_add = [
            {"name": entry["name"], "host": entry["host"], "port": entry["port"]}
            for entry in parse_result["to_add"]
        ]
        response_body = {
            "dry_run": request.dry_run,
            "format": parse_result["format"],
            "to_add": preview_to_add,
            "skipped": parse_result["skipped"],
            "added_count": 0,
        }
        if request.dry_run or not parse_result["to_add"]:
            return response_body

        new_configs = []
        for entry in parse_result["to_add"]:
            if entry["name"] in clients:
                continue  # defensive: clients/store drift
            new_configs.append(WarriorInstanceConfig(
                name=entry["name"], host=entry["host"], port=entry["port"],
                http_username=entry.get("http_username"), http_password=entry.get("http_password"),
            ))

        # Persist once for the whole batch
        saved_instances = store.get_all()
        saved_instances.extend(instance_config.model_dump() for instance_config in new_configs)
        store.save(saved_instances)

        # Register clients now (cards appear as "connecting"), start them in batches
        new_clients = []
        for instance_config in new_configs:
            client = _create_client(instance_config)
            clients[instance_config.name] = client
            new_clients.append(client)

        response_body["added_count"] = len(new_clients)
        logger.info("Bulk import (%s): added %d, skipped %d",
                    parse_result["format"], len(new_clients), len(parse_result["skipped"]))

    asyncio.create_task(_start_clients_in_batches(new_clients))
    return response_body


async def _start_clients_in_batches(new_clients):
    for batch_start in range(0, len(new_clients), _BULK_START_BATCH_SIZE):
        for client in new_clients[batch_start:batch_start + _BULK_START_BATCH_SIZE]:
            # Skip clients removed again before their turn came
            if clients.get(client.config.name) is not client:
                continue
            try:
                await client.start(poll_interval=config.poll_interval)
            except Exception as start_error:
                logger.warning("Failed to start %s: %s", _safe_log(client.config.name), start_error)
        await asyncio.sleep(_BULK_START_BATCH_DELAY_SECONDS)


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
    removed_client = clients.pop(name)
    try:
        await removed_client.stop()
    except Exception as stop_error:  # e.g. bulk-added client not started yet
        logger.debug("Stop during remove of %s: %s", _safe_log(name), stop_error)
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
    """Return cached projects plus manual-selection queue availability metadata.

    The backend owns the 30-minute refresh and stale-count policy. Clients must
    revalidate so a refreshed queue count is visible on their next request.
    """
    project_list = await projects.get_projects()
    return JSONResponse(
        content=project_list,
        headers={
            "Cache-Control": "no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
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


# -- Immediate restart -------------------------------------------------------
# /api/stop_now abandons in-progress items and relies on the warrior
# container's restart policy to relaunch it. Stagger requests so restarting a
# large fleet does not create a simultaneous reconnect / registry-download spike.
_RESTART_BATCH_SIZE = 10
_RESTART_BATCH_DELAY_SECONDS = 3
_MAX_RESTART_REQUEST = 1000
_restart_lock = asyncio.Lock()


async def _restart_one(name):
    client = clients.get(name)
    if not client:
        return name, {"status": "error", "detail": "Not found"}
    if client.status.restarting:
        return name, {"status": "ok", "note": "Already restarting"}
    if client.status.connection_state != ConnectionState.ONLINE:
        return name, {"status": "error", "detail": "Warrior is offline"}

    try:
        success = await client.restart_now()
    except Exception as restart_error:
        logger.error("Restart error for %s: %s", _safe_log(name), restart_error)
        return name, {"status": "error", "detail": "Restart request failed"}

    if success:
        logger.warning("Immediate restart requested for %s", _safe_log(name))
        return name, {"status": "ok"}
    return name, {"status": "error", "detail": "Warrior rejected restart request"}


@app.post("/api/restart")
async def restart_instances(request: RestartRequest):
    """Immediately restart selected warriors in batches of ten.

    This is intentionally destructive to in-progress work. The frontend must
    show an explicit confirmation before calling this endpoint.
    """
    # Preserve caller order while removing duplicates and empty names
    instance_names = list(dict.fromkeys(name for name in request.instance_names if name))
    if not instance_names:
        raise HTTPException(status_code=400, detail="No warrior instances specified")
    if len(instance_names) > _MAX_RESTART_REQUEST:
        raise HTTPException(
            status_code=400,
            detail="Cannot restart more than " + str(_MAX_RESTART_REQUEST) + " warriors at once",
        )
    if _restart_lock.locked():
        raise HTTPException(status_code=409, detail="Another restart operation is already running")

    results = {}
    async with _restart_lock:
        for batch_start in range(0, len(instance_names), _RESTART_BATCH_SIZE):
            batch_names = instance_names[batch_start:batch_start + _RESTART_BATCH_SIZE]
            batch_results = await asyncio.gather(*(_restart_one(name) for name in batch_names))
            for name, result in batch_results:
                results[name] = result

            # No delay after the final batch
            if batch_start + _RESTART_BATCH_SIZE < len(instance_names):
                await asyncio.sleep(_RESTART_BATCH_DELAY_SECONDS)

    accepted = sum(1 for result in results.values() if result.get("status") == "ok")
    failed = len(results) - accepted
    logger.warning("Restart operation complete: %d accepted, %d failed", accepted, failed)
    return {"results": results, "accepted": accepted, "failed": failed}


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
        # Save what the warrior is SET to ("auto" on ArchiveTeam's Choice), so
        # resume restores Choice instead of pinning the resolved project
        slug = client.status.selected_project or client.status.project_slug

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
            logger.info("Paused %s (project: %s, resume: %s)", _safe_log(name), _safe_log(slug), dur_str)
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
            logger.info("Resumed %s with project %s", _safe_log(name), _safe_log(slug))
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
            logger.warning("Tracker returned no data for slug: %s", _safe_log(slug))
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


# -- Project Health (v4.5.2, monitor-only, rolling baseline) --
@app.get("/api/project-health")
async def get_project_health_status():
    return project_health.get_status(client.status for client in clients.values())


@app.get("/api/project-health/history")
async def get_project_health_history(project: str = ""):
    return project_health.get_project_history(project.strip())


@app.put("/api/project-health/policy")
async def update_project_health_policy(request: ProjectHealthPolicyRequest):
    try:
        policy = project_health.configure_policy(
            request.primary_project,
            request.fallback_project,
            request.canary_percent,
            request.minimum_baseline_hours,
            request.full_baseline_hours,
            request.monitoring_enabled,
            request.automatic_failover_enabled,
            request.stalled_evaluations_required,
            request.cooldown_hours,
            request.resume_stabilization_minutes,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return {"status": "ok", "policy": policy}


@app.post("/api/project-health/preview")
async def preview_project_health_groups(request: ProjectHealthPreviewRequest):
    recommendation = project_health.recommend_canaries(
        (client.status for client in clients.values()), request.canary_percent
    )
    project_health.log_manual_failover_decision(
        "previewed",
        details={
            "canary_percent": recommendation["configured_percent"],
            "primary_canaries": [item["name"] for item in recommendation["primary_canaries"]],
            "fallback_fleet_count": len(recommendation["fallback_fleet"]),
        },
    )
    return recommendation


@app.put("/api/project-health/roles")
async def save_project_health_roles(request: ProjectHealthRoleAssignmentRequest):
    requested_names = set(request.primary_canaries).union(request.fallback_fleet)
    unknown_names = sorted(requested_names - set(clients.keys()))
    if unknown_names:
        raise HTTPException(status_code=400, detail="Unknown Warrior assignment(s): " + ", ".join(unknown_names))
    expected_count = project_health.calculate_canary_count(len(requested_names))
    if request.primary_canaries and len(request.primary_canaries) != expected_count:
        raise HTTPException(
            status_code=400,
            detail=f"Primary canary count must match the configured allocation ({expected_count} required)",
        )
    roles = project_health.assign_roles(request.primary_canaries, request.fallback_fleet)
    return {"status": "ok", "roles": roles, "failover": project_health.failover_availability()}


@app.post("/api/project-health/baseline/reset")
async def reset_project_health_baseline(request: ProjectHealthBaselineResetRequest):
    """Delete only the configured primary project's Project Health history."""
    policy = project_health.get_policy()
    primary_project = policy.get("primary_project", "")
    try:
        result = project_health.reset_primary_baseline(request.confirmation, request.actor)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    # The tracker total is cumulative. Removing this counter reference ensures
    # the next poll establishes a fresh zero-delta point instead of importing
    # activity from before the reset into the rebuilt baseline.
    if primary_project:
        _tracker_baselines.pop(primary_project, None)
        _save_tracker_baselines()
    return {"status": "ok", **result, "project_health": project_health.get_status(client.status for client in clients.values())}


@app.post("/api/project-health/decision")
async def record_project_health_decision(request: ManualFailoverDecisionRequest):
    try:
        entry = project_health.log_manual_failover_decision(
            request.action.value, request.actor, request.details
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    return {"status": "ok", "entry": entry}


_MANUAL_FAILOVER_BATCH_SIZE = 10
_MANUAL_FAILOVER_BATCH_DELAY_SECONDS = 1
_manual_failover_lock = asyncio.Lock()


async def _switch_failover_instance(name, fallback_project):
    client = clients.get(name)
    if not client:
        return name, {"status": "error", "detail": "Not found"}
    if client.status.connection_state != ConnectionState.ONLINE:
        return name, {"status": "error", "detail": "Warrior is offline"}
    if client.status.restarting:
        return name, {"status": "error", "detail": "Warrior is restarting"}
    if name in _pause_state:
        return name, {"status": "error", "detail": "Warrior is paused"}
    try:
        success = await client.change_project(fallback_project)
    except Exception as error:
        logger.error("Manual failover error for %s: %s", _safe_log(name), error)
        success = False
    return name, {"status": "ok" if success else "error", "detail": "" if success else "Project switch failed"}


@app.post("/api/project-health/failover")
async def execute_manual_project_failover(request: ManualFailoverRequest):
    """Switch only the saved fallback fleet. No background task calls this endpoint."""
    availability = project_health.failover_availability()
    if not availability["available"]:
        raise HTTPException(status_code=409, detail=availability["message"])
    if _manual_failover_lock.locked():
        raise HTTPException(status_code=409, detail="Another manual failover is running")

    policy = project_health.get_policy()
    assignments = project_health.get_role_assignments()
    fallback_names = [name for name, role in assignments.items() if role == "fallback_fleet"]
    primary_canaries = [name for name, role in assignments.items() if role == "primary_canary"]
    if not fallback_names:
        raise HTTPException(status_code=409, detail="No Fallback fleet is assigned")

    project_health.log_manual_failover_decision(
        "requested", request.actor,
        {"primary_project": policy["primary_project"], "fallback_project": policy["fallback_project"],
         "primary_canaries": primary_canaries, "fallback_fleet": fallback_names},
    )
    results = {}
    async with _manual_failover_lock:
        project_health.log_manual_failover_decision("confirmed", request.actor, {"confirmation": request.confirmation})
        for start in range(0, len(fallback_names), _MANUAL_FAILOVER_BATCH_SIZE):
            batch = fallback_names[start:start + _MANUAL_FAILOVER_BATCH_SIZE]
            for name, result in await asyncio.gather(*(
                _switch_failover_instance(name, policy["fallback_project"]) for name in batch
            )):
                results[name] = result
            if start + _MANUAL_FAILOVER_BATCH_SIZE < len(fallback_names):
                await asyncio.sleep(_MANUAL_FAILOVER_BATCH_DELAY_SECONDS)

    switched = sum(1 for result in results.values() if result["status"] == "ok")
    failed = len(results) - switched
    project_health.log_manual_failover_decision(
        "completed" if failed == 0 else "failed", request.actor,
        {"switched": switched, "failed": failed, "results": results,
         "primary_canaries_unchanged": primary_canaries},
    )
    return {
        "status": "ok" if failed == 0 else "partial", "mode": "manual",
        "automatic_switching_enabled": project_health.get_policy().get("automatic_failover_enabled", False),
        "primary_canaries_unchanged": primary_canaries,
        "fallback_project": policy["fallback_project"],
        "switched": switched, "failed": failed, "results": results,
    }


async def _execute_automatic_failover():
    policy = project_health.get_policy()
    assignments = project_health.get_role_assignments()
    fallback_names = [name for name, role in assignments.items() if role == "fallback_fleet"]
    primary_canaries = [name for name, role in assignments.items() if role == "primary_canary"]
    results = {}
    for start in range(0, len(fallback_names), _MANUAL_FAILOVER_BATCH_SIZE):
        # Turning monitoring or automation off stops new batches. Requests already
        # in flight finish, which avoids abandoning Warriors in an unknown state.
        current_policy = project_health.get_policy()
        if not current_policy.get("monitoring_enabled") or not current_policy.get("automatic_failover_enabled"):
            for skipped_name in fallback_names[start:]:
                results[skipped_name] = {"status": "error", "detail": "Automation disabled before batch started"}
            break
        batch = fallback_names[start:start + _MANUAL_FAILOVER_BATCH_SIZE]
        for name, result in await asyncio.gather(*(
            _switch_failover_instance(name, policy["fallback_project"]) for name in batch
        )):
            results[name] = result
        if start + _MANUAL_FAILOVER_BATCH_SIZE < len(fallback_names):
            await asyncio.sleep(_MANUAL_FAILOVER_BATCH_DELAY_SECONDS)
    switched = sum(1 for result in results.values() if result["status"] == "ok")
    failed = len(results) - switched
    project_health.complete_automatic_failover(switched, failed, results)
    logger.warning(
        "Automatic failover completed: %d switched, %d failed; %d primary canaries unchanged",
        switched, failed, len(primary_canaries),
    )


async def _automatic_failover_loop():
    """Evaluate automation every five minutes; automatic failback is not implemented."""
    while True:
        try:
            await asyncio.sleep(300)
            policy = project_health.get_policy()
            primary_project = policy.get("primary_project", "")
            if not primary_project or not policy.get("monitoring_enabled"):
                continue
            evaluation = project_health.update_health_state_log(primary_project)
            decision = project_health.register_automatic_evaluation(evaluation)
            if decision.get("trigger"):
                await _execute_automatic_failover()
        except asyncio.CancelledError:
            break
        except Exception as error:
            logger.exception("Automatic failover evaluator error: %s", error)
            await asyncio.sleep(30)


# -- WebSocket --
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_connections.append(ws)
    logger.info("WebSocket client connected (%d total)", len(ws_connections))
    try:
        init_state = _build_live_dashboard_state().model_dump(mode="json")
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
