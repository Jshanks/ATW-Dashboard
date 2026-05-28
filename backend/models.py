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
    project_slug: str = ""
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


class BulkSettingsRequest(BaseModel):
    instance_names: list[str] = Field(..., description="List of instance names to update")
    settings: WarriorSettings


class BulkProjectRequest(BaseModel):
    instance_names: list[str] = Field(..., description="List of instance names to update")
    project_name: str = Field(..., description="Project slug to select")


class PauseRequest(BaseModel):
    instance_names: list[str] = Field(..., description="List of instance names to pause")
    duration_hours: Optional[float] = Field(None, description="Hours until auto-resume. None = indefinite")


class ResumeRequest(BaseModel):
    instance_names: list[str] = Field(..., description="List of instance names to resume")


class AddInstanceRequest(BaseModel):
    name: str
    host: str
    port: int = 8001
    http_username: Optional[str] = None
    http_password: Optional[str] = None


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
