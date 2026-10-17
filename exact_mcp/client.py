import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

import httpx

from . import odata
from .config import Settings
from .tokens import TokenManager

PAGE_SIZE = 60
# Waiting out the per-minute limit is fine inside a tool call; anything longer is the daily
# limit, which no caller should sit through.
MAX_MINUTELY_WAIT = 65.0


class ExactError(Exception):
    pass


class RateLimitedError(ExactError):
    pass


def _error_message(response: httpx.Response) -> str:
    try:
        return response.json()["error"]["message"]["value"]
    except (ValueError, KeyError, TypeError):
        return response.text[:300] or response.reason_phrase


class ExactClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        tokens: TokenManager,
        settings: Settings,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ):
        self._http = http
        self._tokens = tokens
        self._base_url = settings.base_url
        self._division = settings.division
        self._sleep = sleep
        self._clock = clock

    async def me(self) -> dict:
        data = await self._get(
            f"{self._base_url}/api/v1/current/Me",
            {"$select": "CurrentDivision,FullName,UserName"},
        )
        return odata.clean(data["d"]["results"][0])

    async def division(self, override: int | None = None) -> int:
        if override is not None:
            return override
        if self._division is None:
            self._division = (await self.me())["CurrentDivision"]
        return self._division

    async def list(
        self,
        resource: str,
        *,
        select: list[str],
        filters: Sequence[str] = (),
        orderby: str | None = None,
        limit: int,
        division: int | None = None,
    ) -> list[dict]:
        """Rows of `resource` (e.g. `crm/Accounts`), following Exact's `__next` links until
        `limit` rows are collected or the collection ends."""
        url = f"{self._base_url}/api/v1/{await self.division(division)}/{resource}"
        params: dict[str, str] = {"$select": ",".join(select)}
        if filters:
            params["$filter"] = " and ".join(filters)
        if orderby:
            params["$orderby"] = orderby
        if limit <= PAGE_SIZE:
            params["$top"] = str(limit)

        rows: list[dict] = []
        while url and len(rows) < limit:
            data = (await self._get(url, params))["d"]
            rows.extend(odata.clean(row) for row in data["results"])
            # __next carries the query (and a skiptoken) itself.
            url, params = data.get("__next"), None
        return rows[:limit]

    async def _get(self, url: str, params: dict[str, str] | None) -> dict:
        for attempt in range(2):
            response = await self._http.get(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {await self._tokens.access_token()}",
                    "Accept": "application/json",
                },
            )
            retry = attempt == 0
            if response.status_code == 401 and retry:
                # Another replica may have rotated the token since this one cached it.
                self._tokens.invalidate()
                continue
            if response.status_code == 429:
                wait = self._minutely_wait(response)
                if wait is not None and retry:
                    await self._sleep(wait)
                    continue
                raise RateLimitedError(self._rate_limit_message(response))
            if response.is_error:
                raise ExactError(f"Exact Online returned {response.status_code}: {_error_message(response)}")
            return response.json()
        raise ExactError(f"Exact Online returned {response.status_code}: {_error_message(response)}")

    def _minutely_wait(self, response: httpx.Response) -> float | None:
        if response.headers.get("X-RateLimit-Minutely-Remaining") != "0":
            return None
        reset_ms = response.headers.get("X-RateLimit-Minutely-Reset")
        if reset_ms is None:
            return None
        wait = max(int(reset_ms) / 1000 - self._clock(), 0) + 0.5
        return wait if wait <= MAX_MINUTELY_WAIT else None

    @staticmethod
    def _rate_limit_message(response: httpx.Response) -> str:
        reset_ms = response.headers.get("X-RateLimit-Reset")
        when = datetime.fromtimestamp(int(reset_ms) / 1000, UTC).isoformat(timespec="minutes") if reset_ms else "later"
        return f"Exact Online's API limit for this company is used up; it resets at {when}."
