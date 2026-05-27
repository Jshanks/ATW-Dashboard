"""Fetch user stats from the ArchiveTeam tracker."""

import asyncio
import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

TRACKER_API_TEMPLATE = "https://v1.api.tracker.archiveteam.org/{project}/stats.json"
CACHE_TTL = 60  # refresh every 60 seconds

_cache: dict[str, dict] = {}  # project -> response
_cache_times: dict[str, float] = {}
_lock = asyncio.Lock()


async def get_tracker_stats(project: str) -> Optional[dict]:
    """Fetch stats.json for a project, with caching."""
    now = time.monotonic()
    if project in _cache and (now - _cache_times.get(project, 0)) < CACHE_TTL:
        return _cache[project]

    async with _lock:
        # Double-check after lock
        if project in _cache and (time.monotonic() - _cache_times.get(project, 0)) < CACHE_TTL:
            return _cache[project]

        url = TRACKER_API_TEMPLATE.format(project=project)
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
                _cache[project] = data
                _cache_times[project] = time.monotonic()
                logger.debug("Refreshed tracker stats for %s", project)
                return data
        except Exception as exc:
            logger.warning("Failed to fetch tracker stats for %s: %s", project, exc)
            return _cache.get(project)


def find_downloader_stats(stats: dict, username: str) -> Optional[dict]:
    """Search the stats response for a specific downloader's entry."""
    if not stats or not username:
        return None

    # Try the "downloaders" list (most common format)
    downloaders = stats.get("downloaders", [])
    if isinstance(downloaders, list):
        for entry in downloaders:
            if isinstance(entry, dict):
                name = entry.get("downloader", entry.get("name", ""))
                if name.lower() == username.lower():
                    return entry

    # Try "downloader_bytes" / "downloader_count" dicts (alternative format)
    items_dict = stats.get("downloader_count", {})
    bytes_dict = stats.get("downloader_bytes", {})
    if isinstance(items_dict, dict) and username in items_dict:
        return {
            "downloader": username,
            "items_done": items_dict.get(username, 0),
            "bytes": bytes_dict.get(username, 0),
        }
    # Case-insensitive fallback
    if isinstance(items_dict, dict):
        for key in items_dict:
            if key.lower() == username.lower():
                return {
                    "downloader": key,
                    "items_done": items_dict.get(key, 0),
                    "bytes": bytes_dict.get(key, 0),
                }

    return None