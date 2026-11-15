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

logger = logging.getLogger(__name__)

PROJECTS_URL = "https://warriorhq.archiveteam.org/projects.json"
CACHE_TTL = 1800  # 30 minutes

# Value the warrior stores in its selected_project setting for
# "ArchiveTeam's Choice". Kept in one place so it is easy to change if the
# warrior ever uses a different value.
AUTO_PROJECT_SLUG = "auto"
AUTO_PROJECT_TITLE = "ArchiveTeam's Choice"

_cache: list[dict] = []
_cache_time: float = 0
_auto_project_target: str = ""  # Real project slug the Choice currently resolves to
_lock = asyncio.Lock()


def is_auto(project_slug) -> bool:
    """Return True if the slug is the ArchiveTeam's Choice value."""
    return isinstance(project_slug, str) and project_slug == AUTO_PROJECT_SLUG


def get_auto_project_target() -> str:
    """Return the real project slug WarriorHQ currently points the Choice at ('' if unknown)."""
    return _auto_project_target


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
                    cleaned_projects.append({
                        "name": project.get("name", ""),
                        "title": project.get("title", project.get("name", "")),
                        "description": project.get("description", ""),
                        "logo": project.get("logo", ""),
                        "is_auto": False,
                        "resolves_to": "",
                    })

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
