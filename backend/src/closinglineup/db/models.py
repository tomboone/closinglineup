"""ORM models. Alembic autogenerates migrations against `Base.metadata` (spec §6)."""

from datetime import date, datetime
from typing import Any, ClassVar

from sqlalchemy import BigInteger, CheckConstraint, DateTime, MetaData, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint names, so migrations can drop and alter what they created.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

_STATUS_CHECK = "status IN ('running', 'succeeded', 'failed')"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSONB,
        datetime: DateTime(timezone=True),
    }


class Timestamps:
    """`created_at` / `updated_at`, which every table carries (spec §6)."""

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class LoadLog(Timestamps, Base):
    """What ran for one (table, game date, step); `fetch --missing` and `load --missing` read it
    to self-heal skipped dates (spec §3.2)."""

    __tablename__ = "load_log"
    __table_args__ = (CheckConstraint(_STATUS_CHECK, name="status"),)

    table_name: Mapped[str] = mapped_column(String, primary_key=True)
    game_date: Mapped[date] = mapped_column(primary_key=True)
    step: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String)
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    detail: Mapped[dict[str, Any] | None]


class JobRun(Timestamps, Base):
    """One run of a scheduled job (spec §3.3)."""

    __tablename__ = "job_run"
    __table_args__ = (CheckConstraint(_STATUS_CHECK, name="status"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job: Mapped[str] = mapped_column(String, index=True)
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String)
    detail: Mapped[dict[str, Any] | None]
