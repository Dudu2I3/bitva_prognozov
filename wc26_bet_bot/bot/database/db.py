from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

DB_PATH = Path(__file__).parent.parent.parent / "data" / "bot.db"


@asynccontextmanager
async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        yield db


async def fetchone(
    db: aiosqlite.Connection, sql: str, params: tuple = ()
) -> aiosqlite.Row | None:
    async with db.execute(sql, params) as cursor:
        return await cursor.fetchone()


async def fetchall(
    db: aiosqlite.Connection, sql: str, params: tuple = ()
) -> list[aiosqlite.Row]:
    async with db.execute(sql, params) as cursor:
        return await cursor.fetchall()


async def init_db() -> None:
    from bot.database.models import SCHEMA

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.executescript(SCHEMA)
        await db.commit()
