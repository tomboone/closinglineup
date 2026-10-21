# closinglineup

Private, single-user NBA rotation & value dashboard: per-player rotation state (Starter / Core / Fringe / Out / Injured / Two-way) per team per date, and the minutes, production and impact trends that explain changes in it. Fetch tier runs at home on `kumadrome` (residential IP required for stats.nba.com).

## Linear

- linear_initiative: closinglineup (I-13)
- linear_team: NEU
- linear_repo: closinglineup
- Project: P-NEU-101 "closinglineup v1: rotation & value dashboard"

## Toolchain

Backend (`backend/`): uv, ruff, pyright strict, pytest, Python 3.13. From `backend/`, `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest` before opening a PR; CI runs the same.

## Docs

- specs_dir: docs

Project spec: `docs/specs/closinglineup-v1-rotation-and-value-dashboard-project-spec.md`
Handoff (original brief): `docs/handoff/nba-rotation-dashboard-handoff.md`
