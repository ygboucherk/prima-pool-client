"""Client configuration.

Loaded from a TOML file (default: ~/.config/prima-pool/client.toml) with
environment-variable overrides (PRIMA_POOL_*).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


@dataclass
class ClientConfig:
    # Pool server
    pool_url: str = "http://127.0.0.1:8000"
    # Worker-scoped API key (sk-worker-...). If empty, the agent cannot run.
    api_key: str = ""
    # Model to serve
    model: str = "demo-model"
    # Self-declared memory to allocate (MB)
    memory_allocated_mb: int = 4096
    # WireGuard
    wg_private_key: str = ""
    wg_listen_port: int = 51820
    wg_interface: str = "prima-pool"
    wg_conf_dir: str = "/etc/wireguard"
    # prima.cpp launch
    prima_mode: str = "docker"  # "docker" (compose) or "native" (exec binaries)
    prima_dir: str = "~/prima"  # for docker mode: compose project dir
    model_file: str = "model.gguf"
    mem_limit: str = "8g"
    gpu_mem_flag: str = ""
    ctx_size: int = 4096
    api_port: int = 8080
    batch_flags: str = ""
    extra_flags: str = ""
    # Heartbeat / WS
    heartbeat_interval_s: float = 10.0
    ws_reconnect_backoff_s: list[int] = field(default_factory=lambda: [1, 30])
    # State persistence
    state_path: str = "~/.local/state/prima-pool/client-state.json"

    @classmethod
    def load(cls, path: str | None = None) -> "ClientConfig":
        data: dict[str, Any] = {}
        if path:
            p = Path(path).expanduser()
            if p.exists() and tomllib:
                with open(p, "rb") as fh:
                    data = tomllib.load(fh)
        cfg = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        # Env overrides
        cfg.pool_url = _env_str("PRIMA_POOL_URL", cfg.pool_url)
        cfg.api_key = _env_str("PRIMA_POOL_API_KEY", cfg.api_key)
        cfg.model = _env_str("PRIMA_POOL_MODEL", cfg.model)
        cfg.memory_allocated_mb = _env_int("PRIMA_POOL_MEMORY_MB", cfg.memory_allocated_mb)
        cfg.wg_private_key = _env_str("PRIMA_POOL_WG_PRIVATE_KEY", cfg.wg_private_key)
        cfg.prima_mode = _env_str("PRIMA_POOL_PRIMA_MODE", cfg.prima_mode)
        cfg.model_file = _env_str("PRIMA_POOL_MODEL_FILE", cfg.model_file)
        cfg.mem_limit = _env_str("PRIMA_POOL_MEM_LIMIT", cfg.mem_limit)
        return cfg

    @property
    def wg_conf_path(self) -> Path:
        return Path(self.wg_conf_dir).expanduser() / f"{self.wg_interface}.conf"

    @property
    def state_file(self) -> Path:
        return Path(self.state_path).expanduser()
