# syntax=docker/dockerfile:1

# Multi-stage: node build of frontend/ → uv install of backend/ → runtime (spec §3.1).

FROM node:22-slim AS frontend

# corepack fetches the pnpm that `packageManager` pins without asking first.
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    npm_config_store_dir=/pnpm-store

RUN corepack enable

WORKDIR /frontend

# Dependencies first, so source edits don't invalidate this layer.
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN --mount=type=cache,target=/pnpm-store \
    pnpm install --frozen-lockfile

COPY frontend ./
RUN pnpm build


FROM python:3.13-slim AS backend

COPY --from=ghcr.io/astral-sh/uv:0.13.3 /uv /bin/uv

# Build against the base image's interpreter so the venv is valid in the runtime stage.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=backend/uv.lock,target=uv.lock \
    --mount=type=bind,source=backend/pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project

COPY backend/pyproject.toml backend/uv.lock ./
COPY backend/src ./src
COPY backend/pbpstats_overrides ./pbpstats_overrides
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.13-slim

# A fixed UID/GID so the host-side volumes on kumadrome can be owned to match.
RUN groupadd --system --gid 999 closinglineup \
    && useradd --system --uid 999 --gid 999 --no-create-home closinglineup \
    && mkdir /data \
    && chown 999:999 /data

WORKDIR /app

COPY --from=backend /app/.venv ./.venv
COPY --from=backend /app/src ./src
# `parse.pbp.OVERRIDES_SOURCE`, copied into DATA_DIR/pbpstats/overrides/ before pbpstats runs.
COPY --from=backend /app/pbpstats_overrides ./pbpstats_overrides
# Alembic reads its config from `[tool.alembic]`, so migrations run from /app.
COPY --from=backend /app/pyproject.toml ./
# `api.app.STATIC_DIR`; the project is installed editable, so this is where the package looks.
COPY --from=frontend /frontend/dist ./src/closinglineup/api/static

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

# Fails the build, rather than serving `/api` alone, if the package stops resolving to /app/src.
RUN python -c "from closinglineup.api.app import STATIC_DIR; assert (STATIC_DIR / 'index.html').is_file(), STATIC_DIR"
# Likewise for the pbpstats overrides, which resolve relative to the package.
RUN python -c "from closinglineup.parse.pbp import ACTION_OVERRIDES, OVERRIDE_FILES, OVERRIDES_SOURCE as d; assert all((d / f).is_file() for f in (*OVERRIDE_FILES, ACTION_OVERRIDES)), d"

USER 999:999

VOLUME /data
EXPOSE 8000

# A DB round-trip, shared by the `app` and `worker` containers this image runs as.
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s CMD ["closinglineup", "health"]

CMD ["closinglineup", "api"]
