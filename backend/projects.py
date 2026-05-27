"""Fetch and cache the active project list from WarriorHQ."""

import asyncio
import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

PROJECTS_URL = "https://warriorhq.archiveteam.org/projects.json"
CACHE_TTL = 1800  # 30 minutes

_cache: list[dict] = []
_cache_time: float = 0
_lock = asyncio.Lock()


async def get_projects(force: bool = False) -> list[dict]:
    """Return cached project list, refreshing if stale."""
    global _cache, _cache_time

    if not force and _cache and (time.monotonic() - _cache_time) < CACHE_TTL:
        return _cache

    async with _lock:
        # Double-check after acquiring lock
        if not force and _cache and (time.monotonic() - _cache_time) < CACHE_TTL:
            return _cache

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(PROJECTS_URL)
                resp.raise_for_status()
                data = resp.json()

                projects = []
                if isinstance(data, list):
                    projects = data
                elif isinstance(data, dict):
                    # Some versions wrap in {"projects": [...]}
                    projects = data.get("projects", list(data.values()))

                cleaned = []
                for p in projects:
                    if isinstance(p, dict) and p.get("name"):
                        cleaned.append({
                            "name": p.get("name", ""),
                            "title": p.get("title", p.get("name", "")),
                            "description": p.get("description", ""),
                            "logo": p.get("logo", ""),
                        })

                _cache = cleaned
                _cache_time = time.monotonic()
                logger.info("Refreshed project list: %d projects", len(cleaned))
                return _cache

        except Exception as exc:
            logger.warning("Failed to fetch projects from WarriorHQ: %s", exc)
            return _cache  # Return stale cache on error