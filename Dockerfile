# ─────────────────────────────────────────────────────────────
# prima-pool-client — worker agent container
# ─────────────────────────────────────────────────────────────
# Runs the worker agent daemon. It needs:
#   • WireGuard tools (wg, wg-quick) to bring up the tunnel
#   • docker CLI to orchestrate prima.cpp (prima-docker blueprint)
#   • CAP_NET_ADMIN + /dev/net/tun to manage the WG interface
# All configuration is via environment variables (PRIMA_POOL_*).
# ─────────────────────────────────────────────────────────────
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install the package.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# ── Runtime ─────────────────────────────────────────────────
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Runtime deps: WireGuard tools + docker CLI (to launch prima.cpp).
RUN apt-get update && apt-get install -y --no-install-recommends \
        wireguard-tools \
        iproute2 \
        docker.io \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy the installed package from the build stage.
COPY --from=base /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=base /usr/local/bin/prima-pool-client /usr/local/bin/prima-pool-client

# State + WireGuard config live here; mount a volume to persist.
ENV PRIMA_POOL_STATE_PATH=/data/client-state.json \
    PRIMA_POOL_WG_CONF_DIR=/etc/wireguard
VOLUME ["/data"]

# The agent needs NET_ADMIN to manage the WireGuard interface.
# (Set via docker-compose capabilities; documented in README.)

CMD ["prima-pool-client", "run"]
