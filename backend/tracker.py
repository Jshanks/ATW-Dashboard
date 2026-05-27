"""Fetch user stats from the ArchiveTeam tracker."""

import asyncio
import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

STATS_URL = "https://v1.api.tracker.archiveteam.org/{project}/stats.json"
CACHE_TTL = 60

_cache = {}
_cache_times = {}
_lock = asyncio.Lock()


async def get_project_data(project_slug):
    """Fetch stats.json for a project, with caching."""
    now = time.monotonic()

    if project_slug in _cache and (now - _cache_times.get(project_slug, 0)) < CACHE_TTL:
        return _cache[project_slug]

    async with _lock:
        if project_slug in _cache and (time.monotonic() - _cache_times.get(project_slug, 0)) < CACHE_TTL:
            return _cache[project_slug]

        url = STATS_URL.format(project=project_slug)
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
                _cache[project_slug] = data
                _cache_times[project_slug] = time.monotonic()
                logger.debug("Refreshed tracker stats for %s", project_slug)
                return data
        except Exception as exc:
            logger.warning("Failed to fetch tracker stats for %s: %s", project_slug, exc)
            return _cache.get(project_slug)


def build_user_stats(stats, username, project_slug):
    """Build a stats dict from stats.json."""
    result = {
        "project": project_slug,
        "downloader": username,
        "user_items_done": 0,
        "user_bytes": 0,
        "items_done": 0,
        "items_out": 0,
        "items_todo": 0,
        "total_items": 0,
        "total_data_bytes": 0,
    }

    if not stats:
        return result

    counts = stats.get("counts", {})
    result["items_done"] = counts.get("done", stats.get("total_items_done", 0))
    result["items_out"] = counts.get("out", stats.get("total_items_out", 0))
    result["items_todo"] = counts.get("todo", stats.get("total_items_todo", 0))
    result["total_items"] = stats.get("total_items", 0)

    domain_bytes = stats.get("domain_bytes", {})
    if isinstance(domain_bytes, dict):
        result["total_data_bytes"] = int(sum(domain_bytes.values()))

    dl_count = stats.get("downloader_count", {})
    if isinstance(dl_count, dict):
        for key, val in dl_count.items():
            if key.lower() == username.lower():
                result["user_items_done"] = int(val)
                break

    dl_bytes = stats.get("downloader_bytes", {})
    if isinstance(dl_bytes, dict):
        for key, val in dl_bytes.items():
            if key.lower() == username.lower():
                result["user_bytes"] = int(val)
                break

    return result