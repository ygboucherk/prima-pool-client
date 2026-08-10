# Setup guide (Docker) — joining the pool as a provider

The **recommended** way to run a provider device. A single Docker container
bundles the worker agent **and** prima.cpp in one network namespace, so the
WireGuard tunnel the agent brings up is directly visible to prima.cpp — no
docker socket, no docker-in-docker, no dependency hell.

> Prefer a bare-metal agent without Docker? See
> [setup-nodocker.md](setup-nodocker.md).

---

## Architecture recap

```
                    ┌──────────────── pool operator ────────────────┐
   user (sk-user) ─▶│  prima-pool-server   ◀─ bootstrap / heartbeat │
                    └────────────────────┬──────────────────────────┘
                                         │ cluster_config / WS push
        ┌────────────────────────────────┴───────────────────────────────┐
        │                          provider device                        │
        │  ┌────────────────────────────────────────────────────────────┐  │
        │  │  client container (this guide)                             │  │
        │  │  ┌──────────────┐   WG tunnel    ┌──────────────────────┐  │  │
        │  │  │ worker agent │ ─────────────▶ │ prima.cpp (ring node)│  │  │
        │  │  └──────────────┘                 └──────────────────────┘  │  │
        │  └────────────────────────────────────────────────────────────┘  │
        └──────────────────────────────────────────────────────────────────┘
```

---

## Part 0 — Prerequisites

- A **Linux** host (macOS/Windows have no WG kernel module for Docker; unsupported)
- Docker + docker compose plugin
- WireGuard kernel module and `/dev/net/tun` on the host
- The operator's `PRIMA_POOL_URL` and a registered model name
  (e.g. `deepseek-v4-flash-0731`) — ask the operator, or check
  `GET <url>/v1/models` (unauthenticated)

### 0.1 WireGuard kernel support

The container manages a WireGuard interface (`CAP_NET_ADMIN` is already in the
compose file), but the **host kernel** must support it:

```bash
# Debian/Ubuntu
sudo apt install wireguard-tools
sudo modprobe wireguard

# Verify
ls /dev/net/tun      # must exist
cat /proc/modules | grep wireguard
```

Without `/dev/net/tun` the agent starts but the tunnel can't come up.

---

## Part 1 — Create an account + worker key

The agent authenticates with a **worker-scoped API key** (`sk-worker-...`)
belonging to an account on the server. Creating both is a one-time step.

### 1.1 Recommended: from the web UI

The operator's dashboard (`<url>/ui`) can create keys for you — no local
Python needed:

1. Open `https://pool.example.com/ui` and **register** an account (or log in).
2. In the **API keys** section, enter a name (e.g. `device-1`), pick the
   **worker** scope, and click **Create key**.
3. Copy the `sk-worker-...` secret shown — it is displayed **once**.

> ⚠️ The worker key is shown **once** — save it. If you lose it, create a new
> one from the dashboard.

### 1.2 Alternative: `bootstrap` CLI

If you prefer the command line (or the dashboard is unreachable), run
`bootstrap` from any machine with the client installed — it registers the
account and prints a worker key:

```bash
git clone <client-repo-url> && cd prima-pool-client
python3 -m venv .bootstrap-venv && source .bootstrap-venv/bin/activate
pip install -e ".[dev]"

prima-pool-client bootstrap --pool-url https://pool.example.com
# prompts for username + password (creates the account if needed)
# → prints account_id + worker key (sk-worker-...)
```

You can also pass `--username` / `--password` / `--name` non-interactively.

> No local Python at all? Build the image first (Part 2.1), then
> `PRIMA_POOL_API_KEY=placeholder docker compose run --rm client \
> prima-pool-client bootstrap --pool-url https://pool.example.com`
> (compose requires `PRIMA_POOL_API_KEY` to be non-empty, hence the placeholder).

---

## Part 2 — Configure the device

### 2.1 Create `.env`

```bash
cp .env.example .env
```

### 2.2 Required variables

```bash
# URL of the operator's control plane
PRIMA_POOL_URL=https://pool.example.com

# Worker-scoped API key from bootstrap (sk-worker-...)
PRIMA_POOL_API_KEY=sk-worker-...
```

### 2.3 Model + memory

```bash
# Model to serve — must exactly match a model in the operator's registry
PRIMA_POOL_MODEL=demo-model

# Self-declared memory to allocate (MB). Together with the other members'
# memory this must meet the model's required_memory_mb before a cluster forms.
PRIMA_POOL_MEMORY_MB=4096

# Memory limit for prima.cpp (≥ model size + 2 GB, else Halda crashes)
PRIMA_POOL_MEM_LIMIT=8g
```

### 2.4 Model file

The GGUF is **mounted** into the container at `/models` (default
`PRIMA_POOL_MODEL_DIR=./models`), because a 30–70B model is 20–40+ GB and must
not be baked into the image:

```bash
mkdir -p models
cp /path/to/<model>.gguf models/
```

The agent expects it at `PRIMA_POOL_MODEL_PATH` (default
`/models/model.gguf`). Either **rename** the file to `model.gguf`, or keep the
name and set the env var:

```bash
# Optional, if your file isn't named model.gguf
PRIMA_POOL_MODEL_PATH=/models/<model>.gguf
```

> 🔒 The agent computes the **SHA-256** of this file at registration and the
> server rejects (400) any file that doesn't match the registered model's
> pinned hash. All cluster members must serve the **byte-identical** GGUF.

### 2.5 WireGuard

Defaults are fine for most setups:

```bash
# Leave empty to auto-generate a keypair on first run (private key never
# leaves the device)
PRIMA_POOL_WG_PRIVATE_KEY=
PRIMA_POOL_WG_LISTEN_PORT=51820
PRIMA_POOL_WG_INTERFACE=prima-pool
```

The WG **endpoint host** is the hard part when devices are behind NAT:

```bash
# RECOMMENDED behind NAT / in a container: leave empty. The server uses the
# source IP it observes on the registration connection.
PRIMA_POOL_WG_ENDPOINT_HOST=

# Alternative: an IP/hostname reachable by other members
# (public IP, Tailscale IP, hostname)
# PRIMA_POOL_WG_ENDPOINT_HOST=203.0.113.7
```

If direct peer-to-peer WG is impossible (symmetric NAT / CGNAT), the operator
can deploy a **relay**; the client then falls back to routing ring traffic
through it automatically (`PRIMA_POOL_WG_RELAY_CHECK_S=10`).

### 2.6 GPU (optional)

```bash
# In .env
CUDA=1
PRIMA_POOL_GPU_MEM_FLAG=--gpu-mem 8   # e.g. offload 8 GiB

# Build the GPU image (requires the nvidia container runtime)
docker compose build
```

---

## Part 3 — Start the worker

```bash
docker compose up -d                      # first run: builds the image
docker compose logs -f client             # watch registration + heartbeat
```

The agent will:

1. **Register** the worker (model + memory + WG pubkey + GGUF hash)
2. **Heartbeat** every 10 s and listen on the WebSocket channel
3. When enough matching workers are online, the server forms a cluster and
   pushes `cluster_assigned`
4. Bring up **WireGuard**, launch **prima.cpp** in-container, report **ready**

Verify:

```bash
# In the operator's web GUI: the worker should appear on the account dashboard
# Model registry (unauthenticated)
curl https://pool.example.com/v1/models
```

> ℹ️ One worker **cannot** form a cluster alone — the scheduler waits until the
> sum of memory of workers advertising the same `(model, gguf_sha256)` reaches
> `required_memory_mb`. Deploy 2+ devices with the identical model file.

---

## Part 4 — Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Container exits: `model file not found` | Wrong `PRIMA_POOL_MODEL_PATH` or no file in `./models/` | Check the path; `ls models/` on host |
| `400 ... gguf_sha256 does not match` | GGUF ≠ the model the registry pins | Use the exact file the operator registered; same hash on all members |
| `Failed to parse total physical memory` | `MEM_LIMIT` too small / cgroup issue | `PRIMA_POOL_MEM_LIMIT` ≥ model size + 2 GB |
| Worker never assigned | Not enough matching memory, or model/hash mismatch | `GET /v1/models`; add workers with identical `PRIMA_POOL_MODEL` + GGUF |
| WG interface won't come up | No `/dev/net/tun` / kernel module | Part 0.1; `docker compose logs client` for the exact error |
| Peer handshakes fail / cluster dissociates | Endpoint is a container IP / NAT | Set `PRIMA_POOL_WG_ENDPOINT_HOST` to a reachable IP, or ask the operator for a relay |
| Tunnel up but no traffic | Firewall blocking UDP `51820` (and UDP `51822` for relay) | Open the ports / check cloud security groups |
| `ModuleNotFoundError` at build | Docker build cache stale | `docker compose build --no-cache` |

---

## Part 5 — Updating

```bash
git pull
docker compose up -d --build
```

State (worker_id, WG keypair) persists in the `prima-pool-client-data` volume,
so the worker keeps its identity across updates.

---

## Related docs

- [README](../README.md) — overview, config table, layout
- [.env.example](../.env.example) — every variable with comments
- [setup-nodocker.md](setup-nodocker.md) — run the agent directly on the host
- Server setup guide — full end-to-end (server + relay + using the pool):
  `prima-pool-server/docs/guides/setup.md`