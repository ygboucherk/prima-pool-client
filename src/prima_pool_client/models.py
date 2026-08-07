"""Client-side Pydantic models mirroring the control plane response schemas."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class WorkerStatus(str, Enum):
    registered = "registered"
    waitlisted = "waitlisted"
    assigned = "assigned"


class ClusterStatus(str, Enum):
    assembling = "assembling"
    live = "live"


class Preferred(str, Enum):
    direct = "direct"
    relay = "relay"


class Worker(BaseModel):
    worker_id: str
    account_id: str
    status: WorkerStatus
    model: str
    waitlist_position: int | None = None
    online: bool = True


class ClusterAssignment(BaseModel):
    cluster_id: str
    assigned_ip: str
    config_url: str


class WorkerState(BaseModel):
    worker_id: str
    account_id: str
    status: WorkerStatus
    online: bool
    model: str
    cluster: ClusterAssignment | None = None


class InterfaceConfig(BaseModel):
    private_ip: str
    subnet: str
    mtu: int = 1280


class RelayConfig(BaseModel):
    pubkey: str = ""
    endpoint: str = ""
    enabled: bool = False


class PeerConfig(BaseModel):
    pubkey: str
    endpoint: str | None = None
    allowed_ips: list[str]
    persistent_keepalive: int = 25
    preferred: Preferred = Preferred.direct


class ClusterConfig(BaseModel):
    cluster_id: str
    interface: InterfaceConfig
    relay: RelayConfig = RelayConfig()
    peers: list[PeerConfig]


class ClusterStatusResponse(BaseModel):
    cluster_id: str
    status: ClusterStatus
    members_ready: int
    members_total: int
