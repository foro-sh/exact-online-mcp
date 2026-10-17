import asyncio
import os

import httpx
import psycopg
import pytest

from exact_mcp.tokens import NotConnectedError, PostgresTokenStore, TokenManager, TokenSet

from .conftest import NOW, fresh_tokens
from .test_tokens import expiring

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set (CI runs this against Postgres)")
KEY = b"k" * 32


@pytest.fixture
async def store():
    async with await psycopg.AsyncConnection.connect(URL, autocommit=True) as conn:
        await conn.execute("DROP TABLE IF EXISTS exact_online_tokens")
    store = PostgresTokenStore(URL, KEY, "nl:client-id")
    await store.init()
    await store.init()
    return store


async def test_put_then_locked_round_trips_and_writes_back_changes(store):
    await store.put(fresh_tokens())
    async with store.locked() as slot:
        assert slot.tokens == fresh_tokens()
        slot.tokens = TokenSet(refresh_token="rotated")
    async with store.locked() as slot:
        assert slot.tokens == TokenSet(refresh_token="rotated")


async def test_tokens_are_encrypted_at_rest(store):
    await store.put(fresh_tokens(refresh="very-secret-refresh"))
    async with await psycopg.AsyncConnection.connect(URL) as conn:
        raw = await (await conn.execute("SELECT tokens FROM exact_online_tokens")).fetchone()
    assert b"very-secret-refresh" not in bytes(raw[0])


async def test_an_exception_inside_the_lock_writes_nothing(store):
    await store.put(fresh_tokens())
    with pytest.raises(RuntimeError):
        async with store.locked() as slot:
            slot.tokens = TokenSet(refresh_token="lost")
            raise RuntimeError
    async with store.locked() as slot:
        assert slot.tokens == fresh_tokens()


async def test_lock_serialises_concurrent_holders(store):
    await store.put(fresh_tokens())
    order = []

    async def hold(name):
        async with store.locked():
            order.append(f"{name}-in")
            await asyncio.sleep(0.2)
            order.append(f"{name}-out")

    await asyncio.gather(hold("a"), hold("b"))
    assert order in (["a-in", "a-out", "b-in", "b-out"], ["b-in", "b-out", "a-in", "a-out"])


async def test_missing_row_says_how_to_connect(store):
    with pytest.raises(NotConnectedError, match="exact_mcp.connect"):
        async with store.locked():
            pass


async def test_two_replicas_refresh_an_expiring_token_once(store, exact, settings):
    await store.put(expiring())
    exact.handler = lambda r: httpx.Response(
        200, json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": "600"}
    )
    replicas = [TokenManager(store, exact.http(), settings, clock=lambda: NOW) for _ in range(2)]

    assert await asyncio.gather(*(r.access_token() for r in replicas)) == ["access-2", "access-2"]
    assert len(exact.requests) == 1
    async with store.locked() as slot:
        assert slot.tokens.refresh_token == "refresh-2"
