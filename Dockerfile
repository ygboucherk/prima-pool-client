# ─────────────────────────────────────────────────────────────
# prima-pool-client — single container: worker agent + prima.cpp
# ─────────────────────────────────────────────────────────────
# The client agent and prima.cpp run in the SAME container, so they
# share one network namespace. The WireGuard interface the agent
# brings up is directly visible to prima.cpp — no docker-in-docker,
# no separate containers, no namespace mismatch.
#
# Build args:
#   CUDA          "0" (default) CPU-only, "1" for GPU
#   BUILDER_BASE  build-stage base image
#   RUNTIME_BASE  runtime-stage base image
#   MODEL_URL     optional URL to download a GGUF model at build time
#   MODEL_PATH    where the model lives in the image (default /models/model.gguf)
#
# The client package requires Python >= 3.13, so the default bases are
# python:3.13-slim (Debian bookworm) which also ships the libs prima.cpp needs.
#
# For CUDA builds you must supply bases that have BOTH Python 3.13 AND the
# CUDA runtime, e.g.:
#   BUILDER_BASE=nvidia/cuda:12.6.0-devel-ubuntu22.04
#   RUNTIME_BASE=nvidia/cuda:12.6.0-runtime-ubuntu22.04
# (and install Python 3.13 in those stages — see the CUDA note below).
# ─────────────────────────────────────────────────────────────
ARG CUDA=0
ARG BUILDER_BASE=python:3.13-slim
ARG RUNTIME_BASE=python:3.13-slim

# ═══════════════════════════════════════════════════════════
# Stage 1: Builder — compile prima.cpp + install the client
# ═══════════════════════════════════════════════════════════
FROM ${BUILDER_BASE} AS builder

ARG CUDA=0

# Build deps (same as prima-docker).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        git \
        python3 \
        python3-pip \
        python3-venv \
        curl \
        pkg-config \
        libcurl4-openssl-dev \
        libzmq3-dev \
        libaio-dev \
    && rm -rf /var/lib/apt/lists/*

# Build and install HiGHS (Halda's LP solver).
RUN git clone --depth 1 https://github.com/ERGO-Code/HiGHS.git /tmp/highs \
    && cd /tmp/highs \
    && cmake -B build -DCMAKE_INSTALL_PREFIX=/usr/local \
    && cmake --build build -j$(nproc) \
    && cmake --install build \
    && ldconfig \
    && rm -rf /tmp/highs

# Clone prima.cpp source.
RUN git clone --depth 1 https://github.com/OpenCPIL/prima.cpp.git /root/prima.cpp
WORKDIR /root/prima.cpp

# Patch the cgroup v1/v2 detection bug (same as prima-docker).
RUN sed -i 's|is_cgroup_v2 = true;|is_cgroup_v2 = (access("/sys/fs/cgroup/memory.max", F_OK) == 0);|' common/profiler.cpp \
    && grep -q '#include <unistd.h>' common/profiler.cpp \
        || sed -i '/#include <fstream>/a #include <unistd.h>' common/profiler.cpp

# Build prima.cpp with HiGHS.
RUN if [ "$CUDA" = "1" ]; then \
        make USE_HIGHS=1 GGML_CUDA=1 -j$(nproc); \
    else \
        make USE_HIGHS=1 -j$(nproc); \
    fi

# Collect binaries + shared libs.
RUN mkdir -p /export/bin /export/lib \
    && for f in llama-cli llama-server llama-gguf-split; do \
        [ -f "$f" ] && cp "$f" /export/bin/; \
    done; \
    for f in libggml.so* libllama.so*; do \
        [ -e "$f" ] && cp "$f" /export/lib/; \
    done; \
    true

# Install the client package (build isolation needs setuptools/wheel).
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python3 -m pip install --no-cache-dir .

# ═══════════════════════════════════════════════════════════
# Stage 2: Runtime — slim image with prima.cpp + client
# ═══════════════════════════════════════════════════════════
FROM ${RUNTIME_BASE} AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PRIMA_POOL_PRIMA_MODE=same-container \
    PRIMA_POOL_MODEL_PATH=/models/model.gguf

# Runtime deps: prima.cpp libs + WireGuard tools (agent manages the tunnel).
# Note: python:3.13-slim is Debian trixie, where libaio is libaio1t64.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libcurl4 \
        libgomp1 \
        libzmq5 \
        libaio1t64 \
        wireguard-tools \
        iproute2 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy HiGHS + prima.cpp shared libs and binaries.
COPY --from=builder /usr/local/lib/libhighs.so* /usr/local/lib/
COPY --from=builder /export/lib/ /usr/local/lib/
COPY --from=builder /export/bin/ /usr/local/bin/
RUN ldconfig

# Copy the installed client package.
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin/prima-pool-client /usr/local/bin/prima-pool-client

# The model is MOUNTED at runtime (default) — see docker-compose.yml
# PRIMA_POOL_MODEL_DIR. We only create the mount point here; we do NOT bake
# the GGUF into the image, because 30-70B models are 20-40+ GB and would make
# the image impractically large. To bake a small model instead, override
# MODEL_URL at build time (see README).
ARG MODEL_URL=""
ARG MODEL_PATH=/models/model.gguf
RUN mkdir -p /models \
    && if [ -n "$MODEL_URL" ]; then \
        curl -L -o "$MODEL_PATH" "$MODEL_URL"; \
    fi

# State + WireGuard config live here; mount a volume to persist.
ENV PRIMA_POOL_STATE_PATH=/data/client-state.json \
    PRIMA_POOL_WG_CONF_DIR=/etc/wireguard
VOLUME ["/data"]

# The agent manages a WireGuard interface, so it needs NET_ADMIN + /dev/net/tun.
# (Set via docker-compose capabilities; documented in README.)

CMD ["prima-pool-client", "run"]
