"""prima.cpp launcher.

Generates the equivalent of prima-docker's .env / docker-compose command from
a cluster assignment, then launches prima.cpp.

Modes:
  - "same-container" (default): exec llama-server/llama-cli directly in THIS
    container. The client and prima.cpp share the same network namespace, so
    the WireGuard interface the agent brings up is directly visible to
    prima.cpp — this is the correct deployment for the single-container model.
  - "docker": launch prima.cpp via `docker compose` against a mounted
    prima-docker project (legacy; requires host networking to share the WG
    namespace).

prima-docker is used as a behavioral blueprint only — this module reproduces
its env-var semantics dynamically.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from .config import ClientConfig
from .models import ClusterConfig

logger = logging.getLogger(__name__)


def build_env(
    config: ClientConfig,
    cluster: ClusterConfig,
    ring_position: int,
    world: int,
    master_ip: str,
    next_ip: str,
) -> dict[str, str]:
    """Build the environment variables for a prima.cpp node.

    Mirrors prima-docker's .env semantics:
      RANK, WORLD, MASTER_IP, NEXT_IP, MODEL_FILE, MEM_LIMIT,
      GPU_MEM_FLAG, CTX_SIZE, API_PORT, BATCH_FLAGS, EXTRA_FLAGS, MODE
    """
    is_head = ring_position == 0
    env = {
        "RANK": str(ring_position),
        "WORLD": str(world),
        "MASTER_IP": master_ip,
        "NEXT_IP": next_ip,
        "MODEL_FILE": config.model_file,
        "MEM_LIMIT": config.mem_limit,
        "GPU_MEM_FLAG": config.gpu_mem_flag,
        "CTX_SIZE": str(config.ctx_size),
        "API_PORT": str(config.api_port),
        "BATCH_FLAGS": config.batch_flags,
        "EXTRA_FLAGS": config.extra_flags,
        "MODE": "llama-server" if is_head else "llama-cli",
        "COMPOSE_PROFILES": ("server-cpu" if is_head else "cpu")
        if not config.gpu_mem_flag
        else ("server" if is_head else "gpu"),
    }
    return env


def _ring_members(cluster: ClusterConfig) -> list:
    """Return the ring members (excludes the control-plane server peer)."""
    return [p for p in cluster.peers if p.role != "server"]


def _ring_neighbors(cluster: ClusterConfig, ring_position: int) -> tuple[str, str]:
    """Return (master_ip, next_ip) as WG private IPs from the ring order.

    Only ring members participate in the prima.cpp ring; the server peer
    (role="server") is excluded.
    """
    members = _ring_members(cluster)
    n = len(members)
    # master = members[0]'s allowed IP (the ring head)
    master_ip = members[0].allowed_ips[0].split("/")[0]
    next_idx = (ring_position + 1) % n
    next_ip = members[next_idx].allowed_ips[0].split("/")[0]
    return master_ip, next_ip


class PrimaLauncher:
    def __init__(self, config: ClientConfig) -> None:
        self.config = config
        self._proc: subprocess.Popen | None = None

    def _resolve_model_path(self) -> str:
        """Resolve the absolute GGUF path for the current mode."""
        if self.config.model_path:
            return self.config.model_path
        return str(Path(self.config.prima_dir).expanduser() / "models" / self.config.model_file)

    def launch(self, cluster: ClusterConfig, ring_position: int) -> subprocess.Popen | None:
        world = len(_ring_members(cluster))
        master_ip, next_ip = _ring_neighbors(cluster, ring_position)
        env = build_env(self.config, cluster, ring_position, world, master_ip, next_ip)

        if self.config.prima_mode == "docker":
            return self._launch_docker(env)
        return self._launch_same_container(env, ring_position)

    def _launch_docker(self, env: dict[str, str]) -> subprocess.Popen | None:
        compose = shutil.which("docker")
        if not compose:
            raise RuntimeError("docker not found; cannot launch prima.cpp in docker mode")
        project_dir = Path(self.config.prima_dir).expanduser()
        if not (project_dir / "docker-compose.yml").exists():
            raise RuntimeError(f"docker-compose.yml not found in {project_dir}")
        cmd = ["docker", "compose", "up", "-d"]
        full_env = {**os.environ, **env}
        logger.info("launching prima.cpp via docker compose in %s", project_dir)
        return subprocess.Popen(cmd, cwd=str(project_dir), env=full_env)

    def _launch_same_container(self, env: dict[str, str], ring_position: int) -> subprocess.Popen | None:
        """Exec llama-server/llama-cli directly in this container.

        Because the client and prima.cpp share the same network namespace, the
        WG interface the agent brought up is directly reachable by prima.cpp.
        """
        binary = shutil.which("llama-server" if ring_position == 0 else "llama-cli")
        if not binary:
            raise RuntimeError(
                "llama-server/llama-cli not found. In same-container mode prima.cpp "
                "must be installed in this image."
            )
        model_path = self._resolve_model_path()
        if not Path(model_path).exists():
            raise RuntimeError(f"model file not found: {model_path}")

        cmd = [
            binary,
            "-m", model_path,
            "-c", str(self.config.ctx_size),
            "--world", env["WORLD"],
            "--rank", env["RANK"],
            "--master", env["MASTER_IP"],
            "--next", env["NEXT_IP"],
            "--prefetch",
        ]
        if ring_position == 0:
            cmd += ["--host", "0.0.0.0", "--port", str(self.config.api_port)]
        if self.config.gpu_mem_flag:
            cmd += [self.config.gpu_mem_flag]
        if self.config.batch_flags:
            cmd += self.config.batch_flags.split()
        if self.config.extra_flags:
            cmd += self.config.extra_flags.split()
        logger.info("launching prima.cpp in-container: %s", " ".join(cmd))
        self._proc = subprocess.Popen(cmd)
        return self._proc

    def stop(self) -> None:
        """Best-effort teardown of the prima.cpp process/container."""
        if self.config.prima_mode == "docker":
            project_dir = Path(self.config.prima_dir).expanduser()
            subprocess.run(
                ["docker", "compose", "down"],
                cwd=str(project_dir),
                capture_output=True,
                text=True,
            )
        else:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            subprocess.run(["pkill", "-f", "llama-server"], capture_output=True, text=True)
            subprocess.run(["pkill", "-f", "llama-cli"], capture_output=True, text=True)
