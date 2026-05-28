"""Async client for communicating with ArchiveTeam Warrior instances."""

import asyncio
import json
import logging
import random
import string
import time
from datetime import datetime, timezone

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
    "checkrequirements": ItemState.PROCESSING,
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
    "checkintegrity": ItemState.PROCESSING,
    "check": ItemState.PROCESSING,
    "setbadurls": ItemState.PROCESSING,
    "integrity": ItemState.PROCESSING,
    "checkip": ItemState.PROCESSING,
}

STALE_ITEM_TIMEOUT = 30
REFRESH_INTERVAL = 30


def classify_stage(text):
    lower = text.lower().strip()
    for keyword, state in STAGE_MAP.items():
        if keyword in lower:
            return state
    if lower == "" or "idle" in lower:
        return ItemState.WAITING
    return ItemState.UNKNOWN


class WarriorClient:
    def __init__(self, instance_config, reconnect_base=5, reconnect_max=60):
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
        self._poll_interval = 5
        self._poll_task = None
        self._http_client = None
        self._sockjs_base = None
        self._sockjs_connected = False
        self._items = {}
        self._item_updated = {}
        self._completed_count = 0
        self._last_refresh = 0.0

    def _build_url(self):
        return "http://" + self.config.host + ":" + str(self.config.port)

    def _get_auth(self):
        if self.config.http_username and self.config.http_password:
            return (self.config.http_username, self.config.http_password)
        return None

    @property
    def status(self):
        return self._status

    async def start(self, poll_interval=5):
        self._running = True
        self._poll_interval = poll_interval
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

    # ------------------------------------------------------------------
    # Main poll loop
    # ------------------------------------------------------------------
    async def _poll_loop(self, interval):
        while self._running:
            try:
                if not self._sockjs_connected:
                    self._sockjs_connected = await self._sockjs_open()
                    if self._sockjs_connected:
                        self._last_refresh = time.monotonic()
                        await self._fetch_settings_and_project()

                if self._sockjs_connected:
                    if time.monotonic() - self._last_refresh > REFRESH_INTERVAL:
                        logger.debug("[%s] Forcing reconnect for fresh item states", self.config.name)
                        self._sockjs_connected = False
                        continue

                    success = await self._sockjs_poll()
                    if success:
                        self._reconnect_attempts = 0
                        self._status.connection_state = ConnectionState.ONLINE
                        self._status.error_message = ""
                        self._status.last_seen = datetime.now(timezone.utc).isoformat()
                        self._check_stale_items()
                        continue
                    else:
                        self._sockjs_connected = False
                        logger.debug("[%s] SockJS dropped, retrying", self.config.name)

                await self._handle_disconnect()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[%s] Unexpected error: %s", self.config.name, e)
                self._status.error_message = str(e)
                self._sockjs_connected = False
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
            "[%s] Offline - reconnect attempt %d in %ds",
            self.config.name, self._reconnect_attempts, delay,
        )
        await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # Fetch settings and project
    # ------------------------------------------------------------------
    async def _fetch_settings_and_project(self):
        await self._fetch_downloader()
        await self._fetch_selected_project()

    async def _fetch_downloader(self):
        try:
            resp = await self._http_client.get(
                self._build_url() + "/api/settings",
                auth=self._get_auth(), timeout=10.0,
            )
            if resp.status_code != 200:
                return
            soup = BeautifulSoup(resp.text, "lxml")
            dl_input = soup.find("input", {"name": "downloader"})
            if dl_input and dl_input.get("value"):
                self._status.downloader = dl_input["value"].strip()
                logger.info("[%s] Downloader: %s", self.config.name, self._status.downloader)
        except Exception as e:
            logger.debug("[%s] Could not fetch downloader: %s", self.config.name, e)

    async def _fetch_selected_project(self):
        """Fetch the selected project from /api/all-projects.

        The selected project is under the <h3>Your current project</h3>
        section. The slug is in <input name="project_name" value="...">.
        The display name is in <h4>.
        """
        try:
            resp = await self._http_client.get(
                self._build_url() + "/api/all-projects",
                auth=self._get_auth(), timeout=10.0,
            )
            if resp.status_code != 200:
                logger.debug("[%s] /api/all-projects returned %d", self.config.name, resp.status_code)
                return
            soup = BeautifulSoup(resp.text, "lxml")
            current_h3 = None
            for h3 in soup.find_all("h3"):
                if "current project" in h3.get_text(strip=True).lower():
                    current_h3 = h3
                    break
            if not current_h3:
                logger.debug("[%s] No 'Your current project' heading found", self.config.name)
                return
            current_ul = current_h3.find_next_sibling("ul")
            if not current_ul:
                logger.debug("[%s] No <ul> after current project heading", self.config.name)
                return
            current_li = current_ul.find("li")
            if not current_li:
                logger.debug("[%s] No <li> in current project list", self.config.name)
                return
            slug_input = current_li.find("input", {"name": "project_name"})
            if slug_input and slug_input.get("value"):
                self._status.project_slug = slug_input["value"].strip()
                logger.info("[%s] Project slug: %s", self.config.name, self._status.project_slug)
            h4 = current_li.find("h4")
            if h4:
                name = h4.get_text(strip=True)
                if name:
                    self._status.current_project = name
                    logger.info("[%s] Project name: %s", self.config.name, name)
        except Exception as e:
            logger.debug("[%s] Could not fetch selected project: %s", self.config.name, e)

    # ------------------------------------------------------------------
    # SockJS xhr-polling
    # ------------------------------------------------------------------
    async def _sockjs_open(self):
        try:
            server_id = str(random.randint(0, 999)).zfill(3)
            session_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
            self._sockjs_base = self._build_url() + "/" + server_id + "/" + session_id
            resp = await self._http_client.post(
                self._sockjs_base + "/xhr",
                auth=self._get_auth(), timeout=10.0,
            )
            if resp.status_code == 200 and resp.text.strip().startswith("o"):
                logger.info("[%s] SockJS session opened", self.config.name)
                return True
            logger.debug(
                "[%s] SockJS open failed: HTTP %d body=%s",
                self.config.name, resp.status_code, resp.text[:100],
            )
            return False
        except Exception as e:
            logger.debug("[%s] SockJS connect error: %s", self.config.name, e)
            return False

    async def _sockjs_poll(self):
        try:
            resp = await self._http_client.post(
                self._sockjs_base + "/xhr",
                auth=self._get_auth(), timeout=35.0,
            )
            if resp.status_code != 200:
                return False
            frame = resp.text.strip()
            if frame.startswith("a"):
                messages = self._parse_sockjs_frame(frame)
                for msg in messages:
                    self._dispatch_event(msg)
                return True
            elif frame == "h":
                return True
            elif frame.startswith("c"):
                logger.info("[%s] SockJS closed by server", self.config.name)
                return False
            else:
                return True
        except httpx.TimeoutException:
            return True
        except Exception as e:
            logger.warning("[%s] SockJS poll error: %s", self.config.name, e)
            return False

    def _parse_sockjs_frame(self, frame):
        try:
            arr = json.loads(frame[1:])
            results = []
            for item in arr:
                if isinstance(item, str):
                    try:
                        results.append(json.loads(item))
                    except json.JSONDecodeError:
                        pass
                elif isinstance(item, dict):
                    results.append(item)
            return results
        except json.JSONDecodeError:
            return []

    # ------------------------------------------------------------------
    # Event dispatch
    # ------------------------------------------------------------------
    def _dispatch_event(self, raw):
        if not isinstance(raw, dict):
            return
        event = raw.get("event_name", "")
        msg = raw.get("message")
        logger.debug("[%s] event: %s", self.config.name, event)
        if event == "project.refresh":
            self._on_project_refresh(msg)
        elif event == "bandwidth":
            self._on_bandwidth(msg)
        elif event == "project.item.new":
            self._on_item_new(msg)
        elif event == "project.item.task":
            self._on_item_task(msg)
        elif event == "project.item.output":
            pass
        elif event == "project.item.completed":
            self._on_item_completed(msg, ItemState.DONE)
        elif event == "project.item.failed":
            self._on_item_completed(msg, ItemState.ERROR)
        elif event == "warrior.status":
            self._on_warrior_status(msg)
        elif event == "runner.status":
            pass
        elif event == "instance_id":
            pass
        elif event == "warrior.projects_loaded":
            pass
        elif event == "warrior.project_selected":
            self._on_project_selected(msg)
        elif event == "warrior.settings_update":
            pass

    def _on_project_selected(self, msg):
        asyncio.create_task(self._fetch_selected_project())

    def _on_project_refresh(self, msg):
        if not isinstance(msg, dict):
            return
        if not self._status.current_project:
            project = msg.get("project", {})
            if isinstance(project, dict):
                html = project.get("project_html", "")
                name = self._extract_project_name(html)
                if name:
                    self._status.current_project = name
        items_raw = msg.get("items", [])
        self._items.clear()
        self._item_updated.clear()
        for item in items_raw:
            if isinstance(item, dict):
                self._ingest_item(item)
        self._sync_items_to_status()

    @staticmethod
    def _extract_project_name(project_html):
        if not project_html:
            return ""
        soup = BeautifulSoup(project_html, "lxml")
        h2 = soup.find("h2")
        if h2:
            parts = []
            for child in h2.children:
                if isinstance(child, str):
                    t = child.strip().strip("\u00b7").strip()
                    if t:
                        parts.append(t)
            if parts:
                return parts[0]
            text = h2.get_text(strip=True)
            for suffix in ["\u00b7Leaderboard", "Leaderboard", "\u00b7 Leaderboard"]:
                text = text.replace(suffix, "").strip().rstrip("\u00b7").strip()
            if text:
                return text
        text = soup.get_text(" ", strip=True)
        for suffix in ["\u00b7Leaderboard", "Leaderboard"]:
            text = text.replace(suffix, "").strip()
        if text:
            return text.split("\n")[0].strip()[:80]
        return ""

    def _on_bandwidth(self, msg):
        if not isinstance(msg, dict):
            return
        self._status.bytes_uploaded = int(msg.get("sent", 0))
        self._status.bytes_downloaded = int(msg.get("received", 0))
        self._status.bandwidth_up = float(msg.get("sending", 0))
        self._status.bandwidth_down = float(msg.get("receiving", 0))

    def _on_warrior_status(self, msg):
        if not isinstance(msg, dict):
            return

    def _on_item_new(self, msg):
        if not isinstance(msg, dict):
            return
        self._ingest_item(msg)
        self._sync_items_to_status()

    def _on_item_task(self, msg):
        if not isinstance(msg, dict):
            return
        item_id = str(msg.get("id", ""))
        task = msg.get("task", {})
        if item_id in self._items and isinstance(task, dict):
            task_name = task.get("name", "")
            task_status = task.get("status", "")
            if task_status == "running":
                self._items[item_id].state = classify_stage(task_name)
                self._items[item_id].task_description = task_name
            self._item_updated[item_id] = time.monotonic()
            self._sync_items_to_status()

    def _on_item_completed(self, msg, final_state):
        if not isinstance(msg, dict):
            return
        item_id = str(msg.get("id", ""))
        if item_id in self._items:
            del self._items[item_id]
            self._item_updated.pop(item_id, None)
            if final_state == ItemState.DONE:
                self._completed_count += 1
                self._status.completed_items = self._completed_count
            self._sync_items_to_status()

    def _ingest_item(self, item):
        item_id = str(item.get("id", ""))
        if not item_id:
            return
        name = item.get("name", "Item " + item_id)
        tasks = item.get("tasks", [])
        current_task_name = ""
        current_state = ItemState.WAITING
        for task in tasks:
            if isinstance(task, dict):
                if task.get("status") == "running":
                    current_task_name = task.get("name", "")
                    current_state = classify_stage(current_task_name)
                    break
        status = item.get("status", "")
        if status == "completed":
            current_state = ItemState.DONE
        elif status == "failed":
            current_state = ItemState.ERROR
        self._items[item_id] = ItemStatus(
            item_id=item_id,
            item_name=str(name),
            state=current_state,
            task_description=current_task_name,
        )
        self._item_updated[item_id] = time.monotonic()

    def _sync_items_to_status(self):
        terminal_states = (ItemState.DONE, ItemState.ERROR)
        active = [i for i in self._items.values() if i.state not in terminal_states]
        done = [i for i in self._items.values() if i.state in terminal_states]
        self._status.items = active + done[-2:]
        if len(self._items) > 50:
            completed_ids = [k for k, v in self._items.items() if v.state in terminal_states]
            for cid in completed_ids[:-5]:
                del self._items[cid]
                self._item_updated.pop(cid, None)

    def _check_stale_items(self):
        now = time.monotonic()
        idle_states = (ItemState.WAITING, ItemState.GETTING_TASK, ItemState.UNKNOWN)
        terminal_states = (ItemState.DONE, ItemState.ERROR)
        changed = False
        for item_id, item in self._items.items():
            if item.state in idle_states or item.state in terminal_states:
                continue
            last_update = self._item_updated.get(item_id, now)
            if now - last_update > STALE_ITEM_TIMEOUT:
                logger.debug(
                    "[%s] Stale check: demoting %s from %s to WAITING",
                    self._status.name, item_id, item.state,
                )
                item.state = ItemState.WAITING
                item.task_description = ""
                changed = True
        if changed:
            self._sync_items_to_status()

    # ------------------------------------------------------------------
    # Push settings / change project / pause / resume
    # ------------------------------------------------------------------
    async def update_settings(self, settings):
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
        return await self._post_settings(config_data)

    async def change_project(self, project_name):
        success = await self._post_settings({"selected_project": project_name})
        if success:
            await self._fetch_selected_project()
            self._sockjs_connected = False
        return success

    async def deselect_project(self):
        slug = self._status.project_slug
        if not slug:
            logger.warning("[%s] No project slug to deselect", self.config.name)
            return False
        try:
            resp = await self._http_client.post(
                self._build_url() + "/api/deselect-project",
                data={"project_name": slug},
                auth=self._get_auth(), timeout=10.0,
            )
            if resp.status_code in (200, 302):
                logger.info("[%s] Project deselected: %s", self.config.name, slug)
                return True
            logger.warning("[%s] Deselect got HTTP %d", self.config.name, resp.status_code)
            return False
        except Exception as e:
            logger.error("[%s] Deselect error: %s", self.config.name, e)
            return False

    async def select_project(self, project_name):
        try:
            resp = await self._http_client.post(
                self._build_url() + "/api/select-project",
                data={"project_name": project_name},
                auth=self._get_auth(), timeout=10.0,
            )
            if resp.status_code in (200, 302):
                logger.info("[%s] Project selected: %s", self.config.name, project_name)
                return True
            logger.warning("[%s] Select project got HTTP %d", self.config.name, resp.status_code)
            return False
        except Exception as e:
            logger.error("[%s] Select project error: %s", self.config.name, e)
            return False

    async def _post_settings(self, data):
        try:
            resp = await self._http_client.post(
                self._build_url() + "/api/settings",
                data=data,
                auth=self._get_auth(), timeout=10.0,
            )
            if resp.status_code in (200, 302):
                logger.info("[%s] Settings pushed via /api/settings", self.config.name)
                return True
            logger.warning("[%s] Settings push got HTTP %d", self.config.name, resp.status_code)
            return False
        except Exception as e:
            logger.error("[%s] Settings push error: %s", self.config.name, e)
            return False
