from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


def _build_engine() -> Engine:
    settings = get_settings()
    connect_args: dict[str, object] = {}
    if settings.database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(settings.database_url, future=True, connect_args=connect_args)


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


def _apply_schema_migrations() -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "tasks" not in table_names:
        return

    existing_columns = {column["name"] for column in inspector.get_columns("tasks")}
    alterations: list[str] = []

    if "task_name" not in existing_columns:
        alterations.append("ALTER TABLE tasks ADD COLUMN task_name VARCHAR(128)")
    if "time_ranges_json" not in existing_columns:
        alterations.append("ALTER TABLE tasks ADD COLUMN time_ranges_json TEXT")
    if "folder_id" not in existing_columns:
        alterations.append("ALTER TABLE tasks ADD COLUMN folder_id VARCHAR(32)")

    if not alterations:
        return

    with engine.begin() as conn:
        for ddl in alterations:
            conn.execute(text(ddl))


def init_db() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _apply_schema_migrations()


def get_db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
