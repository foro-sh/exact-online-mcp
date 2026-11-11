import asyncio
import hashlib
from datetime import timedelta

import httpx
import pytest

from exact_mcp import tokens as tokens_module
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


async def test_token_without_expiry_is_cached_until_invalidated(exact, settings):
    exact.handler = lambda r: httpx.Response(200, json={"access_token": "access-1", "expires_at": None, "extra": {}})
    source = tokens(exact, settings, now=NOW + timedelta(days=3650))
    await source.access_token()
    await source.access_token()
    assert len(exact.requests) == 1

    source.invalidate("access-1")
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
    source.invalidate("access-1")
    await source.access_token()
    assert len(exact.requests) == 2
    assert exact.requests[1].headers["x-foro-rejected-token"] == hashlib.sha256(b"access-1").hexdigest()


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


async def test_without_a_handle_the_request_is_unchanged(exact, settings, caller):
    exact.handler = lambda r: broker_token()
    caller(None)
    await tokens(exact, settings).access_token()
    assert "x-foro-caller" not in exact.requests[0].headers
    assert "x-foro-rejected-token" not in exact.requests[0].headers


async def test_the_handle_is_forwarded_to_the_broker(exact, settings, caller):
    exact.handler = lambda r: broker_token()
    caller("alice", "h-1")
    await tokens(exact, settings).access_token()
    assert exact.requests[0].headers["x-foro-caller"] == "h-1"
    assert exact.requests[0].headers["Authorization"] == "Bearer project.capability"


async def test_each_caller_gets_their_own_token(exact, settings, caller):
    exact.handler = lambda r: broker_token(f"access-{r.headers['x-foro-caller']}")
    source = tokens(exact, settings)
    caller("alice", "a1")
    assert await source.access_token() == "access-a1"
    caller("bob", "b1")
    assert await source.access_token() == "access-b1"
    # A rotated handle for the same subject still hits the cache.
    caller("alice", "a2")
    assert await source.access_token() == "access-a1"
    assert len(exact.requests) == 2

    source.invalidate("access-a1")
    assert await source.access_token() == "access-a2"
    caller("bob", "b2")
    assert await source.access_token() == "access-b1"
    assert len(exact.requests) == 3


async def test_cache_holds_a_bounded_number_of_callers(exact, settings, caller, monkeypatch):
    monkeypatch.setattr(tokens_module, "MAX_CALLERS", 2)
    exact.handler = lambda r: broker_token()
    source = tokens(exact, settings)
    for subject in ("a", "b", "c"):
        caller(subject)
        await source.access_token()
    assert list(source._slots) == ["b", "c"]
    caller("a")
    await source.access_token()
    assert len(exact.requests) == 4


@pytest.mark.parametrize(
    ("status", "body", "error", "message"),
    [
        (401, {"error": "caller_required"}, TokenRefreshError, "could not tell who is calling; try again"),
        (
            404,
            {"error": "not_connected", "connect_url": "https://foro.sh/connect/x"},
            NotConnectedError,
            "Sign in to Exact Online as yourself at https://foro.sh/connect/x, then try again",
        ),
        (404, {"error": "not_found"}, NotConnectedError, "no `exact` upstream grant"),
        (
            409,
            {"error": "grant_failed", "connect_url": "https://foro.sh/connect/x"},
            TokenRefreshError,
            "Sign in to Exact Online again at https://foro.sh/connect/x",
        ),
        (409, {"error": "grant_failed"}, TokenRefreshError, "Reconnect the `exact` grant"),
        (401, {"error": "invalid_capability"}, TokenRefreshError, r"\(401\); try again shortly"),
    ],
)
async def test_per_user_broker_answers_say_what_to_do(exact, settings, status, body, error, message):
    exact.handler = lambda r: httpx.Response(status, json=body)
    with pytest.raises(error, match=message):
        await tokens(exact, settings).access_token()
