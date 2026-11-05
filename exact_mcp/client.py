import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

import httpx

from . import odata
from .config import Settings
from .tokens import BrokerTokens

PAGE_SIZE = 60
# Waiting out the per-minute limit is fine inside a tool call; the daily limit is not.
MAX_MINUTELY_WAIT = 61.0


class ExactError(Exception):
    pass


class RateLimitedError(ExactError):
    pass


def _error_message(response: httpx.Response) -> str:
    try:
        return response.json()["error"]["message"]["value"]
    except (ValueError, KeyError, TypeError):
        return response.text[:300] or response.reason_phrase


def _http_error(response: httpx.Response) -> ExactError:
    message = f"Exact Online returned {response.status_code}: {_error_message(response)}"
    if response.status_code == 403:
        # A valid token is still refused for parts of Exact the company's package, the app's scopes
        # or the user's rights don't cover, so say which endpoint it was.
        message += (
            f" ({response.url.path}). This division's Exact package, the app's scopes or the user's rights"
            " may not cover this part of Exact."
        )
    return ExactError(message)


class ExactClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        tokens: BrokerTokens,
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
        params: dict[str, str] | None = None,
    ) -> list[dict]:
        """Rows of `resource` (e.g. `crm/Accounts`), following Exact's `__next` links until
        `limit` rows are collected or the collection ends. `params` are extra query parameters,
        such as the `year` a `read/` function takes."""
        url = f"{self._base_url}/api/v1/{await self.division(division)}/{resource}"
        params = {**(params or {}), "$select": ",".join(select)}
        if filters:
            params["$filter"] = " and ".join(filters)
        if orderby:
            params["$orderby"] = orderby
        # Bulk endpoints page 1,000 rows, and their docs don't say they take $top.
        if limit <= PAGE_SIZE and not resource.startswith("bulk/"):
            params["$top"] = str(limit)

        rows: list[dict] = []
        while url and len(rows) < limit:
            data = (await self._get(url, params))["d"]
            # Collections come as d.results; some read/ functions reportedly answer with a bare
            # list or a single record.
            if isinstance(data, list):
                data = {"results": data}
            elif "results" not in data:
                data = {"results": [data]}
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
                # The cached token may have been revoked early; fetch it again once.
                self._tokens.invalidate()
                continue
            if response.status_code == 429:
                if response.headers.get("X-RateLimit-Remaining") == "0":
                    raise RateLimitedError(self._daily_limit_message(response))
                if retry:
                    await self._sleep(self._minutely_wait(response))
                    continue
                raise RateLimitedError("Exact Online's per-minute API limit is still used up; try again in a minute.")
            if response.is_error:
                raise _http_error(response)
            return response.json()
        raise _http_error(response)

    def _minutely_wait(self, response: httpx.Response) -> float:
        # Exact's docs don't name the reset header, so without it wait out a whole window.
        reset_ms = response.headers.get("X-RateLimit-Minutely-Reset")
        if reset_ms is None:
            return MAX_MINUTELY_WAIT
        return min(max(int(reset_ms) / 1000 - self._clock(), 0) + 0.5, MAX_MINUTELY_WAIT)

    @staticmethod
    def _daily_limit_message(response: httpx.Response) -> str:
        reset_ms = response.headers.get("X-RateLimit-Reset")
        when = (
            datetime.fromtimestamp(int(reset_ms) / 1000, UTC).isoformat(timespec="minutes") if reset_ms else "midnight"
        )
        return f"Exact Online's daily API limit for this company is used up; it resets at {when}."
