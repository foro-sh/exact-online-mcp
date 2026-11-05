import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from .config import GRANT_NAME, Settings


class NotConnectedError(Exception):
    pass


class TokenRefreshError(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class TokenSet:
    refresh_token: str
    access_token: str
    expires_at: datetime


async def exchange_code(
    http: httpx.AsyncClient, base_url: str, client_id: str, client_secret: str, code: str, redirect_uri: str
) -> TokenSet:
    response = await http.post(
        f"{base_url}/api/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    if response.status_code != 200:
        raise TokenRefreshError(
            f"Exact rejected the authorisation code ({response.status_code}: {response.text[:200]})."
        )
    payload = response.json()
    # Exact sends expires_in as a string.
    return TokenSet(
        refresh_token=payload["refresh_token"],
        access_token=payload["access_token"],
        expires_at=utcnow() + timedelta(seconds=int(payload["expires_in"])),
    )


class BrokerTokens:
    """Access tokens from foro's upstream broker. foro holds the refresh token, which Exact rotates
    on every use, and refreshes it under a row lock, so no replica here ever sees or spends it."""

    def __init__(self, http: httpx.AsyncClient, settings: Settings, clock: Callable[[], datetime] = utcnow):
        self._http = http
        self._url = f"{settings.broker_url}/grants/{GRANT_NAME}"
        self._headers = {"Authorization": f"Bearer {settings.broker_token}"}
        self._clock = clock
        self._cached: tuple[str, datetime] | None = None
        self._lock = asyncio.Lock()

    def _usable(self) -> str | None:
        # The broker already hands out expiry early, so a token is good right up to expires_at.
        if self._cached and self._cached[1] > self._clock():
            return self._cached[0]
        return None

    async def access_token(self) -> str:
        if token := self._usable():
            return token
        async with self._lock:
            if token := self._usable():
                return token
            self._cached = await self._fetch()
            return self._cached[0]

    def invalidate(self) -> None:
        self._cached = None

    async def _fetch(self) -> tuple[str, datetime]:
        response = await self._http.get(self._url, headers=self._headers)
        if response.status_code == 404:
            raise NotConnectedError(
                "Exact Online is not connected yet: this foro project has no `exact` upstream grant. "
                "Run `uv run python -m exact_mcp.connect` and add the grant it prints (see the README)."
            )
        if response.status_code == 409:
            raise TokenRefreshError(
                "foro's Exact Online connection is dead: its refresh token was spent. Reconnect with "
                "`uv run python -m exact_mcp.connect` and reset the `exact` grant (see the README)."
            )
        if response.is_error:
            raise TokenRefreshError(
                f"foro could not get an Exact Online token ({response.status_code}); try again shortly."
            )
        body = response.json()
        return body["access_token"], datetime.fromisoformat(body["expires_at"])
