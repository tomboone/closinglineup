# closinglineup

A private, single-user NBA rotation and value dashboard. It tracks each player's **rotation tier** on his team on every game date (Injured, Starter, Core, Fringe, Out) and the signals that move it: minutes share and volatility first, then production, on-court impact and public impact metrics over 5/10/20-game, season and prior-season windows.

It runs entirely on a home server and is reached over a private tailnet. Nothing here is hosted publicly.

## Status

Implementation is under way, starting with the backend scaffold. See:

- `docs/specs/closinglineup-v1-rotation-and-value-dashboard-project-spec.md` — the project-wide spec every ticket is built from
- `CONTEXT.md` — the glossary
- `docs/adr/` — decisions and why
- `docs/handoff/` — the original brief

## Layout (planned)

```
backend/    Python 3.13, FastAPI, SQLAlchemy, Alembic, APScheduler
frontend/   React, Vite, TypeScript, ECharts
deploy/     compose file and runbook for the home server
```

## Backend development

Local development runs in Docker: `docker-compose.yml` starts Postgres 17 and an `app` container
with `backend/` bind-mounted, running the API server with reload on port 8000. Every tool runs
inside `app`.

```bash
docker compose up -d
docker compose exec -T app uv run alembic upgrade head
docker compose exec -T app uv run closinglineup health
docker compose exec -T app uv run ruff check . && docker compose exec -T app uv run ruff format --check . \
  && docker compose exec -T app uv run pyright && docker compose exec -T app uv run pytest
```

New migrations: `docker compose exec -T app uv run alembic revision --autogenerate -m "…"`.

Pre-commit hooks (ruff, pyright, oxlint) also run inside the containers, so `app` and `frontend`
must be up to commit:

```bash
pre-commit install
```

## Frontend development

The `frontend` container runs the Vite dev server on http://localhost:5173 with `frontend/`
bind-mounted, and proxies `/api` to `app`.

```bash
docker compose exec -T frontend pnpm lint && docker compose exec -T frontend pnpm tsc -b \
  && docker compose exec -T frontend pnpm test
docker compose exec -T frontend pnpm build   # → frontend/dist/
```

## Container image

The root `Dockerfile` builds the frontend bundle and the backend into one image that runs as
UID/GID 999. `closinglineup api` serves `/api` and the bundle on port 8000; the `HEALTHCHECK`
runs `closinglineup health`.

```bash
docker build -t closinglineup .
docker run --rm -p 127.0.0.1:8000:8000 -e DATABASE_URL=postgresql+psycopg://… closinglineup
```

Every push to `main` publishes `ghcr.io/tomboone/closinglineup:latest` and `:sha-<7>`
(`.github/workflows/image.yml`).

## Deployment

The home server runs `deploy/docker-compose.yml`: Postgres, the app, the worker, a Tailscale sidecar
that serves the app on the personal tailnet, and a scoped Watchtower that pulls each new image. A
systemd timer on the host dumps the database nightly to B2 (`deploy/backup/`). `deploy/README.md` is
the runbook.

## License

MIT. See `LICENSE.md`.
