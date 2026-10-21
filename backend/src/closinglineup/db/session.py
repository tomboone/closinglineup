"""The SQLAlchemy engine and session factory, built from `DATABASE_URL` (spec §2, §10)."""

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.settings import Settings

# Bounds how long a command waits on a Postgres that is down rather than hanging on it.
CONNECT_TIMEOUT_SECONDS = 5


def make_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def ping(engine: Engine) -> None:
    """Round-trip `SELECT 1`; raises `sqlalchemy.exc.OperationalError` when Postgres is down."""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
