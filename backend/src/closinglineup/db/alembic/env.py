"""Alembic environment: `DATABASE_URL` comes from `Settings`, never from the Alembic config."""

from alembic import context

from closinglineup.db.models import Base
from closinglineup.db.session import make_engine
from closinglineup.logging import configure_logging
from closinglineup.settings import Settings

settings = Settings()
configure_logging(settings.log_level)


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    with make_engine(settings).connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
