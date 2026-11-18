# gc-mcp — headless Dolphin-controller MCP server.
#
# NOTE: This Dockerfile is provided as the intended deployment shape for running
# the server in a headless container, but it is NOT tested in the development
# environment (no Dolphin display / GPU available there). Review the Dolphin
# version that your base image ships: the stock "Pipes" input backend and the
# modern BBA modes (HLE / IPC local mode) require a reasonably recent Dolphin.

# Use Ubuntu LTS for a recent dolphin-emu package.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# ---------------------------------------------------------------------------
# System packages
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    dolphin-emu \
    xvfb \
    x11-utils \
    x11-apps \
    mesa-utils \
    libgl1-mesa-dri \
    libglx-mesa0 \
    libgl1 \
    libegl1 \
    libgles2 \
    libosmesa6 \
    ffmpeg \
    ca-certificates \
    curl \
    git \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# uv (python package manager)
# ---------------------------------------------------------------------------
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# The 'ocr' extra enables optional text extraction via
# gc_capture_screenshot(ocr=True). RapidOCR bundles its ONNX models, so no
# download is needed at runtime. Drop the extra to slim the image.
RUN uv sync --frozen --no-dev --extra ocr

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
# Xvfb screen size / depth used by the default capture backend.
ENV DISPLAY=:99
ENV XDG_RUNTIME_DIR=/tmp/xdg
ENV GC_MCP_CAPTURE_BACKEND=auto
# Use software Mesa if no GPU is passed through; the server auto-detects /dev/dri.
ENV LIBGL_ALWAYS_SOFTWARE=1

# Dolphin wants a writable home / config dir.
RUN mkdir -p /root/.gc-mcp /tmp/xdg

# The server is a stdio MCP server; the client attaches to this process.
ENTRYPOINT ["/app/.venv/bin/gc-mcp"]
CMD []
