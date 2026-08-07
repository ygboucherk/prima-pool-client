"""Worker agent control loop.

Implements the worker lifecycle:
  register → waitlist → assigned (WG up + prima.cpp up + ready) → dissolve/leave

State is persisted so a restart can recover via GET /workers/{id}/state.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import ClientConfig
from .models import ClusterConfig, WorkerState, WorkerStatus
from .prima import PrimaLauncher
from .rest import PoolClient
from .wireguard import WireGuardManager, derive_public_key, generate_keypair
from .ws_client import WsClient

logger = logging.getLogger(__name__)


@dataclass
class AgentState:
    worker_id: str | None = None
    account_id: str | None = None
    status: str = "registered"
    online: bool = False
    model: str = ""
    cluster_id: str | None = None
    assigned_ip: str | None = None
    ring_position: int | None = None
    config_url: str | None = None
    wg_private_key: str = ""
    wg_public_key: str = ""
    updated_at: float = field(default_factory=time.time)


class Agent:
    def __init__(self, config: ClientConfig, client: PoolClient | None = None) -> None:
        self.config = config
        self.client = client or PoolClient(config.pool_url, config.api_key)
        self.wg = WireGuardManager(config)
        self.prima = PrimaLauncher(config)
        self.state = self._load_state()
        self._stop = asyncio.Event()
        self._ws: WsClient | None = None
        self._prima_proc = None

    # ── persistence ──────────────────────────────────────────────────────
    def _load_state(self) -> AgentState:
        path = self.config.state_file
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return AgentState(**data)
            except (ValueError, TypeError):
                pass
        return AgentState()

    def _save_state(self) -> None:
        path = self.config.state_file
        path.parent.mkdir(parents=True, exist_ok=True)
        self.state.updated_at = time.time()
        path.write_text(json.dumps(self.state.__dict__, indent=2))

    # ── lifecycle ────────────────────────────────────────────────────────
    async def run(self) -> None:
        logger.info("agent starting (model=%s)", self.config.model)
        await self._ensure_registered()
        # Start WS listener
        if self.state.worker_id:
            ws_url = self._ws_url(self.state.worker_id)
            self._ws = WsClient(ws_url, self.config.api_key, self._on_frame, self.config.ws_reconnect_backoff_s)
            ws_task = asyncio.create_task(self._ws.run())
        else:
            ws_task = None

        # Recover any existing assignment
        await self._recover_state()

        # Heartbeat loop
        try:
            while not self._stop.is_set():
                await self._heartbeat_once()
                await asyncio.wait_for(self._stop.wait(), timeout=self.config.heartbeat_interval_s)
        finally:
            if ws_task:
                self._ws.stop()
                ws_task.cancel()

    def _ws_url(self, worker_id: str) -> str:
        base = self.config.pool_url.replace("http://", "ws://").replace("https://", "wss://")
        return f"{base}/v1/workers/{worker_id}/events"

    async def _ensure_registered(self) -> None:
        if self.state.worker_id:
            return
        # Generate WG keypair if not present
        if not self.state.wg_private_key:
            priv, pub = generate_keypair()
            self.state.wg_private_key = priv
            self.state.wg_public_key = pub
        else:
            self.state.wg_public_key = derive_public_key(self.state.wg_private_key)

        payload = {
            "model": self.config.model,
            "memory_allocated_mb": self.config.memory_allocated_mb,
            "wg_pubkey": self.state.wg_public_key,
            "endpoint": {
                "host": self._detect_host(),
                "port": self.config.wg_listen_port,
                "behind_nat": False,
                "nat_type": "unknown",
            },
            "hardware": self._detect_hardware(),
        }
        worker = self.client.register_worker(payload)
        self.state.worker_id = worker.worker_id
        self.state.account_id = worker.account_id
        self.state.status = worker.status.value
        self.state.online = worker.online
        self.state.model = worker.model
        self._save_state()
        logger.info("registered worker %s (status=%s)", worker.worker_id, worker.status)

    async def _recover_state(self) -> None:
        if not self.state.worker_id:
            return
        try:
            st: WorkerState = self.client.get_worker_state(self.state.worker_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("state recovery failed: %s", exc)
            return
        self.state.status = st.status.value
        self.state.online = st.online
        if st.cluster:
            self.state.cluster_id = st.cluster.cluster_id
            self.state.assigned_ip = st.cluster.assigned_ip
            self.state.config_url = st.cluster.config_url
            # If we were assigned but haven't brought up WG, do it now.
            if not self.wg.is_up():
                await self._bring_up_cluster(st.cluster.cluster_id)
        else:
            self.state.cluster_id = None
            self.state.assigned_ip = None
        self._save_state()

    async def _heartbeat_once(self) -> None:
        if not self.state.worker_id:
            return
        try:
            worker = self.client.heartbeat(self.state.worker_id)
            self.state.online = worker.online
            self.state.status = worker.status.value
            self._save_state()
        except Exception as exc:  # noqa: BLE001
            logger.warning("heartbeat failed: %s", exc)

    # ── WS frame handling ────────────────────────────────────────────────
    async def _on_frame(self, frame: dict) -> None:
        ftype = frame.get("type")
        logger.info("WS frame: %s", ftype)
        if ftype == "hello":
            cadence = frame.get("cadence", {})
            if "heartbeat_s" in cadence:
                self.config.heartbeat_interval_s = float(cadence["heartbeat_s"])
        elif ftype == "cluster_assigned":
            await self._handle_assigned(frame)
        elif ftype == "cluster_dissolved":
            await self._handle_dissolved(frame)
        elif ftype == "pong":
            pass

    async def _handle_assigned(self, frame: dict) -> None:
        cluster_id = frame["cluster_id"]
        self.state.cluster_id = cluster_id
        self.state.assigned_ip = frame.get("assigned_ip")
        self.state.ring_position = frame.get("ring_position")
        self.state.config_url = frame.get("config_url")
        self.state.status = "assigned"
        self._save_state()
        await self._bring_up_cluster(cluster_id)

    async def _bring_up_cluster(self, cluster_id: str) -> None:
        try:
            config: ClusterConfig = self.client.get_cluster_config(cluster_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("failed to fetch cluster config: %s", exc)
            return
        # Render + bring up WireGuard
        conf = self._render_wg_conf(config)
        self.wg.up(conf)
        # Launch prima.cpp
        ring_position = self.state.ring_position or 0
        self._prima_proc = self.prima.launch(config, ring_position)
        # Report readiness
        try:
            status = self.client.report_ready(cluster_id)
            logger.info("readiness reported: %s (%d/%d)", status.status, status.members_ready, status.members_total)
        except Exception as exc:  # noqa: BLE001
            logger.error("readiness report failed: %s", exc)

    def _render_wg_conf(self, config: ClusterConfig) -> str:
        from .wireguard import render_wg_conf

        return render_wg_conf(config, self.state.wg_private_key, self.config.wg_listen_port)

    async def _handle_dissolved(self, frame: dict) -> None:
        logger.info("cluster %s dissolved (%s)", frame.get("cluster_id"), frame.get("reason"))
        self.wg.down()
        if self._prima_proc:
            self.prima.stop()
            self._prima_proc = None
        self.state.cluster_id = None
        self.state.assigned_ip = None
        self.state.ring_position = None
        self.state.status = "waitlisted"
        self._save_state()

    # ── helpers ──────────────────────────────────────────────────────────
    def _detect_host(self) -> str:
        import socket

        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:  # noqa: BLE001
            return "127.0.0.1"

    def _detect_hardware(self) -> dict:
        import platform

        return {
            "cpu": platform.processor() or None,
            "gpu": None,
            "ram_gb": None,
            "os": platform.system().lower(),
            "prima_version": None,
        }

    async def stop(self) -> None:
        self._stop.set()
        if self._ws:
            self._ws.stop()
        # Best-effort leave
        if self.state.worker_id:
            try:
                self.client.revoke_worker(self.state.worker_id)
            except Exception:  # noqa: BLE001
                pass
        self.wg.down()
        self.client.close()
