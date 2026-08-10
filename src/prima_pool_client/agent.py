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
        self._relay_task: asyncio.Task | None = None
        self._cluster_config: ClusterConfig | None = None

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
                # wait_for raises TimeoutError when the heartbeat interval
                # elapses — that's the normal pacing signal, not an error.
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.config.heartbeat_interval_s)
                except asyncio.TimeoutError:
                    pass
        finally:
            if ws_task:
                self._ws.stop()
                ws_task.cancel()

    def _ws_url(self, worker_id: str) -> str:
        base = self.config.pool_url.replace("http://", "ws://").replace("https://", "wss://")
        return f"{base}/v1/workers/{worker_id}/events"

    async def _ensure_registered(self) -> None:
        # If we have a worker_id, verify it still exists server-side; if the
        # server lost it (e.g. it was revoked while we were down), re-register.
        if self.state.worker_id:
            try:
                self.client.get_worker_state(self.state.worker_id)
                return
            except Exception:  # noqa: BLE001
                logger.warning("worker %s no longer valid; re-registering", self.state.worker_id)
                self.state.worker_id = None
                self.state.cluster_id = None
                self.state.assigned_ip = None
                self.state.ring_position = None
                self.state.status = "registered"
        # Generate WG keypair if not present
        if not self.state.wg_private_key:
            priv, pub = generate_keypair()
            self.state.wg_private_key = priv
            self.state.wg_public_key = pub
        else:
            self.state.wg_public_key = derive_public_key(self.state.wg_private_key)

        payload = {
            "model": self.config.model,
            "gguf_sha256": self._compute_gguf_hash(),
            "memory_allocated_mb": self.config.memory_allocated_mb,
            "wg_pubkey": self.state.wg_public_key,
            "endpoint": {
                "host": self.config.wg_endpoint_host or self._detect_host(),
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
        # Determine ring position: prefer the persisted value, else derive it
        # from the config (the peer whose allowed IP matches our assigned IP).
        ring_position = self.state.ring_position
        if ring_position is None:
            ring_position = self._find_ring_position(config)
            self.state.ring_position = ring_position
        # Render + bring up WireGuard
        conf = self._render_wg_conf(config)
        self.wg.up(conf)
        # Remember the config so the relay monitor can route unreachable peers.
        self._cluster_config = config
        # Start the direct→relay fallback monitor (if a relay is configured).
        if config.relay.enabled and config.relay.pubkey and config.relay.endpoint:
            if self._relay_task is None or self._relay_task.done():
                self._relay_task = asyncio.create_task(self._relay_monitor(config))
        # Launch prima.cpp. A launch failure (e.g. missing binary, model file)
        # must NOT kill the agent or leave it stuck half-assigned: bring the
        # tunnel back down and return to the waitlist so the scheduler can
        # reassign us later.
        try:
            self._prima_proc = self.prima.launch(config, ring_position)
        except Exception as exc:  # noqa: BLE001
            logger.error("prima.cpp launch failed: %s", exc)
            self.wg.down()
            self._cluster_config = None
            self.state.cluster_id = None
            self.state.assigned_ip = None
            self.state.ring_position = None
            self.state.status = "waitlisted"
            self._save_state()
            return
        # Report readiness
        try:
            status = self.client.report_ready(cluster_id)
            logger.info("readiness reported: %s (%d/%d)", status.status, status.members_ready, status.members_total)
        except Exception as exc:  # noqa: BLE001
            logger.error("readiness report failed: %s", exc)

    def _find_ring_position(self, config: ClusterConfig) -> int:
        """Find our index in the ring by matching our assigned IP to a peer.

        Only ring members are considered (the server peer is excluded).
        """
        from .prima import _ring_members

        my_ip = self.state.assigned_ip
        for i, peer in enumerate(_ring_members(config)):
            for allowed in peer.allowed_ips:
                if allowed.split("/")[0] == my_ip:
                    return i
        # Fallback: we are not in the peer list (shouldn't happen); default to 0.
        logger.warning("could not find own IP %s in cluster peers; defaulting to head", my_ip)
        return 0

    def _render_wg_conf(self, config: ClusterConfig) -> str:
        from .wireguard import render_wg_conf

        return render_wg_conf(config, self.state.wg_private_key, self.config.wg_listen_port)

    async def _relay_monitor(self, config: ClusterConfig) -> None:
        """Direct-first, relay-fallback.

        Polls `wg show ... latest-handshakes`; for ring peers that have no
        recent direct handshake (or are marked `preferred: relay`), ensure the
        relay routes their IP. Peers whose direct path recovers are removed
        from the relay route (WG will re-route direct).
        """
        relay = config.relay
        relayed_ips: set[str] = set()
        check_interval = max(5.0, float(getattr(self.config, "wg_relay_check_s", 10)))
        direct_stale_s = 120.0
        while not self._stop.is_set():
            try:
                if not self.wg.is_up():
                    break
                now = time.time()
                handshakes = self.wg.latest_handshakes()
                stale_epoch = int(now - direct_stale_s)

                wanted: set[str] = set()
                for peer in config.peers:
                    if peer.role == "server":
                        continue
                    if not peer.allowed_ips:
                        continue
                    peer_ip = peer.allowed_ips[0].split("/")[0]
                    direct_ok = handshakes.get(peer.pubkey, 0) > stale_epoch
                    prefer_relay = peer.preferred.value == "relay"
                    if prefer_relay or not direct_ok:
                        wanted.add(peer_ip)

                if wanted != relayed_ips:
                    if wanted:
                        self.wg.add_peer(
                            relay.pubkey,
                            relay.endpoint,
                            sorted(wanted),
                            keepalive=25,
                        )
                        logger.info("routing %d peer(s) via relay: %s", len(wanted), sorted(wanted))
                    else:
                        # All direct — remove the relay route entirely.
                        self.wg.remove_peer(relay.pubkey)
                        logger.info("all peers direct; removed relay route")
                    relayed_ips = wanted
            except Exception as exc:  # noqa: BLE001
                logger.warning("relay monitor error: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=check_interval)
            except asyncio.TimeoutError:
                pass

    async def _handle_dissolved(self, frame: dict) -> None:
        logger.info("cluster %s dissolved (%s)", frame.get("cluster_id"), frame.get("reason"))
        if self._relay_task:
            self._relay_task.cancel()
            self._relay_task = None
        self._cluster_config = None
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
    def _compute_gguf_hash(self) -> str:
        """Compute the SHA-256 of the local GGUF file for registration."""
        from .prima import compute_gguf_sha256

        model_path = self.prima._resolve_model_path()
        if not Path(model_path).exists():
            raise RuntimeError(
                f"model file not found at {model_path}; cannot compute GGUF hash"
            )
        logger.info("computing SHA-256 of %s", model_path)
        return compute_gguf_sha256(model_path)

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
        if self._relay_task:
            self._relay_task.cancel()
            self._relay_task = None
        # Tear down the tunnel and prima.cpp, but do NOT revoke the worker —
        # revocation is a permanent delete. On restart the agent reconnects with
        # the same worker_id (liveness is transient; the worker re-adds on the
        # next heartbeat).
        self.wg.down()
        if self._prima_proc:
            self.prima.stop()
            self._prima_proc = None
        self.client.close()
