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
_PROJECT_HEALTH_EVALUATION_INTERVAL_SECONDS = 5 * 60
_PRIMARY_TODO_REFRESH_SECONDS = 15 * 60
_FALLBACK_TODO_REFRESH_SECONDS = 30 * 60
_CONFIRMATION_TODO_REFRESH_SECONDS = 5 * 60
_LOW_RUNWAY_MONITORING_MINUTES = 60.0
_LOW_RUNWAY_EXIT_MINUTES = 90.0

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
# Baselines older than this cannot be assigned to the current chart interval.
MAX_TRACKER_SAMPLE_GAP = 180

_tracker_baselines = {}  # {project_slug: {items, bytes, updated_at}}


def _load_tracker_baselines():
    global _tracker_baselines
    if not os.path.exists(TRACKER_BASELINE_FILE):
        return
    try:
        with open(TRACKER_BASELINE_FILE, "r") as fh:
            loaded_baselines = json.load(fh)
        if not isinstance(loaded_baselines, dict):
            raise ValueError("Tracker baseline root must be an object")

        normalized_baselines = {}
        for slug, value in loaded_baselines.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                value = {"items": value, "bytes": 0, "updated_at": 0}
            elif not isinstance(value, dict):
                value = {"items": 0, "bytes": 0, "updated_at": 0}
            else:
                value = dict(value)
                value.setdefault("items", 0)
                value.setdefault("bytes", 0)
                timestamp = value.get("updated_at")
                if (
                    isinstance(timestamp, bool)
                    or not isinstance(timestamp, (int, float))
                    or timestamp <= 0
                ):
                    value["updated_at"] = 0
            normalized_baselines[str(slug)] = value

        _tracker_baselines = normalized_baselines
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


async def _refresh_project_health_queue_evidence(force: bool = False) -> dict[str, dict]:
    """Refresh Project Health queue evidence without treating unavailable TODO as zero."""
    entries = await projects.get_projects(force=force)
    evidence = {}
    for entry in entries:
        project_slug = str(entry.get("name") or "").strip()
        if not project_slug or projects.is_auto(project_slug):
            continue
        evidence[project_slug] = project_health.update_project_queue(
            project_slug,
            entry.get("todo_items"),
            updated_at=entry.get("todo_updated_at"),
            stale=bool(entry.get("todo_stale", False)),
        )
    return evidence


def _eligible_warrior_count(project_slug: str) -> int:
    """Count dashboard-managed Warriors contributing to one project sample."""
    return sum(
        1
        for name, client in clients.items()
        if client.status.connection_state == ConnectionState.ONLINE
        and not client.status.restarting
        and name not in _pause_state
        and client.status.project_slug == project_slug
    )


async def _tracker_poll_loop():
    """Poll the ArchiveTeam tracker for per-project item and byte deltas.

    Iterates all active project/downloader pairs, tracks timestamped baselines
    per project, rejects stale catch-up deltas, and records aggregate deltas for
    the 24-hour history chart.
    """
    while True:
        try:
            await asyncio.sleep(60)

            # Keep WarriorHQ's ArchiveTeam's Choice target fresh. The project
            # module serves its normal 30-minute cache when it remains current.
            try:
                await _refresh_project_health_queue_evidence()
            except Exception as projects_error:
                logger.debug("Project list and queue refresh failed: %s", projects_error)

            active_pairs = {}
            for client in clients.values():
                warrior_status = client.status
                if warrior_status.connection_state != ConnectionState.ONLINE:
                    continue
                if not warrior_status.downloader or not warrior_status.project_slug:
                    continue
                active_pairs.setdefault(
                    warrior_status.project_slug,
                    warrior_status.downloader,
                )

            if not active_pairs:
                continue

            baselines_changed = False
            total_items_delta = 0
            total_bytes_delta = 0
            sample_timestamp = time.time()

            for project_slug, downloader_name in active_pairs.items():
                project_stats = await tracker.get_project_data(project_slug)
                if not project_stats:
                    # Preserve the old successful timestamp through an outage so
                    # the next success can recognize and reject a stale gap.
                    continue

                user_stats = tracker.build_user_stats(
                    project_stats,
                    downloader_name,
                    project_slug,
                )
                items_done = user_stats.get("user_items_done", 0)
                bytes_used = user_stats.get("user_bytes", 0)

                baseline = _tracker_baselines.get(
                    project_slug,
                    {"items": 0, "bytes": 0, "updated_at": 0},
                )
                if isinstance(baseline, (int, float)) and not isinstance(baseline, bool):
                    baseline = {"items": baseline, "bytes": 0, "updated_at": 0}
                elif not isinstance(baseline, dict):
                    baseline = {"items": 0, "bytes": 0, "updated_at": 0}

                previous_items = baseline.get("items", 0)
                previous_bytes = baseline.get("bytes", 0)
                previous_timestamp = baseline.get("updated_at", 0)
                if (
                    isinstance(previous_timestamp, bool)
                    or not isinstance(previous_timestamp, (int, float))
                ):
                    previous_timestamp = 0

                items_delta = 0
                bytes_delta = 0
                elapsed_seconds = (
                    sample_timestamp - previous_timestamp
                    if previous_timestamp > 0
                    else None
                )
                sample_gap_valid = (
                    elapsed_seconds is not None
                    and 0 < elapsed_seconds <= MAX_TRACKER_SAMPLE_GAP
                )

                if not sample_gap_valid:
                    if previous_timestamp > 0 and elapsed_seconds is not None:
                        logger.info(
                            "Tracker sample gap %.1fs for '%s' exceeds valid range; resetting baseline",
                            elapsed_seconds,
                            project_slug,
                        )
                    elif previous_items > 0 or previous_bytes > 0:
                        logger.info(
                            "Tracker baseline for '%s' has no timestamp; migrating without catch-up delta",
                            project_slug,
                        )
                else:
                    if previous_items > 0 and items_done >= previous_items:
                        candidate_delta = items_done - previous_items
                        if candidate_delta <= MAX_TRACKER_DELTA_PER_POLL:
                            items_delta = candidate_delta
                            total_items_delta += items_delta
                        else:
                            logger.warning(
                                "Tracker items delta %d for '%s' exceeds cap %d; resetting baseline",
                                candidate_delta,
                                project_slug,
                                MAX_TRACKER_DELTA_PER_POLL,
                            )
                    elif previous_items > 0 and items_done < previous_items:
                        logger.info(
                            "Tracker items for '%s' decreased (%d to %d); resetting baseline",
                            project_slug,
                            previous_items,
                            items_done,
                        )

                    if previous_bytes > 0 and bytes_used >= previous_bytes:
                        bytes_delta = bytes_used - previous_bytes
                        total_bytes_delta += bytes_delta
                    elif previous_bytes > 0 and bytes_used < previous_bytes:
                        logger.info(
                            "Tracker bytes for '%s' decreased (%d to %d); resetting baseline",
                            project_slug,
                            previous_bytes,
                            bytes_used,
                        )

                # Project Health compares tracker activity per contributing,
                # dashboard-managed Warrior. Fleet-size changes therefore do not
                # look like throughput collapses when per-Warrior work is stable.
                eligible_warriors = _eligible_warrior_count(project_slug)
                if eligible_warriors > 0:
                    project_health.record_project_activity(
                        project_slug,
                        items_delta,
                        bytes_delta,
                        eligible_warriors=eligible_warriors,
                        timestamp=sample_timestamp,
                    )
                    project_health.update_health_state_log(project_slug)

                # Every successful tracker observation becomes the new baseline,
                # even when migration or stale-gap protection rejected its delta.
                _tracker_baselines[project_slug] = {
                    "items": items_done,
                    "bytes": bytes_used,
                    "updated_at": sample_timestamp,
                }
                baselines_changed = True

            if baselines_changed:
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
    global _pending_primary_change
    previous_policy = project_health.get_policy()
    previous_primary_project = previous_policy.get("primary_project", "")
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
    primary_project = policy.get("primary_project", "")
    if previous_primary_project != primary_project:
        _pending_primary_change = {
            "from": previous_primary_project,
            "to": primary_project,
        }
    return {
        "status": "ok",
        "policy": policy,
        "primary_changed": previous_primary_project != primary_project,
        "previous_primary_project": previous_primary_project,
        "primary_project": primary_project,
        "fleet_placement": project_health.get_fleet_placement(
            client.status for client in clients.values()
        ),
    }


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
_pending_primary_change = {"from": "", "to": ""}


def _summarize_project_switch_results(results):
    categories = {
        "switched": 0,
        "already_at_target": 0,
        "skipped_unrelated_project": 0,
        "skipped_project_unknown": 0,
        "failed": 0,
    }
    for result in results.values():
        category = result.get("status", "failed")
        if category in categories:
            categories[category] += 1
        else:
            categories["failed"] += 1
    return categories


async def _switch_failover_instance(
    name,
    target_project,
    policy,
    additional_allowed_projects=(),
):
    """Switch one role-managed Warrior without overriding third-project work."""
    client = clients.get(name)
    if not client:
        return name, {"status": "failed", "detail": "Not found"}
    if client.status.connection_state != ConnectionState.ONLINE:
        return name, {"status": "failed", "detail": "Warrior is offline"}
    if client.status.restarting:
        return name, {"status": "failed", "detail": "Warrior is restarting"}
    if name in _pause_state:
        return name, {"status": "failed", "detail": "Warrior is paused"}

    current_project = str(client.status.project_slug or "").strip()
    primary_project = str(policy.get("primary_project") or "").strip()
    secondary_project = str(policy.get("fallback_project") or "").strip()
    if not current_project:
        return name, {
            "status": "skipped_project_unknown",
            "detail": "Current project is unknown",
        }
    allowed_projects = {
        project for project in (
            primary_project,
            secondary_project,
            *additional_allowed_projects,
        ) if project
    }
    if current_project not in allowed_projects:
        return name, {
            "status": "skipped_unrelated_project",
            "detail": "Warrior is intentionally assigned to an unrelated project",
            "current_project": current_project,
        }
    if current_project == target_project:
        return name, {
            "status": "already_at_target",
            "detail": "Warrior is already on the requested project",
            "current_project": current_project,
        }
    try:
        success = await client.change_project(target_project)
    except Exception as error:
        logger.error("Project Health switch error for %s: %s", _safe_log(name), error)
        success = False
    return name, {
        "status": "switched" if success else "failed",
        "detail": "" if success else "Project switch failed",
        "from_project": current_project,
        "target_project": target_project,
    }


async def _restart_switched_project_health_warriors(batch, results):
    """Request immediate restarts only after successful project changes.

    Restart outcomes are independent of project-switch outcomes. A rejected
    restart leaves the selected destination intact but reports an incomplete
    immediate transition; no retry is attempted automatically.
    """
    candidates = [name for name in batch if results[name].get("status") == "switched"]
    if not candidates:
        return {}
    if _restart_lock.locked():
        return {
            name: {"status": "error", "detail": "Another restart operation is running"}
            for name in candidates
        }
    async with _restart_lock:
        return dict(await asyncio.gather(*(_restart_one(name) for name in candidates)))


def _summarize_project_health_restarts(restart_results):
    return {
        "restart_requested": len(restart_results),
        "restart_accepted": sum(
            result.get("status") == "ok" for result in restart_results.values()
        ),
        "restart_failed": sum(
            result.get("status") != "ok" for result in restart_results.values()
        ),
    }


@app.post("/api/project-health/failover")
async def execute_manual_project_failover(request: ManualFailoverRequest):
    """Move the saved fallback fleet to the configured primary or secondary project.

    Manual switching intentionally does not require baseline readiness. The
    operator may use these controls to put non-critical Warriors back to useful
    work even while Project Health is building or monitoring is disabled.
    """
    if _manual_failover_lock.locked():
        raise HTTPException(status_code=409, detail="Another manual failover is running")

    policy = project_health.get_policy()
    assignments = project_health.get_role_assignments()
    fallback_names = [name for name, role in assignments.items() if role == "fallback_fleet"]
    primary_canaries = [name for name, role in assignments.items() if role == "primary_canary"]
    primary_change_request = (
        request.actor == "dashboard-primary-change"
        and request.target.value == "primary"
        and _pending_primary_change.get("to") == policy.get("primary_project")
    )
    operation_names = list(fallback_names)
    if primary_change_request:
        operation_names.extend(
            name for name in primary_canaries if name not in operation_names
        )
    if not operation_names:
        raise HTTPException(status_code=409, detail="No Project Health Warriors are assigned")

    target_role = request.target.value
    target_project = (
        policy.get("primary_project", "")
        if target_role == "primary"
        else policy.get("fallback_project", "")
    )
    if not target_project:
        raise HTTPException(
            status_code=409,
            detail=f"No {target_role} project is configured",
        )

    event_details = {
        "target_role": target_role,
        "target_project": target_project,
        "primary_project": policy.get("primary_project", ""),
        "fallback_project": policy.get("fallback_project", ""),
        "primary_canaries": primary_canaries,
        "fallback_fleet": fallback_names,
        "switch_mode": request.switch_mode.value,
    }
    project_health.log_manual_failover_decision(
        "requested",
        request.actor,
        event_details,
    )

    results = {}
    restart_results = {}
    async with _manual_failover_lock:
        project_health.log_manual_failover_decision(
            "confirmed",
            request.actor,
            {"target_role": target_role, "target_project": target_project},
        )
        additional_allowed_projects = (
            (_pending_primary_change.get("from", ""),)
            if primary_change_request
            else ()
        )
        for start in range(0, len(operation_names), _MANUAL_FAILOVER_BATCH_SIZE):
            batch = operation_names[start:start + _MANUAL_FAILOVER_BATCH_SIZE]
            for name, result in await asyncio.gather(*(
                _switch_failover_instance(
                    name,
                    target_project,
                    policy,
                    additional_allowed_projects,
                )
                for name in batch
            )):
                results[name] = result
            if request.switch_mode.value == "immediate":
                restart_results.update(
                    await _restart_switched_project_health_warriors(batch, results)
                )
            if start + _MANUAL_FAILOVER_BATCH_SIZE < len(operation_names):
                await asyncio.sleep(_MANUAL_FAILOVER_BATCH_DELAY_SECONDS)

        # Reconcile automation against the actual managed fleet after all batches.
        # This must remain under the manual-operation lock so another manual
        # switch cannot interleave before runtime synchronization finishes.
        fleet_mode_result = project_health.complete_manual_fleet_switch(
            destination=target_role,
            instances=(client.status for client in clients.values()),
            actor=request.actor,
        )

    summary = _summarize_project_switch_results(results)
    restart_summary = _summarize_project_health_restarts(restart_results)
    canary_results = {
        name: results[name]
        for name in primary_canaries
        if name in results
    }
    if primary_change_request:
        _pending_primary_change = {"from": "", "to": ""}
    final_details = {
        **event_details,
        **summary,
        "results": results,
        "primary_canaries_unchanged": [] if primary_change_request else primary_canaries,
        "primary_canary_results": canary_results,
        "primary_change_request": primary_change_request,
        "fleet_mode_result": fleet_mode_result,
        "restart_results": restart_results,
        **restart_summary,
        "fleet_placement": project_health.get_fleet_placement(
            client.status for client in clients.values()
        ),
    }
    project_health.log_manual_failover_decision(
        "completed" if summary["failed"] == 0 and restart_summary["restart_failed"] == 0 else "failed",
        request.actor,
        final_details,
    )
    return {
        "status": "ok" if summary["failed"] == 0 and restart_summary["restart_failed"] == 0 else "partial",
        "mode": "manual",
        "target": target_role,
        "target_project": target_project,
        "automatic_switching_enabled": policy.get("automatic_failover_enabled", False),
        "primary_canaries_unchanged": [] if primary_change_request else primary_canaries,
        "primary_canary_results": canary_results,
        "primary_change_request": primary_change_request,
        "switch_mode": request.switch_mode.value,
        **summary,
        **restart_summary,
        "restart_results": restart_results,
        "results": results,
        "project_health": project_health.get_status(
            client.status for client in clients.values()
        ),
    }


async def _execute_automatic_failover():
    policy = project_health.get_policy()
    assignments = project_health.get_role_assignments()
    fallback_names = [name for name, role in assignments.items() if role == "fallback_fleet"]
    primary_canaries = [name for name, role in assignments.items() if role == "primary_canary"]
    results = {}
    restart_results = {}
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
            _switch_failover_instance(name, policy["fallback_project"], policy) for name in batch
        )):
            results[name] = result
        restart_results.update(
            await _restart_switched_project_health_warriors(batch, results)
        )
        if start + _MANUAL_FAILOVER_BATCH_SIZE < len(fallback_names):
            await asyncio.sleep(_MANUAL_FAILOVER_BATCH_DELAY_SECONDS)
    summary = _summarize_project_switch_results(results)
    restart_summary = _summarize_project_health_restarts(restart_results)
    if restart_summary["restart_failed"]:
        logger.warning(
            "Project Health %s: %d restart requests failed: %s",
            "failover" if "failover" in "_execute_automatic_failover" else "failback",
            restart_summary["restart_failed"],
            restart_results,
        )
    project_health.complete_automatic_failover(
        summary["switched"],
        summary["failed"],
        results,
    )
    logger.warning(
        "Automatic failover completed: %d switched, %d already target, "
        "%d unrelated skipped, %d unknown skipped, %d failed; "
        "%d primary canaries unchanged",
        summary["switched"],
        summary["already_at_target"],
        summary["skipped_unrelated_project"],
        summary["skipped_project_unknown"],
        summary["failed"],
        len(primary_canaries),
    )


async def _execute_automatic_failback():
    """Return eligible Fallback-fleet Warriors from secondary to primary."""
    policy = project_health.get_policy()
    assignments = project_health.get_role_assignments()
    fallback_names = [
        name for name, role in assignments.items()
        if role == "fallback_fleet"
    ]
    results = {}
    restart_results = {}
    for start in range(0, len(fallback_names), _MANUAL_FAILOVER_BATCH_SIZE):
        current_policy = project_health.get_policy()
        if (
            not current_policy.get("monitoring_enabled")
            or not current_policy.get("automatic_failover_enabled")
        ):
            for skipped_name in fallback_names[start:]:
                results[skipped_name] = {
                    "status": "failed",
                    "detail": "Automation disabled before failback batch started",
                }
            break
        batch = fallback_names[start:start + _MANUAL_FAILOVER_BATCH_SIZE]
        for name, result in await asyncio.gather(*(
            _switch_failover_instance(
                name,
                policy["primary_project"],
                policy,
            )
            for name in batch
        )):
            results[name] = result
        restart_results.update(
            await _restart_switched_project_health_warriors(batch, results)
        )
        if start + _MANUAL_FAILOVER_BATCH_SIZE < len(fallback_names):
            await asyncio.sleep(_MANUAL_FAILOVER_BATCH_DELAY_SECONDS)
    summary = _summarize_project_switch_results(results)
    restart_summary = _summarize_project_health_restarts(restart_results)
    if restart_summary["restart_failed"]:
        logger.warning(
            "Project Health %s: %d restart requests failed: %s",
            "failover" if "failover" in "_execute_automatic_failback" else "failback",
            restart_summary["restart_failed"],
            restart_results,
        )
    project_health.complete_automatic_failback(
        summary["switched"],
        summary["failed"],
        results,
    )
    logger.warning(
        "Automatic failback completed: %d switched to primary, "
        "%d already primary, %d unrelated skipped, %d unknown skipped, "
        "%d failed",
        summary["switched"],
        summary["already_at_target"],
        summary["skipped_unrelated_project"],
        summary["skipped_project_unknown"],
        summary["failed"],
    )


def _reconcile_project_health_startup_placement() -> bool:
    """Reconcile persisted mode once actual fallback placement is observable.

    Never infer placement from a default or missing project during startup.
    An unresolved Warrior delays reconciliation rather than incorrectly
    declaring the managed fleet fully primary or secondary.
    """
    assignments = project_health.get_role_assignments()
    fallback_names = [
        name for name, role in assignments.items() if role == "fallback_fleet"
    ]
    if not fallback_names:
        return True

    observed = []
    for name in fallback_names:
        client = clients.get(name)
        if client is None:
            return False
        status = client.status
        if (
            status.connection_state != ConnectionState.ONLINE
            or not str(status.project_slug or "").strip()
        ):
            return False
        observed.append(status)

    if _manual_failover_lock.locked():
        return False

    placement = project_health.get_fleet_placement(observed)
    counts = placement["counts"]
    total = len(fallback_names)
    actual_mode = (
        "primary" if counts["primary"] == total
        else "secondary" if counts["secondary"] == total
        else "mixed"
    )
    if project_health.get_runtime().get("fleet_mode") != actual_mode:
        project_health.complete_manual_fleet_switch(
            destination="primary" if actual_mode == "primary" else "secondary",
            instances=observed,
            actor="startup-reconciliation",
        )
        logger.warning("Project Health startup reconciled fleet mode to %s", actual_mode)
    return True


async def _automatic_failover_loop():
    """Evaluate every five minutes and refresh TODO evidence adaptively.

    Healthy operation uses the cached project list with a 15-minute primary and
    30-minute fallback target. The first adverse evaluation, an active
    confirmation sequence, or a low-runway project forces a fresh queue lookup.
    A final forced lookup verifies the fallback immediately before switching.
    """
    startup_reconciled = False
    queue_monitoring_accelerated = False
    last_primary_todo_refresh_at = 0.0
    last_fallback_todo_refresh_at = 0.0

    while True:
        try:
            await asyncio.sleep(_PROJECT_HEALTH_EVALUATION_INTERVAL_SECONDS)
            if not startup_reconciled:
                startup_reconciled = _reconcile_project_health_startup_placement()
                if not startup_reconciled:
                    continue
            current_time = time.time()
            policy = project_health.get_policy()
            primary_project = policy.get("primary_project", "")
            fallback_project = policy.get("fallback_project", "")
            if not primary_project or not policy.get("monitoring_enabled"):
                continue

            evaluation = project_health.update_health_state_log(primary_project)
            runtime = project_health.get_runtime()
            confirmation_active = int(runtime.get("consecutive_stalled_evaluations", 0)) > 0
            adverse_evaluation = evaluation.get("decision_reason") in {
                "queue_exhausted",
                "queue_low",
                "throughput_stalled",
            }

            primary_queue = evaluation.get("queue", {})
            cached_runway = primary_queue.get("runway_minutes")
            if isinstance(cached_runway, (int, float)) and not isinstance(cached_runway, bool):
                if cached_runway < _LOW_RUNWAY_MONITORING_MINUTES:
                    queue_monitoring_accelerated = True
                elif cached_runway > _LOW_RUNWAY_EXIT_MINUTES:
                    queue_monitoring_accelerated = False

            primary_refresh_due = (
                current_time - last_primary_todo_refresh_at >= _PRIMARY_TODO_REFRESH_SECONDS
            )
            fallback_refresh_due = (
                current_time - last_fallback_todo_refresh_at >= _FALLBACK_TODO_REFRESH_SECONDS
            )
            confirmation_refresh_due = confirmation_active and (
                current_time - min(last_primary_todo_refresh_at, last_fallback_todo_refresh_at)
                >= _CONFIRMATION_TODO_REFRESH_SECONDS
            )
            force_queue_refresh = (
                adverse_evaluation
                or confirmation_refresh_due
                or queue_monitoring_accelerated
                or primary_refresh_due
                or fallback_refresh_due
            )

            queue_evidence = {}
            if force_queue_refresh:
                try:
                    queue_evidence = await _refresh_project_health_queue_evidence(force=True)
                    last_primary_todo_refresh_at = current_time
                    last_fallback_todo_refresh_at = current_time
                    # Re-evaluate after refreshing TODO so the confirmation uses
                    # the freshest queue evidence, not the cached classification.
                    evaluation = project_health.update_health_state_log(primary_project)
                except Exception as projects_error:
                    logger.debug("Project Health queue refresh failed: %s", projects_error)

            fallback_queue = queue_evidence.get(fallback_project)
            if fallback_queue is None and fallback_project:
                fallback_queue = project_health.get_project_queue(fallback_project)
            fallback_explicitly_empty = bool(
                fallback_queue
                and fallback_queue.get("available")
                and not fallback_queue.get("stale")
                and fallback_queue.get("todo_items") == 0
            )
            if fallback_explicitly_empty:
                logger.warning(
                    "Automatic failover held: fallback project '%s' reports zero queued items",
                    fallback_project,
                )
                continue

            runtime = project_health.get_runtime()
            if runtime.get("fleet_mode") in ("secondary", "mixed"):
                recovery_decision = (
                    project_health.register_automatic_recovery_evaluation(
                        evaluation
                    )
                )
                if recovery_decision.get("trigger"):
                    # Recheck primary TODO immediately before returning the fleet.
                    try:
                        final_queue = await _refresh_project_health_queue_evidence(
                            force=True
                        )
                        last_primary_todo_refresh_at = time.time()
                        last_fallback_todo_refresh_at = last_primary_todo_refresh_at
                        primary_queue = final_queue.get(primary_project)
                    except Exception as projects_error:
                        logger.debug(
                            "Final primary queue refresh failed: %s",
                            projects_error,
                        )
                        primary_queue = project_health.get_project_queue(
                            primary_project
                        )
                    runway_minutes = (
                        primary_queue.get("runway_minutes")
                        if primary_queue
                        else None
                    )
                    primary_queue_sufficient = (
                        primary_queue
                        and primary_queue.get("available")
                        and not primary_queue.get("stale")
                        and isinstance(runway_minutes, (int, float))
                        and not isinstance(runway_minutes, bool)
                        and runway_minutes
                        >= project_health.FAILBACK_MINIMUM_RUNWAY_MINUTES
                    )
                    if primary_queue_sufficient:
                        await _execute_automatic_failback()
                    else:
                        logger.warning(
                            "Automatic failback cancelled: primary queue no "
                            "longer has at least %.0f minutes of runway",
                            project_health.FAILBACK_MINIMUM_RUNWAY_MINUTES,
                        )
                    continue
                # While the fleet is on secondary, only evaluate recovery.
                continue

            decision = project_health.register_automatic_evaluation(evaluation)
            if not decision.get("trigger"):
                continue

            # Verify the fallback one final time immediately before changing the
            # fleet. TODO unavailable remains neutral; a fresh explicit zero holds.
            try:
                final_queue = await _refresh_project_health_queue_evidence(force=True)
                last_primary_todo_refresh_at = time.time()
                last_fallback_todo_refresh_at = last_primary_todo_refresh_at
                fallback_queue = final_queue.get(fallback_project)
            except Exception as projects_error:
                logger.debug("Final fallback queue refresh failed: %s", projects_error)
                fallback_queue = project_health.get_project_queue(fallback_project)

            if (
                fallback_queue
                and fallback_queue.get("available")
                and not fallback_queue.get("stale")
                and fallback_queue.get("todo_items") == 0
            ):
                logger.warning(
                    "Automatic failover cancelled: fallback project '%s' now reports zero queued items",
                    fallback_project,
                )
                project_health.complete_automatic_failover(
                    False,
                    fallback_project,
                    "Fallback reports zero queued items",
                )
                continue

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
