import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from .config import GRANT_NAME, Settings


class NotConnectedError(Exception):
    pass


class TokenRefreshError(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


class BrokerTokens:
    """Access tokens from foro's upstream broker. foro holds the refresh token, which Exact rotates
    on every use, and refreshes it under a row lock, so no replica here ever sees or spends it."""

    def __init__(self, http: httpx.AsyncClient, settings: Settings, clock: Callable[[], datetime] = utcnow):
        self._http = http
        self._url = f"{settings.broker_url}/grants/{GRANT_NAME}"
        self._exact = settings.base_url
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
                "On the project's Env tab, under Upstream grants, connect one named `exact` with "
                f"authorization URL {self._exact}/api/oauth2/auth, token endpoint {self._exact}/api/oauth2/token, "
                "your Exact app's client ID and EXACT_CLIENT_SECRET as the client secret."
            )
        if response.status_code == 409:
            raise TokenRefreshError(
                "foro's Exact Online connection is dead: its refresh token was spent. Reconnect the "
                "`exact` grant on the project's Env tab."
            )
        if response.is_error:
            raise TokenRefreshError(
                f"foro could not get an Exact Online token ({response.status_code}); try again shortly."
            )
        body = response.json()
        return body["access_token"], datetime.fromisoformat(body["expires_at"])
