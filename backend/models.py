"""Pydantic models for ATW Dashboard."""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


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
    # Project the warrior is actually running right now (resolved slug).
    project_slug: str = ""
    # Project the warrior is set to. This is "auto" on ArchiveTeam's Choice.
    selected_project: str = ""
    downloader: str = ""
    concurrent_items: int = 0
    items: list[ItemStatus] = Field(default_factory=list)
    last_seen: Optional[str] = None
    error_message: str = ""
    bandwidth_down: float = 0.0
    bandwidth_up: float = 0.0
    bytes_downloaded: int = 0
    bytes_uploaded: int = 0
    completed_items: int = 0
    restarting: bool = False


class BulkSettingsRequest(BaseModel):
    instance_names: list[str] = Field(..., description="List of instance names to update")
    settings: WarriorSettings


class BulkProjectRequest(BaseModel):
    instance_names: list[str] = Field(..., description="List of instance names to update")
    project_name: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Project slug to select ('auto' = ArchiveTeam's Choice)",
    )


class RestartRequest(BaseModel):
    instance_names: list[str] = Field(
        ...,
        description="Warriors to restart immediately (/api/stop_now)",
    )


class PauseRequest(BaseModel):
    instance_names: list[str] = Field(..., description="List of instance names to pause")
    duration_hours: Optional[float] = Field(
        None,
        gt=0,
        le=24 * 365,
        description="Hours until auto-resume. None = indefinite",
    )


class ResumeRequest(BaseModel):
    instance_names: list[str] = Field(..., description="List of instance names to resume")


class AddInstanceRequest(BaseModel):
    # Optional: when blank, the backend names the instance "host:port".
    name: Optional[str] = Field(None, max_length=100)
    host: str = Field(..., min_length=1, max_length=253)
    port: int = Field(8001, ge=1, le=65535)
    http_username: Optional[str] = None
    http_password: Optional[str] = None


class BulkAddRequest(BaseModel):
    """Bulk add / import request.

    ``text`` may contain line syntax, CSV with a host column, or a dashboard
    JSON export. A dry run returns the preview without changing the fleet.
    """

    text: str = Field(..., max_length=500_000)
    dry_run: bool = True


class EditInstanceRequest(BaseModel):
    host: Optional[str] = Field(None, min_length=1, max_length=253)
    port: Optional[int] = Field(None, ge=1, le=65535)
    http_username: Optional[str] = None
    http_password: Optional[str] = None


class DashboardState(BaseModel):
    instances: list[WarriorStatus] = Field(default_factory=list)
    total_online: int = 0
    total_offline: int = 0
    total_items_active: int = 0


class LiveItemStatus(BaseModel):
    """Minimal item state required by the live dashboard grid."""

    item_id: str = ""
    state: ItemState = ItemState.UNKNOWN


class LiveWarriorStatus(BaseModel):
    """Compact, explicit schema for routine browser state broadcasts.

    Full item names and task descriptions remain available from the existing
    per-instance HTTP endpoint and are intentionally excluded here.
    """

    name: str
    host: str
    port: int
    url: str = ""
    connection_state: ConnectionState = ConnectionState.OFFLINE
    reconnect_attempts: int = 0
    current_project: str = ""
    project_slug: str = ""
    selected_project: str = ""
    downloader: str = ""
    concurrent_items: int = 0
    items: list[LiveItemStatus] = Field(default_factory=list)
    last_seen: Optional[str] = None
    error_message: str = ""
    bandwidth_down: float = 0.0
    bandwidth_up: float = 0.0
    bytes_downloaded: int = 0
    bytes_uploaded: int = 0
    completed_items: int = 0
    restarting: bool = False

    @classmethod
    def from_warrior_status(cls, status: WarriorStatus) -> LiveWarriorStatus:
        return cls(
            name=status.name,
            host=status.host,
            port=status.port,
            url=status.url,
            connection_state=status.connection_state,
            reconnect_attempts=status.reconnect_attempts,
            current_project=status.current_project,
            project_slug=status.project_slug,
            selected_project=status.selected_project,
            downloader=status.downloader,
            concurrent_items=status.concurrent_items,
            items=[
                LiveItemStatus(item_id=item.item_id, state=item.state)
                for item in status.items
            ],
            last_seen=status.last_seen,
            error_message=status.error_message,
            bandwidth_down=status.bandwidth_down,
            bandwidth_up=status.bandwidth_up,
            bytes_downloaded=status.bytes_downloaded,
            bytes_uploaded=status.bytes_uploaded,
            completed_items=status.completed_items,
            restarting=status.restarting,
        )


class LiveDashboardState(BaseModel):
    """Validated WebSocket state contract for the live dashboard."""

    instances: list[LiveWarriorStatus] = Field(default_factory=list)
    total_online: int = 0
    total_offline: int = 0
    total_items_active: int = 0

    @classmethod
    def from_dashboard_state(cls, state: DashboardState) -> LiveDashboardState:
        return cls(
            instances=[
                LiveWarriorStatus.from_warrior_status(instance)
                for instance in state.instances
            ],
            total_online=state.total_online,
            total_offline=state.total_offline,
            total_items_active=state.total_items_active,
        )


# ---------------------------------------------------------------------------
# Project Health automation and baseline management (v4.6.0)
# ---------------------------------------------------------------------------


def _clean_name_list(values: list[str]) -> list[str]:
    """Trim, remove blanks, and preserve order while removing duplicates."""
    cleaned_values = []
    seen_values = set()
    for raw_value in values:
        value = str(raw_value).strip()
        if not value or value in seen_values:
            continue
        cleaned_values.append(value)
        seen_values.add(value)
    return cleaned_values


class ProjectHealthPolicyRequest(BaseModel):
    """Persistent monitor-only Project Health policy."""

    primary_project: str = Field(..., min_length=1, max_length=200)
    fallback_project: str = Field(..., min_length=1, max_length=200)
    canary_percent: int = Field(
        5,
        ge=1,
        le=100,
        description=(
            "Percentage used to calculate the canary group. The backend applies "
            "a minimum of 3 and maximum of 10 canaries. Five percent is recommended."
        ),
    )
    minimum_baseline_hours: int = Field(
        6,
        ge=1,
        le=48,
        description=(
            "Minimum covered history required before health ratios and manual "
            "failover become available. Defaults to 6 hours."
        ),
    )
    full_baseline_hours: int = Field(
        24,
        ge=1,
        le=48,
        description=(
            "Covered history required for a full-confidence baseline. Defaults "
            "to 24 hours and cannot be shorter than the minimum baseline."
        ),
    )
    monitoring_enabled: bool = Field(
        True,
        description="Master Project Health collection and evaluation switch",
    )
    automatic_failover_enabled: bool = Field(
        False,
        description="Allow automatic failover after all prerequisites are satisfied",
    )
    stalled_evaluations_required: int = Field(
        3,
        ge=2,
        le=10,
        description="Consecutive stalled evaluations required before automatic failover",
    )
    cooldown_hours: int = Field(
        6,
        ge=1,
        le=48,
        description="Hours after automatic failover before another automatic action",
    )
    resume_stabilization_minutes: int = Field(
        30,
        ge=15,
        le=120,
        description="Delay after re-enabling monitoring before automation may trigger",
    )

    @field_validator("primary_project", "fallback_project")
    @classmethod
    def normalize_project_slug(cls, value: str) -> str:
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("Project slug cannot be blank")
        return normalized_value

    @model_validator(mode="after")
    def validate_policy(self):
        if self.primary_project == self.fallback_project:
            raise ValueError("Primary and fallback projects must be different")
        if self.full_baseline_hours < self.minimum_baseline_hours:
            raise ValueError("Full baseline cannot be shorter than the minimum baseline")
        return self


class ProjectHealthPreviewRequest(BaseModel):
    """Request a fresh canary recommendation without saving assignments."""

    canary_percent: Optional[int] = Field(
        None,
        ge=1,
        le=100,
        description="Temporary preview percentage; omitted uses the saved policy",
    )


class ProjectHealthRoleAssignmentRequest(BaseModel):
    """Persist the reviewed Primary canary and Fallback fleet groups."""

    primary_canaries: list[str] = Field(default_factory=list, max_length=10)
    fallback_fleet: list[str] = Field(default_factory=list)

    @field_validator("primary_canaries", "fallback_fleet")
    @classmethod
    def normalize_instance_names(cls, values: list[str]) -> list[str]:
        return _clean_name_list(values)

    @model_validator(mode="after")
    def validate_assignments(self):
        overlap = set(self.primary_canaries).intersection(self.fallback_fleet)
        if overlap:
            raise ValueError(
                "Warriors cannot be assigned to both Primary canaries and Fallback fleet"
            )
        if self.fallback_fleet and not self.primary_canaries:
            raise ValueError("At least one Primary canary is required when a fallback fleet is assigned")
        return self


class ProjectHealthBaselineResetRequest(BaseModel):
    """Delete retained Project Health samples for the configured primary project."""

    confirmation: str = Field(..., min_length=1, max_length=220)
    actor: str = Field("dashboard", min_length=1, max_length=200)

    @field_validator("confirmation")
    @classmethod
    def normalize_reset_confirmation(cls, value: str) -> str:
        normalized_value = value.strip()
        if not normalized_value.startswith("RESET "):
            raise ValueError("Confirmation must start with RESET followed by the primary project slug")
        return normalized_value

    @field_validator("actor")
    @classmethod
    def normalize_reset_actor(cls, value: str) -> str:
        normalized_value = value.strip()
        return normalized_value or "dashboard"


class ManualFailoverDecisionAction(str, Enum):
    PREVIEWED = "previewed"
    REQUESTED = "requested"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class ManualFailoverDecisionRequest(BaseModel):
    """Record an operator decision without performing automatic switching."""

    action: ManualFailoverDecisionAction
    actor: str = Field("dashboard", min_length=1, max_length=200)
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("actor")
    @classmethod
    def normalize_decision_actor(cls, value: str) -> str:
        normalized_value = value.strip()
        return normalized_value or "dashboard"


class ManualFailoverRequest(BaseModel):
    """Explicit operator request for a manual failover operation.

    The endpoint using this model must still enforce baseline readiness and use
    the persisted role assignments. This model does not enable background or
    automatic switching.
    """

    confirmation: str = Field(
        ...,
        description="Must be exactly 'FAILOVER' to authorize the manual operation",
    )
    actor: str = Field("dashboard", min_length=1, max_length=200)

    @field_validator("confirmation")
    @classmethod
    def require_explicit_confirmation(cls, value: str) -> str:
        if value.strip() != "FAILOVER":
            raise ValueError("Type FAILOVER to confirm the manual failover")
        return "FAILOVER"

    @field_validator("actor")
    @classmethod
    def normalize_failover_actor(cls, value: str) -> str:
        normalized_value = value.strip()
        return normalized_value or "dashboard"
