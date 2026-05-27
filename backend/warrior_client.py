"""Async client for communicating with ArchiveTeam Warrior instances."""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from backend.models import (
    ConnectionState,
    ItemState,
    ItemStatus,
    WarriorInstanceConfig,
    WarriorSettings,
    WarriorStatus,
)

logger = logging.getLogger(__name__)

STAGE_MAP = {
    "getitemfromtracker": ItemState.GETTING_TASK,
    "get_item": ItemState.GETTING_TASK,
    "preparedirectories": ItemState.PROCESSING,
    "prepare": ItemState.PROCESSING,
    "wgetdownload": ItemState.DOWNLOADING,
    "wget": ItemState.DOWNLOADING,
    "download": ItemState.DOWNLOADING,
    "preparestatsfortracker": ItemState.PROCESSING,
    "stats": ItemState.PROCESSING,
    "uploadwithtracker": ItemState.UPLOADING,
    "upload": ItemState.UPLOADING,
    "rsync": ItemState.UPLOADING,
    "senddonetotracker": ItemState.UPLOADING,
    "done": ItemState.DONE,
    "movefiles": ItemState.PROCESSING,
    "deduplicate": ItemState.PROCESSING,
    "waiting": ItemState.WAITING,
}


def classify_stage(text: str) -> ItemState:
    lower = text.lower().strip()
    for keyword, state in STAGE_MAP.items():
        if keyword in lower:
            return state
    if lower == "" or "idle" in lower:
        return ItemState.WAITING
    return ItemState.UNKNOWN


class WarriorClient:
    def __init__(
        self,
        instance_config: WarriorInstanceConfig,
        reconnect_base: int = 5,
        reconnect_max: int = 60,
    ):
        self.config = instance_config
        self.reconnect_base = reconnect_base
        self.reconnect_max = reconnect_max
        self._reconnect_attempts = 0
        self._status = WarriorStatus(
            name=instance_config.name,
            host=instance_config.host,
            port=instance_config.port,
            url=self._build_url(),
            connection_state=ConnectionState.OFFLINE,
        )
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._http_client: Optional[httpx.AsyncClient] = None

    def _build_url(self) -> str:
        return "http://" + self.config.host + ":" + str(self.config.port)

    def _get_auth(self) -> Optional[tuple[str, str]]:
        if self.config.http_username and self.config.http_password:
            return (self.config.http_username, self.config.http_password)
        return None

    @property
    def status(self) -> WarriorStatus:
        return self._status

    async def start(self, poll_interval: int = 5):
        self._running = True
        self._http_client = httpx.AsyncClient(timeout=10.0)
        self._poll_task = asyncio.create_task(self._poll_loop(poll_interval))
        logger.info("[%s] Started polling %s", self.config.name, self._build_url())

    async def stop(self):
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._http_client:
            await self._http_client.aclose()
        logger.info("[%s] Stopped polling", self.config.name)

    async def _poll_loop(self, interval: int):
        while self._running:
            try:
                success = await self._fetch_status()
                if success:
                    self._reconnect_attempts = 0
                    self._status.connection_state = ConnectionState.ONLINE
                    self._status.error_message = ""
                    self._status.last_seen = datetime.now(timezone.utc).isoformat()
                    await asyncio.sleep(interval)
                else:
                    await self._handle_disconnect()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[%s] Unexpected error: %s", self.config.name, e)
                self._status.error_message = str(e)
                await self._handle_disconnect()

    async def _handle_disconnect(self):
        self._status.connection_state = ConnectionState.OFFLINE
        self._reconnect_attempts += 1
        self._status.reconnect_attempts = self._reconnect_attempts
        delay = min(
            self.reconnect_base * (2 ** (self._reconnect_attempts - 1)),
            self.reconnect_max,
        )
        logger.warning(
            "[%s] Offline -- reconnect attempt %d in %ds",
            self.config.name, self._reconnect_attempts, delay,
        )
        await asyncio.sleep(delay)

    async def _fetch_status(self) -> bool:
        try:
            auth = self._get_auth()
            response = await self._http_client.get(
                self._build_url() + "/",
                auth=auth,
            )
            if response.status_code == 401:
                self._status.connection_state = ConnectionState.AUTH_FAILED
                self._status.error_message = "Authentication failed (HTTP 401)"
                return False
            if response.status_code != 200:
                self._status.error_message = "HTTP " + str(response.status_code)
                return False
            self._parse_html_status(response.text)
            return True
        except httpx.ConnectError:
            self._status.error_message = "Connection refused"
            return False
        except httpx.TimeoutException:
            self._status.error_message = "Connection timed out"
            return False
        except Exception as e:
            self._status.error_message = "Fetch error: " + type(e).__name__ + ": " + str(e)
            return False

    def _parse_html_status(self, html: str):
        soup = BeautifulSoup(html, "lxml")

        project_el = (
            soup.find("span", class_="project-name")
            or soup.find("h2")
            or soup.find("title")
        )
        if project_el:
            text = project_el.get_text(strip=True)
            text = re.sub(r"^(ArchiveTeam Warrior\s*[-\u2013\u2014]\s*)", "", text)
            self._status.current_project = text or "Unknown"
        else:
            self._status.current_project = self._extract_text_fallback(html, "project")

        downloader_el = soup.find(
            "input", {"name": "downloader"}
        ) or soup.find("input", {"id": "downloader"})
        if downloader_el and downloader_el.get("value"):
            self._status.downloader = downloader_el["value"]
        else:
            match = re.search(r'"?downloader"?\s*[:=]\s*"([^"]*)"', html)
            if match:
                self._status.downloader = match.group(1)

        concurrent_el = soup.find(
            "input", {"name": "concurrent_items"}
        ) or soup.find("select", {"name": "concurrent_items"})
        if concurrent_el:
            val = concurrent_el.get("value", "")
            if val.isdigit():
                self._status.concurrent_items = int(val)
            else:
                selected = concurrent_el.find("option", selected=True)
                if selected and selected.get_text(strip=True).isdigit():
                    self._status.concurrent_items = int(selected.get_text(strip=True))

        items = []
        item_elements = soup.find_all("div", class_=re.compile(r"item|pipeline"))
        if item_elements:
            for idx, item_el in enumerate(item_elements):
                item_text = item_el.get_text(" ", strip=True)
                item_name = ""
                name_el = item_el.find(class_=re.compile(r"item.?name|item.?id"))
                if name_el:
                    item_name = name_el.get_text(strip=True)
                else:
                    name_match = re.search(r"([a-zA-Z0-9_-]+:\d+)", item_text)
                    if name_match:
                        item_name = name_match.group(1)
                stage_el = item_el.find(class_=re.compile(r"task|stage|active|current"))
                stage_text = stage_el.get_text(strip=True) if stage_el else item_text
                state = classify_stage(stage_text)
                items.append(ItemStatus(
                    item_id="item-" + str(idx),
                    item_name=item_name or ("Item " + str(idx + 1)),
                    state=state,
                    task_description=stage_text[:200],
                ))

        if not items:
            json_match = re.search(
                r"var\s+(?:items|pipeline_data|status)\s*=\s*(\[.*?\]);",
                html, re.DOTALL,
            )
            if json_match:
                try:
                    data = json.loads(json_match.group(1))
                    for idx, entry in enumerate(data):
                        if isinstance(entry, dict):
                            name = entry.get("name", entry.get("id", "Item " + str(idx + 1)))
                            stage = entry.get("stage", entry.get("task", entry.get("status", "")))
                            items.append(ItemStatus(
                                item_id="item-" + str(idx),
                                item_name=str(name),
                                state=classify_stage(str(stage)),
                                task_description=str(stage)[:200],
                            ))
                except (json.JSONDecodeError, TypeError):
                    pass

        if not items:
            log_entries = re.findall(r"(Item\s+\d+|item\d+)[:\s]+(.*?)(?:\n|<br|$)", html)
            for idx, (name, desc) in enumerate(log_entries[:6]):
                items.append(ItemStatus(
                    item_id="item-" + str(idx),
                    item_name=name.strip(),
                    state=classify_stage(desc),
                    task_description=desc.strip()[:200],
                ))

        self._status.items = items

    def _extract_text_fallback(self, html: str, keyword: str) -> str:
        match = re.search(
            keyword + r"""[\"\':\s]+([^<\"\']+)""", html, re.IGNORECASE
        )
        return match.group(1).strip() if match else "Unknown"

    async def update_settings(self, settings: WarriorSettings) -> bool:
        try:
            config_data = {}
            if settings.downloader is not None:
                config_data["downloader"] = settings.downloader
            if settings.concurrent_items is not None:
                config_data["concurrent_items"] = str(settings.concurrent_items)
            if settings.http_username is not None:
                config_data["http_username"] = settings.http_username
            if settings.http_password is not None:
                config_data["http_password"] = settings.http_password
            if settings.shared_rsync_threads is not None:
                config_data["shared:rsync_threads"] = str(settings.shared_rsync_threads)

            if not config_data:
                return True

            auth = self._get_auth()

            try:
                response = await self._http_client.post(
                    self._build_url() + "/api/settings",
                    data=config_data, auth=auth,
                )
                if response.status_code in (200, 302):
                    logger.info("[%s] Settings updated via API", self.config.name)
                    return True
            except Exception:
                pass

            try:
                response = await self._http_client.post(
                    self._build_url() + "/",
                    data=config_data, auth=auth,
                )
                if response.status_code in (200, 302):
                    logger.info("[%s] Settings updated via form POST", self.config.name)
                    return True
            except Exception:
                pass

            try:
                response = await self._http_client.post(
                    self._build_url() + "/api/config",
                    json=config_data, auth=auth,
                )
                if response.status_code in (200, 302):
                    logger.info("[%s] Settings updated via /api/config", self.config.name)
                    return True
            except Exception:
                pass

            logger.warning("[%s] Could not update settings via any method", self.config.name)
            return False
        except Exception as e:
            logger.error("[%s] Error updating settings: %s", self.config.name, e)
            return False
