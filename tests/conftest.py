from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from exact_mcp import tokens as tokens_module
from exact_mcp.client import ExactClient
from exact_mcp.config import load_settings
from exact_mcp.tokens import BrokerTokens

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)

BROKER = "http://worker:3005"

ENV = {
    "FORO_UPSTREAM_BROKER_URL": f"{BROKER}/",
    "FORO_UPSTREAM_BROKER_TOKEN": "project.capability",
}


def broker_token(access: str = "access-1", expires_at: datetime = NOW + timedelta(minutes=9)) -> httpx.Response:
    return httpx.Response(200, json={"access_token": access, "expires_at": expires_at.isoformat(), "extra": {}})


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
    tokens = BrokerTokens(http, settings, clock=lambda: NOW)
    tokens._slot(None).cached = ("access-1", NOW + timedelta(minutes=9))
    return ExactClient(http, tokens, settings, sleep=sleep, clock=lambda: NOW.timestamp())


@pytest.fixture
def caller(monkeypatch):
    """Sets the headers foro's gate added to the current request."""
    headers: dict[str, str] = {}
    monkeypatch.setattr(tokens_module, "get_http_headers", lambda: headers)

    def set_caller(subject: str | None, handle: str | None = None) -> None:
        headers.clear()
        if subject:
            headers.update({"x-foro-subject": subject, "x-foro-caller": handle or f"handle-{subject}"})

    return set_caller
