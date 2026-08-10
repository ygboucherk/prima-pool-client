# prima-pool-client

A **worker agent** for the prima-pool inference pool. It runs on each provider
machine and implements the worker lifecycle against the control plane
(`prima-pool-server`):

```
register → waitlist → assigned (WireGuard up + prima.cpp up + ready) → dissolve/leave
```

It uses `prima-docker` **only as a behavioral blueprint** — it reproduces the
same env-var semantics (RANK/WORLD/MASTER_IP/NEXT_IP, profiles, MEM_LIMIT) but
generates them dynamically from the cluster assignment.

> **First time?** Follow the
> [Docker setup guide](docs/guides/setup-docker.md) (recommended) or the
> [no-Docker guide](docs/guides/setup-nodocker.md) (bare-metal agent) —
> bootstrap, config, model, and start are covered step by step.

## What it does

- **Bootstrap** — register an account and create a worker-scoped API key
- **Register** — declares a model + self-declared memory + WireGuard pubkey +
  the **SHA-256 of the local GGUF**; the server only groups workers with the
  same GGUF hash, so a mismatched model/quantization is rejected at registration
- **Heartbeat** — keeps the worker online (default every 10 s)
- **WebSocket** — listens for `cluster_assigned` / `cluster_dissolved` with
  reconnect/backoff; REST is the source of truth
- **WireGuard** — generates a keypair locally (private key never leaves the
  device), renders `/etc/wireguard/prima-pool.conf`, brings the tunnel up
- **prima.cpp** — launches the ring node in the **same container** with the
  correct RANK/WORLD/MASTER_IP/NEXT_IP from the cluster config
- **Readiness** — reports `POST /clusters/{id}/ready` after WG + prima.cpp are up

> **Server peer:** when the pool server joins the cluster WG network (option A),
> it appears in the cluster config as a peer with `role: "server"`. The client
> excludes server peers from ring topology computation (`WORLD`/`NEXT_IP`), so
> the prima.cpp ring is built from worker members only.

## Quick start (local)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 1. One-time bootstrap (register account + create worker key)
prima-pool-client bootstrap --pool-url http://127.0.0.1:8000

# 2. Run the agent
export PRIMA_POOL_API_KEY=sk-worker-...
export PRIMA_POOL_URL=http://127.0.0.1:8000
prima-pool-client run
```

## Quick start (docker)

The client and prima.cpp run in a **single container**, sharing one network
namespace — so the WireGuard tunnel the agent brings up is directly visible to
prima.cpp. No docker socket, no docker-in-docker.

Deployed by each **provider** on their own device, pointed at the pool
operator's server:

```bash
cp .env.example .env
# set PRIMA_POOL_URL (operator's server) + PRIMA_POOL_API_KEY in .env
docker compose up -d
```

The container needs `CAP_NET_ADMIN` and `/dev/net/tun` to manage the WireGuard
interface — already configured in this repo's `docker-compose.yml`.

**Model (default: mount)**: put the GGUF in `./models/` on the host (or set
`PRIMA_POOL_MODEL_DIR`); it's mounted read-only into the container at `/models`.
This keeps the image small — a 30-70B GGUF is 20-40+ GB and must not be baked
into the image. For small models only, you can instead bake one in at build
time via the `MODEL_URL` build arg. The model must be the same one the pool
operator expects, and ideally identical across all cluster members.

## Configuration

All settings are read from `PRIMA_POOL_*` environment variables (or a TOML file
via `--config`). See `src/prima_pool_client/config.py` for defaults.

| Variable | Default | Description |
|---|---|---|
| `PRIMA_POOL_URL` | `http://127.0.0.1:8000` | Control plane base URL |
| `PRIMA_POOL_API_KEY` | — | Worker-scoped API key (**required**) |
| `PRIMA_POOL_MODEL` | `demo-model` | Model to serve |
| `PRIMA_POOL_MEMORY_MB` | `4096` | Self-declared memory to allocate |
| `PRIMA_POOL_WG_PRIVATE_KEY` | auto | WireGuard private key (auto-generated if empty) |
| `PRIMA_POOL_WG_LISTEN_PORT` | `51820` | WG listen port |
| `PRIMA_POOL_WG_INTERFACE` | `prima-pool` | WG interface name |
| `PRIMA_POOL_WG_ENDPOINT_HOST` | auto | Explicit WG endpoint host (public IP / Tailscale IP / hostname). Empty = server uses the IP it observes on the registration connection |
| `PRIMA_POOL_WG_RELAY_CHECK_S` | `10` | Seconds between direct→relay fallback health checks (relay monitor) |
| `PRIMA_POOL_WG_CONF_DIR` | `/etc/wireguard` | Where the WG config is written |
| `PRIMA_POOL_PRIMA_MODE` | `same-container` | `same-container` (exec in this container) or `docker` (compose) |
| `PRIMA_POOL_PRIMA_DIR` | `~/prima` | prima-docker project dir (docker mode only) |
| `PRIMA_POOL_MODEL_FILE` | `model.gguf` | GGUF filename (used by `PRIMA_POOL_MODEL_PATH` fallback + docker mode) |
| `PRIMA_POOL_MODEL_PATH` | `/models/model.gguf` | Absolute GGUF path inside the container |
| `PRIMA_POOL_MEM_LIMIT` | `8g` | Memory limit for prima.cpp (≥ model size + 2 GB) |
| `PRIMA_POOL_GPU_MEM_FLAG` | — | e.g. `--gpu-mem 8` |
| `PRIMA_POOL_CTX_SIZE` | `4096` | Context window |
| `PRIMA_POOL_API_PORT` | `8080` | prima.cpp server port (head only) |
| `PRIMA_POOL_BATCH_FLAGS` | — | e.g. `-np 4 --cont-batching` |
| `PRIMA_POOL_EXTRA_FLAGS` | — | Extra prima.cpp flags |
| `PRIMA_POOL_STATE_PATH` | `~/.local/state/...` | Agent state persistence |

## Tests

```bash
pip install -e ".[dev]"
pytest
```

## Layout

```
src/prima_pool_client/
├── agent.py        # Worker lifecycle control loop
├── cli.py          # bootstrap / run / genkey commands
├── config.py       # ClientConfig
├── models.py       # Response schemas
├── prima.py        # prima.cpp launcher (docker/native)
├── rest.py         # Typed REST client
├── wireguard.py    # Keygen, config render, wg-quick bring-up
└── ws_client.py    # WebSocket push channel with reconnect
```