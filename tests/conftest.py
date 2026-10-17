import asyncio
import base64
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from exact_mcp.client import ExactClient
from exact_mcp.config import load_settings
from exact_mcp.tokens import TokenManager, TokenSet, TokenSlot

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)

ENV = {
    "EXACT_CLIENT_ID": "client-id",
    "EXACT_CLIENT_SECRET": "client-secret",
    "TOKEN_STORE_URL": "postgresql://unused",
    "TOKEN_STORE_KEY": base64.b64encode(b"k" * 32).decode(),
}


class MemoryTokenStore:
    def __init__(self, tokens: TokenSet):
        self.tokens = tokens
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def locked(self) -> AsyncIterator[TokenSlot]:
        async with self._lock:
            slot = TokenSlot(self.tokens)
            yield slot
            if slot.changed:
                self.tokens = slot.tokens

    async def put(self, tokens: TokenSet) -> None:
        self.tokens = tokens


def fresh_tokens(access: str = "access-1", refresh: str = "refresh-1") -> TokenSet:
    return TokenSet(refresh_token=refresh, access_token=access, expires_at=NOW + timedelta(minutes=9))


@pytest.fixture
def settings():
    return load_settings({**ENV, "EXACT_DIVISION": "123"})


def odata(rows: list[dict], next_url: str | None = None) -> dict:
    return {"d": {"results": rows, **({"__next": next_url} if next_url else {})}}


class FakeExact:
    """Records requests and answers them with the handler a test sets."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.handler: Callable[[httpx.Request], httpx.Response] = lambda r: httpx.Response(404)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


@pytest.fixture
def exact():
    return FakeExact()


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def client(exact, settings, sleeps):
    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    http = exact.http()
    tokens = TokenManager(MemoryTokenStore(fresh_tokens()), http, settings, clock=lambda: NOW)
    return ExactClient(http, tokens, settings, sleep=sleep, clock=lambda: NOW.timestamp())
