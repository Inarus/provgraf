"""Test fixtures — a connection inside a transaction that is rolled back (no leftovers in the DB)."""
import asyncpg
import pytest_asyncio

from provgraf.config import Settings


@pytest_asyncio.fixture
async def conn():
    s = Settings()
    c = await asyncpg.connect(s.database_url)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()
