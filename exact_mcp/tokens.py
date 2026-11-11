import asyncio
import hashlib
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from fastmcp.server.dependencies import get_http_headers

from .config import GRANT_NAME, Settings

NEVER = datetime.max.replace(tzinfo=UTC)
# A team is small; past this the least recently used caller's token is simply fetched again.
MAX_CALLERS = 256


class NotConnectedError(Exception):
    pass


class TokenRefreshError(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Slot:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cached: tuple[str, datetime] | None = None
    rejected: str | None = None


class BrokerTokens:
    """Access tokens from foro's upstream broker. foro holds the refresh token, which Exact rotates
    on every use, and refreshes it under a row lock, so no replica here ever sees or spends it.

    With a per-user grant, foro's gate adds `x-foro-caller` (a short-lived handle the broker
    resolves to the caller) and `x-foro-subject` to each request, and strips any a client sent.
    Tokens are then cached per subject, since the handle rotates; without a handle there is one
    shared token, as with a shared grant."""

    def __init__(self, http: httpx.AsyncClient, settings: Settings, clock: Callable[[], datetime] = utcnow):
        self._http = http
        self._url = f"{settings.broker_url}/grants/{GRANT_NAME}"
        self._exact = settings.base_url
        self._capability = settings.broker_token
        self._clock = clock
        self._slots: OrderedDict[str | None, _Slot] = OrderedDict()

    def _slot(self, key: str | None) -> _Slot:
        slot = self._slots.pop(key, None) or _Slot()
        self._slots[key] = slot
        if len(self._slots) > MAX_CALLERS:
            self._slots.popitem(last=False)
        return slot

    def _usable(self, slot: _Slot) -> str | None:
        # The broker already hands out expiry early, so a token is good right up to expires_at.
        if slot.cached and slot.cached[1] > self._clock():
            return slot.cached[0]
        return None

    async def access_token(self) -> str:
        handle, key = caller()
        slot = self._slot(key)
        if token := self._usable(slot):
            return token
        async with slot.lock:
            if token := self._usable(slot):
                return token
            slot.cached = await self._fetch(handle, slot.rejected)
            slot.rejected = None
            return slot.cached[0]

    def invalidate(self, token: str) -> None:
        """Drop the current caller's token after Exact refused it, and tell the broker so on the
        next fetch, or it would hand the same token back."""
        slot = self._slot(caller()[1])
        # Another request may have replaced it already.
        if slot.cached is None or slot.cached[0] == token:
            slot.cached = None
            slot.rejected = hashlib.sha256(token.encode()).hexdigest()

    async def _fetch(self, handle: str | None, rejected: str | None) -> tuple[str, datetime]:
        headers = {"Authorization": f"Bearer {self._capability}"}
        if handle:
            headers["x-foro-caller"] = handle
        if rejected:
            headers["x-foro-rejected-token"] = rejected
        response = await self._http.get(self._url, headers=headers)
        if response.is_error:
            raise self._error(response)
        body = response.json()
        # foro sends no expiry for a token that never lapses; it is kept until Exact refuses it.
        expires_at = datetime.fromisoformat(body["expires_at"]) if body.get("expires_at") else NEVER
        return body["access_token"], expires_at

    def _error(self, response: httpx.Response) -> Exception:
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        error, connect_url = body.get("error"), body.get("connect_url")
        if response.status_code == 401 and error == "caller_required":
            return TokenRefreshError("foro could not tell who is calling; try again.")
        if response.status_code == 404 and error == "not_connected" and connect_url:
            return NotConnectedError(
                f"You have not connected Exact Online yet. Sign in to Exact Online as yourself at {connect_url}, "
                "then try again."
            )
        if response.status_code == 404:
            return NotConnectedError(
                "Exact Online is not connected yet: this foro project has no `exact` upstream grant. "
                "On the project's Env tab, under Upstream grants, connect one named `exact` with "
                f"authorization URL {self._exact}/api/oauth2/auth, token endpoint {self._exact}/api/oauth2/token, "
                "your Exact app's client ID and EXACT_CLIENT_SECRET as the client secret."
            )
        if response.status_code == 409 and connect_url:
            return TokenRefreshError(
                f"Your Exact Online connection is dead. Sign in to Exact Online again at {connect_url}, then try again."
            )
        if response.status_code == 409:
            return TokenRefreshError(
                "foro's Exact Online connection is dead: its refresh token was spent. Reconnect the "
                "`exact` grant on the project's Env tab."
            )
        return TokenRefreshError(
            f"foro could not get an Exact Online token ({response.status_code}); try again shortly."
        )


def caller() -> tuple[str | None, str | None]:
    """The gate's handle for this request's caller, and the key its token is cached under."""
    headers = get_http_headers()
    handle = headers.get("x-foro-caller")
    if not handle:
        return None, None
    return handle, headers.get("x-foro-subject") or handle
