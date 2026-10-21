import os

import pytest


@pytest.fixture
def database_url() -> str:
    """A reachable Postgres: the compose `postgres` service locally, the service container in CI.

    Tests that take this fixture skip on a host with no `DATABASE_URL`, so a bare
    `uv run pytest` outside the container still runs everything else.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set; run inside `docker compose exec app`")
    return url
