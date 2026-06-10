"""
Schémas Pydantic utilisés par l'orchestrateur.

Ces modèles servent à la fois pour :
- la validation des requêtes/réponses HTTP
- la (dé)sérialisation depuis/vers SQLite (via dict / JSON)
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    """Retourne l'heure UTC actuelle (timezone-aware)."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Enumérations
# ---------------------------------------------------------------------------

class DeviceType(str, Enum):
    mobile_agent = "mobile_agent"
    desktop_agent = "desktop_agent"
    laptop_agent = "laptop_agent"


class DeviceStatus(str, Enum):
    online = "online"
    offline = "offline"


class CommandStatus(str, Enum):
    pending = "pending"
    awaiting_confirmation = "awaiting_confirmation"
    executing = "executing"
    done = "done"
    failed = "failed"
    expired = "expired"


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

class CapabilityFlag(BaseModel):
    """Décrit l'état d'une capability : activée ou non, et si elle nécessite
    une confirmation utilisateur avant exécution."""

    enabled: bool = False
    auth_required: bool = False


# Capacités par défaut proposées à l'enregistrement d'un device.
DEFAULT_CAPABILITIES: dict[str, dict[str, bool]] = {
    "send_sms": {"enabled": True, "auth_required": False},
    "make_call": {"enabled": True, "auth_required": True},
    "get_location": {"enabled": False, "auth_required": False},
    "screenshot": {"enabled": True, "auth_required": False},
    "open_file": {"enabled": True, "auth_required": False},
    "run_terminal": {"enabled": False, "auth_required": True},
    "launch_app": {"enabled": True, "auth_required": False},
    "get_battery": {"enabled": True, "auth_required": False},
    "set_volume": {"enabled": True, "auth_required": False},
    "send_notification": {"enabled": True, "auth_required": False},
}


# ---------------------------------------------------------------------------
# Réseau
# ---------------------------------------------------------------------------

class NetworkInfo(BaseModel):
    ip: Optional[str] = None
    transport: Optional[str] = None
    nat: Optional[bool] = None
    carrier: Optional[str] = None


# ---------------------------------------------------------------------------
# Device
# ---------------------------------------------------------------------------

class Device(BaseModel):
    device_hash: str
    device_name: str
    device_type: DeviceType
    status: DeviceStatus = DeviceStatus.offline
    last_seen: datetime = Field(default_factory=utcnow)
    previous_names: list[str] = Field(default_factory=list)
    capabilities: dict[str, CapabilityFlag] = Field(default_factory=dict)
    network: NetworkInfo = Field(default_factory=NetworkInfo)
    version: str = "0.0.0"
    capabilities_version: str = ""
    session_token: Optional[str] = None


# ---------------------------------------------------------------------------
# Commande
# ---------------------------------------------------------------------------

class Command(BaseModel):
    command_id: str
    device_hash: str
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    status: CommandStatus = CommandStatus.pending
    result: Optional[dict[str, Any]] = None
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    confirmed_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Schémas de requêtes / réponses HTTP
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    device_name: str
    device_hash: str
    device_type: DeviceType
    capabilities: dict[str, CapabilityFlag] = Field(default_factory=dict)
    version: str = "0.0.0"


class RegisterResponse(BaseModel):
    status: str = "registered"
    session_token: str
    heartbeat_interval: int
    polling_interval: int
    server_time: datetime


class HeartbeatRequest(BaseModel):
    device_hash: str
    capabilities_version: str
    network: NetworkInfo = Field(default_factory=NetworkInfo)


class HeartbeatResponse(BaseModel):
    status: str = "ok"
    capabilities_refresh_needed: bool = False
    server_time: datetime


class CommandOut(BaseModel):
    """Représentation d'une commande envoyée à un device via /commands."""

    command_id: str
    action: str
    params: dict[str, Any]
    confirm_required: bool
    expires_at: datetime


class ResultRequest(BaseModel):
    command_id: str
    status: CommandStatus
    data: Optional[dict[str, Any]] = None
    error: Optional[str] = None


class ConfirmRequest(BaseModel):
    command_id: str
    approved: bool


class RenameRequest(BaseModel):
    device_hash: str
    new_name: str


class CapabilitiesUpdateRequest(BaseModel):
    device_hash: str
    capabilities: dict[str, CapabilityFlag]


class CapabilityPatch(BaseModel):
    enabled: bool
    auth_required: bool = False
