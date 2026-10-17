import asyncio
from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest

from exact_mcp.tokens import TokenManager, TokenRefreshError, TokenSet

from .conftest import NOW, MemoryTokenStore, fresh_tokens


def expiring(refresh: str = "refresh-1") -> TokenSet:
    return TokenSet(refresh_token=refresh, access_token="old-access", expires_at=NOW + timedelta(seconds=20))


def token_response(access="access-2", refresh="refresh-2"):
    body = {"access_token": access, "token_type": "bearer", "expires_in": "600"}
    if refresh:
        body["refresh_token"] = refresh
    return httpx.Response(200, json=body)


def manager(store, exact, settings, now=NOW):
    return TokenManager(store, exact.http(), settings, clock=lambda: now)


async def test_usable_token_is_used_without_refreshing(exact, settings):
    tokens = manager(MemoryTokenStore(fresh_tokens()), exact, settings)
    assert await tokens.access_token() == "access-1"
    assert exact.requests == []


async def test_expiring_token_is_refreshed_and_rotated_refresh_token_stored(exact, settings):
    store = MemoryTokenStore(expiring())
    exact.handler = lambda r: token_response()

    assert await manager(store, exact, settings).access_token() == "access-2"
    assert store.tokens.refresh_token == "refresh-2"
    assert store.tokens.expires_at == NOW + timedelta(seconds=600)

    form = parse_qs(exact.requests[0].content.decode())
    assert exact.requests[0].url == "https://start.exactonline.nl/api/oauth2/token"
    assert form["grant_type"] == ["refresh_token"]
    assert form["refresh_token"] == ["refresh-1"]
    assert form["client_id"] == ["client-id"]


async def test_refresh_response_without_refresh_token_keeps_the_old_one(exact, settings):
    store = MemoryTokenStore(expiring())
    exact.handler = lambda r: token_response(refresh=None)

    await manager(store, exact, settings).access_token()
    assert store.tokens.refresh_token == "refresh-1"


async def test_concurrent_callers_share_one_refresh(exact, settings):
    store = MemoryTokenStore(expiring())
    exact.handler = lambda r: token_response()
    tokens = manager(store, exact, settings)

    results = await asyncio.gather(*(tokens.access_token() for _ in range(5)))
    assert results == ["access-2"] * 5
    assert len(exact.requests) == 1


async def test_token_refreshed_by_another_replica_is_picked_up_without_refreshing(exact, settings):
    store = MemoryTokenStore(expiring())
    tokens = manager(store, exact, settings)
    tokens._cached = expiring()
    store.tokens = fresh_tokens(access="from-other-replica", refresh="refresh-9")

    assert await tokens.access_token() == "from-other-replica"
    assert exact.requests == []


async def test_rejected_refresh_raises_and_leaves_the_store_alone(exact, settings):
    store = MemoryTokenStore(expiring())
    exact.handler = lambda r: httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(TokenRefreshError, match="reconnect"):
        await manager(store, exact, settings).access_token()
    assert store.tokens == expiring()


def test_token_set_round_trips_through_json():
    tokens = fresh_tokens()
    assert TokenSet.from_json(tokens.to_json()) == tokens
    assert TokenSet.from_json(TokenSet(refresh_token="r").to_json()) == TokenSet(refresh_token="r")
