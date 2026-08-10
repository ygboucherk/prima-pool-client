# Setup guide (no Docker) — joining the pool as a provider, bare-metal

Run the worker agent **directly on the host**, without containers. Useful for
development, debugging, or providers who prefer not to run Docker (or whose
kernel lacks WG support in Docker but has it on the host).

> Prefer the packaged, single-container route? See
> [setup-docker.md](setup-docker.md) — it's the recommended way.

**What changes vs. Docker:** you install prima.cpp (llama.cpp fork) yourself
and put its binaries on `PATH`, you install the client package into a venv,
and the agent manages a **host** WireGuard interface (`wg-quick`) — so you need
`CAP_NET_ADMIN`-equivalent privileges (root / sudo) on this machine.

---

## Architecture recap

```
   user (sk-user) ─▶ prima-pool-server ◀─ bootstrap / heartbeat
                          │  cluster_config / WS push
              ┌───────────┴───────────┐
              │   provider device     │
              │   wg-quick tunnel     │
              │   worker agent  ─────▶  prima.cpp (llama-server/cli)
              └───────────────────────┘
```

---

## Part 0 — Prerequisites

- A **Linux** host with root (or sudo)
- WireGuard kernel module + `/dev/net/tun` on the **host** (the agent manages
  a real `wg-quick` interface)
- Python ≥ 3.13
- The operator's `PRIMA_POOL_URL` and a registered model name
  (e.g. `deepseek-v4-flash-0731`) — ask the operator, or check
  `GET <url>/v1/models` (unauthenticated)

### 0.1 WireGuard kernel support

```bash
# Debian/Ubuntu
sudo apt install wireguard-tools
sudo modprobe wireguard

# Verify
ls /dev/net/tun      # must exist
cat /proc/modules | grep wireguard
```

If `modprobe wireguard` fails, install the kernel headers and rebuild the
module, or use a kernel with built-in WG support.

---

## Part 1 — Install prima.cpp

The agent launches `llama-server` (rank 0 / head) or `llama-cli` (other ranks)
from `PATH` (`same-container` mode without a container). Build it once:

```bash
# System deps (Debian/Ubuntu; adjust for your distro)
sudo apt install build-essential cmake git pkg-config \
     libcurl4-openssl-dev libzmq3-dev libaio-dev

# HiGHS — Halda's LP solver (prima.cpp needs it)
git clone --depth 1 https://github.com/ERGO-Code/HiGHS.git /tmp/highs
cd /tmp/highs
cmake -B build -DCMAKE_INSTALL_PREFIX=/usr/local
cmake --build build -j$(nproc)
sudo cmake --install build && sudo ldconfig
cd ..

# prima.cpp
git clone --depth 1 https://github.com/OpenCPIL/prima.cpp.git
cd prima.cpp
# Patch the cgroup v1/v2 detection bug (also present on bare hosts with cgroups)
sed -i 's|is_cgroup_v2 = true;|is_cgroup_v2 = (access("/sys/fs/cgroup/memory.max", F_OK) == 0);|' common/profiler.cpp \
  && grep -q '#include <unistd.h>' common/profiler.cpp \
      || sed -i '/#include <fstream>/a #include <unistd.h>' common/profiler.cpp

# CPU build. For GPU: make USE_HIGHS=1 GGML_CUDA=1 -j$(nproc)
make USE_HIGHS=1 -j$(nproc)

# Put the binaries on PATH for the agent
sudo cp llama-cli llama-server /usr/local/bin/
```

Verify:

```bash
which llama-server llama-cli   # both must be on PATH
```

---

## Part 2 — Install the client agent

```bash
git clone <client-repo-url> && cd prima-pool-client
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

---

## Part 3 — Create an account + worker key

The agent authenticates with a **worker-scoped API key** (`sk-worker-...`)
belonging to an account on the server. Creating both is a one-time step.

### 3.1 Recommended: from the web UI

The operator's dashboard (`<url>/ui`) can create keys for you:

1. Open `https://pool.example.com/ui` and **register** an account (or log in).
2. In the **API keys** section, enter a name (e.g. `device-1`), pick the
   **worker** scope, and click **Create key**.
3. Copy the `sk-worker-...` secret shown — it is displayed **once**.

> ⚠️ The worker key is shown **once** — save it. If you lose it, create a new
> one from the dashboard.

### 3.2 Alternative: `bootstrap` CLI

If you prefer the command line, run `bootstrap` — it registers the account and
prints a worker key:

```bash
prima-pool-client bootstrap --pool-url https://pool.example.com
# prompts for username + password (creates the account if needed)
# → prints account_id + worker key (sk-worker-...)
```

You can also pass `--username` / `--password` / `--name` non-interactively.

---

## Part 4 — Configure the agent

Configuration comes from `PRIMA_POOL_*` env vars (or a TOML file via
`--config`). Create a `.env` helper (the agent doesn't read `.env` itself —
`export` it, or use a tool like `direnv` / `dotenv`):

```bash
cp .env.example .env
# Edit, then source it:
set -a; source .env; set +a
```

### 4.1 Required variables

```bash
# URL of the operator's control plane
PRIMA_POOL_URL=https://pool.example.com

# Worker-scoped API key from bootstrap (sk-worker-...)
PRIMA_POOL_API_KEY=sk-worker-...
```

### 4.2 Model + memory

```bash
# Model to serve — must exactly match a model in the operator's registry
PRIMA_POOL_MODEL=demo-model

# Self-declared memory to allocate (MB)
PRIMA_POOL_MEMORY_MB=4096

# Memory limit for prima.cpp (≥ model size + 2 GB)
PRIMA_POOL_MEM_LIMIT=8g
```

### 4.3 Model file

No container mount — point `PRIMA_POOL_MODEL_PATH` straight at your GGUF:

```bash
PRIMA_POOL_MODEL_PATH=/data/models/<model>.gguf
```

> 🔒 The agent computes the **SHA-256** of this file at registration and the
> server rejects (400) any file that doesn't match the registered model's
> pinned hash. All cluster members must serve the **byte-identical** GGUF.

If `PRIMA_POOL_MODEL_PATH` is left empty, the agent falls back to
`<PRIMA_POOL_PRIMA_DIR>/models/<PRIMA_POOL_MODEL_FILE>` (default
`~/prima/models/model.gguf`) — used by the legacy `docker` prima mode, not
needed here.

### 4.4 WireGuard

The agent creates a host interface named `PRIMA_POOL_WG_INTERFACE` (default
`prima-pool`) via `wg-quick`:

```bash
# Leave empty to auto-generate a keypair on first run (private key never
# leaves the device; stored in the state file)
PRIMA_POOL_WG_PRIVATE_KEY=
PRIMA_POOL_WG_LISTEN_PORT=51820
PRIMA_POOL_WG_INTERFACE=prima-pool
```

The WG **endpoint host** is the hard part when devices are behind NAT:

```bash
# RECOMMENDED behind NAT: leave empty. The server uses the source IP it
# observes on the registration connection.
PRIMA_POOL_WG_ENDPOINT_HOST=

# Alternative: an IP/hostname reachable by other members
# (public IP, Tailscale IP, hostname)
# PRIMA_POOL_WG_ENDPOINT_HOST=203.0.113.7
```

If direct peer-to-peer WG is impossible (symmetric NAT / CGNAT), the operator
can deploy a **relay**; the client then falls back automatically
(`PRIMA_POOL_WG_RELAY_CHECK_S=10`).

> On a bare host, `wg-quick` needs NET_ADMIN → run the agent as root or a user
> with `CAP_NET_ADMIN` on the interface, e.g. `sudo -E prima-pool-client run`.

### 4.5 GPU (optional)

```bash
PRIMA_POOL_GPU_MEM_FLAG=--gpu-mem 8   # e.g. offload 8 GiB
```

---

## Part 5 — Run the worker

```bash
sudo -E .venv/bin/prima-pool-client run --log-level info
```

The agent will:

1. **Register** the worker (model + memory + WG pubkey + GGUF hash)
2. **Heartbeat** every 10 s and listen on the WebSocket channel
3. When enough matching workers are online, the server forms a cluster and
   pushes `cluster_assigned`
4. Bring up **WireGuard** (`wg-quick up prima-pool`), launch **prima.cpp**
   (`llama-server`/`llama-cli`) on this host, and report **ready**

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

## Part 6 — Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `llama-server not found` on assignment | prima.cpp not on `PATH` of the run user | Part 1; `which llama-server` |
| `wg-quick/wg not found` | wireguard-tools missing on host | `sudo apt install wireguard-tools` |
| `operation not permitted` on WG up | No NET_ADMIN for the agent's user | Run with `sudo -E` / grant CAP_NET_ADMIN |
| `400 ... gguf_sha256 does not match` | GGUF ≠ the model the registry pins | Use the exact file the operator registered; same hash on all members |
| `Failed to parse total physical memory` | `MEM_LIMIT` too small / cgroup issue | `PRIMA_POOL_MEM_LIMIT` ≥ model size + 2 GB |
| Worker never assigned | Not enough matching memory, or model/hash mismatch | `GET /v1/models`; add workers with identical `PRIMA_POOL_MODEL` + GGUF |
| WG interface won't come up | No `/dev/net/tun` / kernel module | Part 0.1 |
| Peer handshakes fail / cluster dissociates | Endpoint not reachable / NAT | Set `PRIMA_POOL_WG_ENDPOINT_HOST` to a reachable IP, or ask the operator for a relay |
| Tunnel up but no traffic | Firewall blocking UDP `51820` (and `51822` for relay) | Open the ports / check cloud security groups |

---

## Part 7 — Updating

```bash
git pull
# reinstall the client
pip install -e ".[dev]"
# rebuild prima.cpp if you pulled new upstream
cd prima.cpp && git pull && make USE_HIGHS=1 -j$(nproc) && sudo cp llama-cli llama-server /usr/local/bin/
```

State (worker_id, WG keypair) persists in `PRIMA_POOL_STATE_PATH` (default
`~/.local/state/prima-pool/client-state.json`), so the worker keeps its
identity across restarts.

---

## Related docs

- [README](../README.md) — overview, config table, layout
- [.env.example](../.env.example) — every variable with comments
- [setup-docker.md](setup-docker.md) — single-container deployment (recommended)
- Server setup guide — full end-to-end (server + relay + using the pool):
  `prima-pool-server/docs/guides/setup.md`