"""Fetch and cache the active project list from WarriorHQ.

Also exposes the synthetic "ArchiveTeam's Choice" entry. Selecting it tells
the warrior to follow WarriorHQ's auto_project instead of a fixed project,
so the warrior switches automatically whenever ArchiveTeam changes the
default. The entry is always first in the returned list.
"""

import asyncio
import logging
import time

import httpx

from backend import tracker

logger = logging.getLogger(__name__)

PROJECTS_URL = "https://warriorhq.archiveteam.org/projects.json"
CACHE_TTL = 1800  # 30 minutes
TODO_STALE_SECONDS = 3600
TODO_CONCURRENCY = 5
TODO_REQUEST_TIMEOUT = 10.0

# Value the warrior stores in its selected_project setting for
# "ArchiveTeam's Choice". Kept in one place so it is easy to change if the
# warrior ever uses a different value.
AUTO_PROJECT_SLUG = "auto"
AUTO_PROJECT_TITLE = "ArchiveTeam's Choice"

_cache: list[dict] = []
_cache_time: float = 0
_auto_project_target: str = ""  # Real project slug the Choice currently resolves to
_lock = asyncio.Lock()
_todo_cache: dict[str, dict] = {}


def is_auto(project_slug) -> bool:
    """Return True if the slug is the ArchiveTeam's Choice value."""
    return isinstance(project_slug, str) and project_slug == AUTO_PROJECT_SLUG


def get_auto_project_target() -> str:
    """Return the real project slug WarriorHQ currently points the Choice at ('' if unknown)."""
    return _auto_project_target


def _todo_fields(project_slug: str, now: float | None = None) -> dict:
    current_time = time.time() if now is None else now
    cached = _todo_cache.get(project_slug)
    if not cached:
        return {"todo_items": None, "todo_updated_at": None, "todo_stale": False}
    updated_at = float(cached.get("updated_at", 0.0))
    return {
        "todo_items": cached.get("items"),
        "todo_updated_at": updated_at or None,
        "todo_stale": bool(updated_at and current_time - updated_at >= TODO_STALE_SECONDS),
    }


async def _fetch_project_todo(client, semaphore, project_slug):
    async with semaphore:
        try:
            response = await client.get(tracker.STATS_URL.format(project=project_slug))
            response.raise_for_status()
            todo_items = tracker.extract_todo_count(response.json())
            if todo_items is None:
                raise ValueError("tracker payload did not contain a to-do count")
            return project_slug, todo_items, None
        except Exception as error:
            return project_slug, None, error


async def _refresh_todo_counts(project_slugs: list[str]) -> None:
    if not project_slugs:
        return
    semaphore = asyncio.Semaphore(TODO_CONCURRENCY)
    async with httpx.AsyncClient(timeout=httpx.Timeout(TODO_REQUEST_TIMEOUT)) as client:
        results = await asyncio.gather(*(
            _fetch_project_todo(client, semaphore, project_slug)
            for project_slug in project_slugs
        ))
    refreshed_at = time.time()
    successful = 0
    for project_slug, todo_items, error in results:
        if error is None:
            _todo_cache[project_slug] = {"items": todo_items, "updated_at": refreshed_at}
            successful += 1
        else:
            logger.debug("Could not refresh to-do count for %s: %s", project_slug, error)
    logger.info("Refreshed to-do counts for %d of %d projects", successful, len(project_slugs))


def _build_auto_entry() -> dict:
    """Build the synthetic ArchiveTeam's Choice entry for the project list."""
    description = "Automatically work on whichever project ArchiveTeam currently needs most."
    if _auto_project_target:
        description += " Currently: " + _auto_project_target
    return {
        "name": AUTO_PROJECT_SLUG,
        "title": AUTO_PROJECT_TITLE,
        "description": description,
        "logo": "",
        "is_auto": True,
        "resolves_to": _auto_project_target,
        "todo_items": None,
        "todo_updated_at": None,
        "todo_stale": False,
    }


def _with_auto_entry_first(project_entries: list[dict]) -> list[dict]:
    """Return the project list with the Choice entry first and real projects sorted by title."""
    real_projects = [
        entry for entry in project_entries
        if not is_auto(entry.get("name"))
    ]
    real_projects.sort(key=lambda entry: (entry.get("title") or entry.get("name") or "").lower())
    return [_build_auto_entry()] + real_projects


async def get_projects(force: bool = False) -> list[dict]:
    """Return cached project list (Choice first), refreshing if stale."""
    global _cache, _cache_time, _auto_project_target

    if not force and _cache and (time.monotonic() - _cache_time) < CACHE_TTL:
        return _cache

    async with _lock:
        # Double-check after acquiring lock
        if not force and _cache and (time.monotonic() - _cache_time) < CACHE_TTL:
            return _cache

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(PROJECTS_URL)
                response.raise_for_status()
                data = response.json()

            raw_projects = []
            if isinstance(data, list):
                raw_projects = data
            elif isinstance(data, dict):
                # WarriorHQ wraps the list in {"projects": [...], "auto_project": "..."}
                raw_projects = data.get("projects", [])
                auto_target = data.get("auto_project", "")
                if isinstance(auto_target, str):
                    if auto_target != _auto_project_target:
                        logger.info(
                            "ArchiveTeam's Choice now resolves to: %s",
                            auto_target or "(none)",
                        )
                    _auto_project_target = auto_target

            cleaned_projects = []
            for project in raw_projects:
                if isinstance(project, dict) and project.get("name"):
                    project_slug = project.get("name", "")
                    cleaned_projects.append({
                        "name": project_slug,
                        "title": project.get("title", project_slug),
                        "description": project.get("description", ""),
                        "logo": project.get("logo", ""),
                        "is_auto": False,
                        "resolves_to": "",
                        **_todo_fields(project_slug),
                    })

            await _refresh_todo_counts([entry["name"] for entry in cleaned_projects])
            for cleaned_project in cleaned_projects:
                cleaned_project.update(_todo_fields(cleaned_project["name"]))
            _cache = _with_auto_entry_first(cleaned_projects)
            _cache_time = time.monotonic()
            logger.info("Refreshed project list: %d projects (+ ArchiveTeam's Choice)", len(cleaned_projects))
            return _cache

        except Exception as exc:
            logger.warning("Failed to fetch projects from WarriorHQ: %s", exc)
            # Stale cache on error; Choice stays selectable even if WarriorHQ was never reachable
            if _cache:
                return _cache
            return [_build_auto_entry()]
