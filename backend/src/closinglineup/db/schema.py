"""Waiting for `app` to migrate. Only `app` runs `alembic upgrade head`, and Watchtower starts the
worker right after `app` without waiting for it to turn healthy, so the worker can start on a new
image before the schema it expects is there; it waits on this first."""

import logging
from datetime import timedelta
from functools import cache
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from closinglineup.clock import Clock

log = logging.getLogger(__name__)

# `[tool.alembic]` lives here; the image copies it to /app beside `src/`.
PYPROJECT = Path(__file__).parents[3] / "pyproject.toml"
POLL_SECONDS = 5.0


class SchemaNotAtHeadError(Exception):
    pass


@cache
def script_head() -> str:
    """The head revision of the migrations this code ships with."""
    head = ScriptDirectory.from_config(Config(toml_file=str(PYPROJECT))).get_current_head()
    if head is None:
        raise SchemaNotAtHeadError(f"no migrations found from {PYPROJECT}")
    return head


def _current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def wait_for_schema_head(engine: Engine, *, timeout: timedelta, clock: Clock) -> None:
    """Return once the database is at `script_head()`, polling every `POLL_SECONDS`; raise
    `SchemaNotAtHeadError` if it is not by `timeout`. An unreachable database counts as not yet."""
    head = script_head()
    deadline = clock.now() + timeout
    while True:
        try:
            current = _current_revision(engine)
        except OperationalError as exc:
            state = f"database unreachable: {exc.orig}"
        else:
            if current == head:
                log.info("schema at head %s", head)
                return
            state = f"schema at {current}"
        now = clock.now()
        if now >= deadline:
            raise SchemaNotAtHeadError(
                f"{state}, not head {head}, after {timeout.total_seconds():g} s"
            )
        log.info("%s; waiting for head %s", state, head)
        clock.sleep_until(min(now + timedelta(seconds=POLL_SECONDS), deadline))
