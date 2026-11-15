"""Pydantic models for the ATW Dashboard."""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ItemState(str, Enum):
    UNKNOWN = "unknown"
    WAITING = "waiting"
    GETTING_TASK = "getting_task"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    UPLOADING = "uploading"
    DONE = "done"
    ERROR = "error"


class ConnectionState(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    CONNECTING = "connecting"
    AUTH_FAILED = "auth_failed"


class ItemStatus(BaseModel):
    item_id: str = ""
    item_name: str = ""
    state: ItemState = ItemState.UNKNOWN
    task_description: str = ""


class WarriorSettings(BaseModel):
    downloader: Optional[str] = Field(None, description="Nickname / downloader name")
    concurrent_items: Optional[int] = Field(None, ge=1, le=6)
    http_username: Optional[str] = None
    http_password: Optional[str] = None
    shared_rsync_threads: Optional[int] = Field(None, ge=1, le=40)


class WarriorInstanceConfig(BaseModel):
    name: str
    host: str
    port: int = 8001
    http_username: Optional[str] = None
    http_password: Optional[str] = None


class WarriorStatus(BaseModel):
    name: str
    host: str
    port: int
    url: str = ""
    connection_state: ConnectionState = ConnectionState.OFFLINE
    reconnect_attempts: int = 0
    current_project: str = ""
    # Project the warrior is actually running right now (resolved slug, e.g. "telegram").
    # Used for tracker stats, history deltas, and the card badge.
    project_slug: str = ""
    # Project the warrior is *set to* (what the user chose). Usually equals
    # project_slug, but is "auto" when the warrior is on ArchiveTeam's Choice.
    # Used for pause/resume and for grouping cards by project.
    selected_project: str = ""
    downloader: str = ""
    concurrent_items: int = 0
    items: list[ItemStatus] = []
    last_seen: Optional[str] = None
    error_message: str = ""
    bandwidth_down: float = 0.0
    bandwidth_up: float = 0.0
    bytes_downloaded: int = 0
    bytes_uploaded: int = 0
    completed_items: int = 0
    # v4.4.0: True from a "Restart now" (/api/stop_now) until the warrior has
    # gone offline and come back, or the restart window times out
    restarting: bool = False


class BulkSettingsRequest(BaseModel):
    instance_names: list[str] = Field(..., description="List of instance names to update")
    settings: WarriorSettings


class BulkProjectRequest(BaseModel):
    instance_names: list[str] = Field(..., description="List of instance names to update")
    project_name: str = Field(..., description="Project slug to select ('auto' = ArchiveTeam's Choice)")


class RestartRequest(BaseModel):
    instance_names: list[str] = Field(..., description="Warriors to restart immediately (/api/stop_now)")


class PauseRequest(BaseModel):
    instance_names: list[str] = Field(..., description="List of instance names to pause")
    duration_hours: Optional[float] = Field(None, description="Hours until auto-resume. None = indefinite")


class ResumeRequest(BaseModel):
    instance_names: list[str] = Field(..., description="List of instance names to resume")


class AddInstanceRequest(BaseModel):
    # Optional: when blank, the backend names the instance "host:port"
    # (unique by definition, since duplicate host:port pairs are rejected)
    name: Optional[str] = Field(None, max_length=100)
    host: str = Field(..., min_length=1, max_length=253)
    port: int = Field(8001, ge=1, le=65535)
    http_username: Optional[str] = None
    http_password: Optional[str] = None


class BulkAddRequest(BaseModel):
    """Bulk add / import. `text` may be any of:
      - line syntax, one entry per line (or comma/space separated):
            192.168.1.50                 -> port 8001
            192.168.1.50:8002
            192.168.1.50:8001-8099       -> port range on one host
            192.168.1.50-60              -> last-octet IP range, port 8001
            192.168.1.50-60:8001         -> IP range with explicit port
      - CSV with a header row containing host (name, port optional)
      - JSON exported from /api/instances/export
    dry_run=True returns the preview (to_add / skipped) without changing anything.
    """
    text: str = Field(..., max_length=500_000)
    dry_run: bool = True


class EditInstanceRequest(BaseModel):
    host: Optional[str] = None
    port: Optional[int] = None
    http_username: Optional[str] = None
    http_password: Optional[str] = None


class DashboardState(BaseModel):
    instances: list[WarriorStatus] = []
    total_online: int = 0
    total_offline: int = 0
    total_items_active: int = 0
