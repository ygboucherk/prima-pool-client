"""WireGuard key generation, config rendering, and interface bring-up.

The private key never leaves the device. v0 uses the `wg` / `wg-quick` CLI
tools; a pure-Python fallback (wireguard-go) is noted but not implemented.
"""
from __future__ import annotations

import base64
import logging
import shutil
import subprocess
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519

from .config import ClientConfig
from .models import ClusterConfig

logger = logging.getLogger(__name__)


def generate_keypair() -> tuple[str, str]:
    """Generate a (private_key, public_key) WireGuard keypair (base64)."""
    private = x25519.X25519PrivateKey.generate()
    public = private.public_key()
    priv_b64 = base64.b64encode(
        private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
    ).decode()
    pub_b64 = base64.b64encode(
        public.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode()
    return priv_b64, pub_b64


def derive_public_key(private_key_b64: str) -> str:
    """Derive the public key from a base64 private key."""
    raw = base64.b64decode(private_key_b64)
    private = x25519.X25519PrivateKey.from_private_bytes(raw)
    pub = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(pub).decode()


def render_wg_conf(config: ClusterConfig, private_key: str, listen_port: int) -> str:
    """Render a wg-quick config file from a cluster config."""
    lines: list[str] = []
    lines.append("[Interface]")
    lines.append(f"PrivateKey = {private_key}")
    lines.append(f"Address = {config.interface.private_ip}/24")
    lines.append(f"MTU = {config.interface.mtu}")
    lines.append(f"ListenPort = {listen_port}")
    lines.append("")

    for peer in config.peers:
        lines.append("[Peer]")
        lines.append(f"PublicKey = {peer.pubkey}")
        if peer.endpoint:
            lines.append(f"Endpoint = {peer.endpoint}")
        lines.append(f"AllowedIPs = {', '.join(peer.allowed_ips)}")
        lines.append(f"PersistentKeepalive = {peer.persistent_keepalive}")
        lines.append("")

    if config.relay.enabled and config.relay.pubkey:
        lines.append("[Peer]")
        lines.append(f"PublicKey = {config.relay.pubkey}")
        if config.relay.endpoint:
            lines.append(f"Endpoint = {config.relay.endpoint}")
        lines.append(f"AllowedIPs = {', '.join(p.allowed_ips for p in config.peers)}")
        lines.append(f"PersistentKeepalive = {config.relay.persistent_keepalive if hasattr(config.relay, 'persistent_keepalive') else 25}")
        lines.append("")

    return "\n".join(lines)


class WireGuardManager:
    """Brings up / tears down a WireGuard interface via wg-quick."""

    def __init__(self, config: ClientConfig) -> None:
        self.config = config
        self._wg_quick = shutil.which("wg-quick")
        self._wg = shutil.which("wg")

    @property
    def available(self) -> bool:
        return self._wg_quick is not None and self._wg is not None

    def write_conf(self, content: str) -> Path:
        path = self.config.wg_conf_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o600)
        logger.info("wrote WireGuard config to %s", path)
        return path

    def up(self, content: str) -> None:
        if not self.available:
            raise RuntimeError("wg-quick/wg not found; cannot bring up WireGuard")
        self.write_conf(content)
        subprocess.run(
            [self._wg_quick, "up", self.config.wg_interface],
            check=True,
            capture_output=True,
            text=True,
        )
        logger.info("WireGuard interface %s is up", self.config.wg_interface)

    def down(self) -> None:
        if not self.available:
            return
        subprocess.run(
            [self._wg_quick, "down", self.config.wg_interface],
            capture_output=True,
            text=True,
        )
        logger.info("WireGuard interface %s is down", self.config.wg_interface)

    def is_up(self) -> bool:
        if not self._wg:
            return False
        result = subprocess.run(
            [self._wg, "show", self.config.wg_interface],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
