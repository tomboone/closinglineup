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
with `backend/` bind-mounted, and every tool runs inside `app`.

```bash
docker compose up -d
docker compose exec -T app uv run alembic upgrade head
docker compose exec -T app uv run closinglineup health
docker compose exec -T app uv run ruff check . && docker compose exec -T app uv run ruff format --check . \
  && docker compose exec -T app uv run pyright && docker compose exec -T app uv run pytest
```

New migrations: `docker compose exec -T app uv run alembic revision --autogenerate -m "…"`.

Pre-commit hooks (ruff, pyright) also run inside the container, so `app` must be up to commit:

```bash
pre-commit install
```

## License

MIT. See `LICENSE.md`.
