import asyncio
from datetime import timedelta

import httpx
import pytest

from exact_mcp.tokens import BrokerTokens, NotConnectedError, TokenRefreshError

from .conftest import BROKER, NOW, broker_token


def tokens(exact, settings, now=NOW):
    return BrokerTokens(exact.http(), settings, clock=lambda: now)


async def test_token_is_fetched_from_the_exact_grant_with_the_capability(exact, settings):
    exact.handler = lambda r: broker_token()

    assert await tokens(exact, settings).access_token() == "access-1"
    request = exact.requests[0]
    assert str(request.url) == f"{BROKER}/grants/exact"
    assert request.headers["Authorization"] == "Bearer project.capability"


async def test_token_is_cached_until_its_expiry(exact, settings):
    exact.handler = lambda r: broker_token(expires_at=NOW + timedelta(minutes=5))
    source = tokens(exact, settings)
    await source.access_token()
    await source.access_token()
    assert len(exact.requests) == 1

    source._clock = lambda: NOW + timedelta(minutes=5)
    await source.access_token()
    assert len(exact.requests) == 2


async def test_concurrent_callers_share_one_fetch(exact, settings):
    exact.handler = lambda r: broker_token()
    source = tokens(exact, settings)

    assert await asyncio.gather(*(source.access_token() for _ in range(5))) == ["access-1"] * 5
    assert len(exact.requests) == 1


async def test_invalidate_fetches_again(exact, settings):
    exact.handler = lambda r: broker_token()
    source = tokens(exact, settings)
    await source.access_token()
    source.invalidate()
    await source.access_token()
    assert len(exact.requests) == 2


@pytest.mark.parametrize(
    ("status", "error", "message"),
    [
        (404, NotConnectedError, "no `exact` upstream grant"),
        (409, TokenRefreshError, "connection is dead"),
        (502, TokenRefreshError, "try again shortly"),
    ],
)
async def test_broker_failures_say_what_to_do(exact, settings, status, error, message):
    exact.handler = lambda r: httpx.Response(status, json={"error": "x"})
    with pytest.raises(error, match=message):
        await tokens(exact, settings).access_token()
