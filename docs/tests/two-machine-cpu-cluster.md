# Tested deployment — two-machine CPU inference cluster

**Validated end-to-end: 2026-08-11.** A live prima-pool with **two CPU-only
machines** serving OpenAI-compatible completions over the public internet.
This page records the exact topology, hardware, memory limits, and measured
performance so future operators know the stack works as deployed.

## Topology

```
                        internet
                            │
                 ┌──────────┴───────────┐
                 │  Pool VPS (24fire)   │  <public IP> — control plane
                 │  public IP           │  (prima-pool-server) + provider
                 │  rank 1 → llama-cli  │  client (contributes compute)
                 └──────────┬───────────┘
                            │ HTTPS/WSS (control plane)
                 ┌──────────▼───────────┐
                 │  Laptop (gbook)      │  behind home NAT
                 │  provider client     │
                 │  rank 0 → llama-server│ (head)
                 └──────────────────────┘

   ring (2 workers): laptop ↔ VPS over WireGuard, 10.23.1.0/24
```

- **Machine A — the one running the pool**: a publicly reachable VPS
  (public IP, see the deploy notes). Runs the `prima-pool-server` control
  plane, and also joins the cluster as a provider (client agent + rank 1
  worker, `llama-cli`).
- **Machine B — behind a NAT**: a home laptop. Runs the client agent
  (rank 0 head, `llama-server`), reached by the pool's inference proxy over
  the cluster WireGuard network.

Ring as launched by the agents (`world=2`):

| Node | Launch line (relevant flags) |
|---|---|
| Laptop | `llama-server --rank 0 --master 10.23.1.1 --next 10.23.1.2` |
| VPS | `llama-cli --rank 1 --master 10.23.1.1 --next 10.23.1.1` (next closes the ring) |

Both ranks exchanged layer windows after profiling, so **control + data plane
(WireGuard) were verified up** — including the NAT'd laptop.

## Stack

- `prima-pool-server` + `prima-pool-client` (same-container mode: the agent
  execs prima.cpp in its own container, sharing the WG network namespace)
- prima.cpp (llama.cpp fork) with HiGHS LP solver; **CPU-only** build (`CUDA=0`)
- Docker Compose on both machines
- Model: **Qwen2.5 Coder 3B Instruct, GGUF Q5_K_M** — 3.40 B params, 2.27 GiB,
  context 4096
- WireGuard tunnel between members (10.23.x.x, UDP 51820)

## Hardware (as reported by prima.cpp's profiler)

| | Laptop (gbook) — rank 0 | Pool VPS (24fire) — rank 1 |
|---|---|---|
| CPU | 12th Gen Intel Core | AMD EPYC (4 vCPU) |
| Cores (logical) | 12 | 4 |
| CPU F32 GFLOPS | 23.8 | 204.6 |
| CPU F16 GFLOPS | 17.8 | 154.6 |
| CPU Q5K GFLOPS | 14.7 | 96.1 |
| RAM read BW (GB/s) | 10.19 | 57.56 |
| KV cache copy (ms/layer) | 0.04 | 0.01 |
| Disk seq read / write (GB/s) | 1.57 / 1.19 | 0.70 / 0.10 |
| Disk rnd read / write (GB/s) | 0.09 / 0.14 | 0.03 / 0.00 |
| Mem total / available (GiB) * | 8.00 / 7.90 | 6.00 / 5.91 |

\* prima.cpp prints `Using cgroup v1, the available memory could be error`
when running under Docker with cgroup v1, and its cgroup-derived readings are
not authoritative — see the configured limits below.

## Memory allocation

Docker `mem_limit` per container — this is what Halda's profiler reads from
cgroups; without it prima.cpp crashes (`Failed to parse total physical
memory`). See the known-issues fix baked into this repo's Dockerfile/compose.

| Container | mem_limit | Notes |
|---|---|---|
| Laptop (gbook) | **4 GB** (`PRIMA_POOL_MEM_LIMIT=4g`) | enough for the 2.27 GiB Q5_K + KV/compute buffers (~155–457 MiB); tight but stable in this test |
| Pool VPS (24fire) | **6 GB** (`PRIMA_POOL_MEM_LIMIT=6g`) | comfortable headroom |

Rule of thumb (from prima-docker): `mem_limit` ≥ model size + 2 GB, or the
container is OOM-killed (exit 137). The laptop's 4 GB sits at the low edge of
that guidance — fine for this 3B model, not for larger ones.

## Performance (measured 2026-08-11)

Streaming completion through the pool's OpenAI-compatible endpoint
(`POST /v1/chat/completions`, user-scoped key):

- Prompt **28 tokens** → completion **21 tokens**, SSE chunks
- **~4–5 tokens/s** end-to-end (21 tokens streamed over ~5 s through the WG
  tunnel + pool proxy)
- First chunk latency: sub-second once the cluster reports `live`
- Layer split chosen by HiGHS (`k = 1`): laptop **1 layer** (`win_size = 1`),
  VPS **35 layers** (`win_size = 35`) — the EPYC does ~6.5× the laptop's
  quantized GFLOPS, so the solver pinned ~97 % of the model there

The answer was coherent and context-aware, proving the full path:
control plane → cluster assignment → WireGuard ring → distributed decode →
proxy → SSE.

Operational notes from the run:

- Requests sent while the cluster is still warming up return
  `503 {"error":..., "type":"unavailable_error"}` ("Loading model"); retry once
  the head logs `main: model loaded` / `main: server is listening` (cluster
  flips to `live`).
- CPU-only — no `--gpu-mem` / GPU flags on either node.

## Reproduce

Same `docker-compose.yml` + `.env` on both machines — see the
[setup guide](../../../prima-pool-server/docs/guides/setup.md). Both hosts
needed the two Docker/Halda fixes already baked into the client image
(`mem_limit` for cgroup profiling, plus the `fio` package for Halda's disk
benchmark), documented in `prima-docker/README.md` → "Known issues and fixes".