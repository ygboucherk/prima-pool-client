"""Unit tests for the client: WireGuard rendering, prima env building, keygen."""
from __future__ import annotations

from prima_pool_client.config import ClientConfig
from prima_pool_client.models import ClusterConfig, InterfaceConfig, PeerConfig, Preferred
from prima_pool_client.prima import build_env, _ring_neighbors
from prima_pool_client.wireguard import derive_public_key, generate_keypair, render_wg_conf


def _sample_cluster() -> ClusterConfig:
    return ClusterConfig(
        cluster_id="clu_1",
        interface=InterfaceConfig(private_ip="10.23.1.2", subnet="10.23.1.0/24", mtu=1280),
        peers=[
            PeerConfig(pubkey="A", endpoint="203.0.113.1:51820", allowed_ips=["10.23.1.1/32"], preferred=Preferred.direct),
            PeerConfig(pubkey="B", endpoint="203.0.113.2:51820", allowed_ips=["10.23.1.2/32"], preferred=Preferred.direct),
            PeerConfig(pubkey="C", endpoint="203.0.113.3:51820", allowed_ips=["10.23.1.3/32"], preferred=Preferred.relay),
        ],
    )


def test_generate_keypair_roundtrip():
    priv, pub = generate_keypair()
    assert derive_public_key(priv) == pub
    assert len(priv) > 0 and len(pub) > 0


def test_render_wg_conf():
    cluster = _sample_cluster()
    conf = render_wg_conf(cluster, "PRIVATEKEY", 51820)
    assert "[Interface]" in conf
    assert "PrivateKey = PRIVATEKEY" in conf
    assert "Address = 10.23.1.2/24" in conf
    assert "MTU = 1280" in conf
    assert "PublicKey = A" in conf
    assert "PublicKey = C" in conf
    assert "AllowedIPs = 10.23.1.1/32" in conf


def test_ring_neighbors():
    cluster = _sample_cluster()
    master, nxt = _ring_neighbors(cluster, 1)
    assert master == "10.23.1.1"  # peers[0]
    assert nxt == "10.23.1.3"  # peers[2]


def test_ring_neighbors_wraps():
    cluster = _sample_cluster()
    master, nxt = _ring_neighbors(cluster, 2)
    assert master == "10.23.1.1"
    assert nxt == "10.23.1.1"  # wraps back to head


def test_ring_neighbors_excludes_server_peer():
    # A cluster with 2 ring members + 1 server peer (role="server").
    cluster = ClusterConfig(
        cluster_id="clu_1",
        interface=InterfaceConfig(private_ip="10.23.1.2", subnet="10.23.1.0/24", mtu=1280),
        peers=[
            PeerConfig(pubkey="A", allowed_ips=["10.23.1.1/32"], preferred=Preferred.direct),
            PeerConfig(pubkey="B", allowed_ips=["10.23.1.2/32"], preferred=Preferred.direct),
            PeerConfig(pubkey="SRV", allowed_ips=["10.23.1.254/32"], preferred=Preferred.direct, role="server"),
        ],
    )
    # Ring has 2 members; world must be 2, not 3.
    from prima_pool_client.prima import _ring_members

    assert len(_ring_members(cluster)) == 2
    master, nxt = _ring_neighbors(cluster, 1)
    assert master == "10.23.1.1"
    assert nxt == "10.23.1.1"  # wraps to head (only 2 ring members)


def test_build_env_head():
    cfg = ClientConfig(model_file="model.gguf", gpu_mem_flag="")
    cluster = _sample_cluster()
    env = build_env(cfg, cluster, ring_position=0, world=3, master_ip="10.23.1.1", next_ip="10.23.1.2")
    assert env["RANK"] == "0"
    assert env["WORLD"] == "3"
    assert env["MODE"] == "llama-server"
    assert env["COMPOSE_PROFILES"] == "server-cpu"


def test_build_env_worker():
    cfg = ClientConfig(model_file="model.gguf", gpu_mem_flag="")
    cluster = _sample_cluster()
    env = build_env(cfg, cluster, ring_position=1, world=3, master_ip="10.23.1.1", next_ip="10.23.1.3")
    assert env["RANK"] == "1"
    assert env["MODE"] == "llama-cli"
    assert env["COMPOSE_PROFILES"] == "cpu"


def test_model_path_resolution_explicit():
    from prima_pool_client.prima import PrimaLauncher

    cfg = ClientConfig(model_path="/models/my-model.gguf")
    launcher = PrimaLauncher(cfg)
    assert launcher._resolve_model_path() == "/models/my-model.gguf"


def test_model_path_resolution_default():
    from prima_pool_client.prima import PrimaLauncher

    cfg = ClientConfig(prima_dir="/prima", model_file="model.gguf", model_path="")
    launcher = PrimaLauncher(cfg)
    assert launcher._resolve_model_path() == "/prima/models/model.gguf"


def test_same_container_default_mode():
    cfg = ClientConfig()
    assert cfg.prima_mode == "same-container"
