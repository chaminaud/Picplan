# SPDX-License-Identifier: LGPL-3.0-or-later
# This file is part of PicPlan. See COPYING.LESSER and COPYING for license details.

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.settings import settings


class Base(DeclarativeBase):
    pass


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


if settings.database_url.startswith("sqlite"):
    database_path = settings.database_url.removeprefix("sqlite:///")
    if database_path != ":memory:" and not database_path.startswith("/"):
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(connection, _connection_record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_db():
    with SessionLocal() as session:
        yield session
