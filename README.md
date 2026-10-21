# closinglineup

A private, single-user NBA rotation and value dashboard. It tracks each player's **rotation tier** on his team on every game date (Injured, Starter, Core, Fringe, Out) and the signals that move it: minutes share and volatility first, then production, on-court impact and public impact metrics over 5/10/20-game, season and prior-season windows.

It runs entirely on a home server and is reached over a private tailnet. Nothing here is hosted publicly.

## Status

Planning complete; implementation has not started. See:

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

## License

MIT. See `LICENSE.md`.
