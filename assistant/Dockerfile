# Named stages keep copied tool images visible to Dependabot.
FROM ghcr.io/astral-sh/uv:0.12.0@sha256:606e70c71c852d03f611b1e56a195d08648507018a7057fab82c4974c4eae105 AS uv
FROM mwader/static-ffmpeg:7.1@sha256:a8090df5f5608daef387e1b2e93b98aaacb4d92153ad904e7d715c725724fca4 AS ffmpeg

FROM python:3.14-slim AS builder

# uv is only needed to build the venv; it stays out of the final image
COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first (no dev deps, no editable) so this layer
# stays cached across source-only changes
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-editable --no-install-project

# Copy sources and install the project itself
COPY src/ ./src/
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.14-slim

# ffmpeg converts Telegram voice notes (OGG/Opus) to MP3 for transcription.
# A static binary instead of Debian's ffmpeg package, whose dependency
# tree (libLLVM, Mesa, codec libs) adds ~400MB.
COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg

# git powers the optional vault backup (local-only commit history)
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Non-root user
RUN useradd -m -u 1000 assistant
WORKDIR /app

# The venv symlinks to /usr/local/bin/python3, identical in both stages
COPY --from=builder /app/.venv ./.venv

# Copy config example (user mounts actual config at runtime)
COPY config.example.toml ./

# Data directories (mounted as volumes at runtime).
# /app stays root-owned: chown -R would duplicate the venv layer.
RUN mkdir -p /data/vault /data/state && chown -R assistant:assistant /data

# Where this image keeps the vault and state. The application defaults to
# paths relative to the working directory; the container layout is a property
# of the image, so it is declared here rather than compiled into the code.
ENV VAULT_PATH=/data/vault \
    STATE_DIR=/data/state

USER assistant

# Default: run the bot
ENTRYPOINT ["/app/.venv/bin/assistant"]
CMD ["run"]
