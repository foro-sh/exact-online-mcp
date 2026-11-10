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


def _json_message(response: httpx.Response) -> str | None:
    try:
        return response.json()["error"]["message"]["value"]
    except (ValueError, KeyError, TypeError):
        return None


def _http_error(response: httpx.Response) -> ExactError:
    reason = _json_message(response)
    if response.status_code == 500 and reason:
        # Exact answers a record it won't accept with a 500 and says why, so it's no outage.
        return ExactError(f"Exact Online refused it: {reason}")
    text = reason or response.text[:300] or response.reason_phrase
    parts = response.url.path.split("/")
    if response.status_code == 403 and "WrongDivision" in text and parts[1:3] == ["api", "v1"]:
        return ExactError(f"This connection can't use division {parts[3]}; see list_divisions for the ones it can.")
    message = f"Exact Online returned {response.status_code}: {text}"
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
        response = await self._get(
            f"{self._base_url}/api/v1/current/Me",
            {"$select": "CurrentDivision,FullName,UserName"},
        )
        return odata.clean(response.json()["d"]["results"][0])

    async def metadata(self, service: str, division: int | None = None) -> str:
        """The `$metadata` document (EDMX) of a service such as `crm`, which describes its
        resources and their properties."""
        url = f"{self._base_url}/api/v1/{await self.division(division)}/{service}/$metadata"
        return (await self._get(url, None, accept="application/xml")).text

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
            data = (await self._get(url, params)).json()["d"]
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

    async def download(self, url: str) -> bytes:
        """The file behind an Exact URL, such as a document attachment's `Url`. Only Exact's own
        host gets the access token."""
        target, base = httpx.URL(url), httpx.URL(self._base_url)
        if (target.scheme, target.host) != (base.scheme, base.host):
            raise ExactError(f"Not downloading {url}: it isn't on {self._base_url}.")
        return (await self._get(url, None, accept="*/*")).content

    async def create(self, resource: str, body: dict, division: int | None = None) -> dict:
        """POST a new record to `resource`, such as `purchaseentry/PurchaseEntries` with its lines,
        and return the record Exact made."""
        url = f"{self._base_url}/api/v1/{await self.division(division)}/{resource}"
        response = await self._write(url, json=body)
        return odata.clean(response.json()["d"])

    async def upload_xml(self, topic: str, xml: str, division: int | None = None) -> str:
        """POST an eExact document to the XML API, for what REST can't do, such as `MatchSets`.
        Exact answers 200 either way, with a Message per record saying how it went."""
        url = f"{self._base_url}/docs/XMLUpload.aspx"
        params = {"Topic": topic, "_Division_": str(await self.division(division))}
        response = await self._write(url, params=params, content=xml.encode(), content_type="application/xml")
        return response.text

    async def _write(self, url: str, **kwargs) -> httpx.Response:
        try:
            return await self._send("POST", url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as err:
            raise ExactError("Couldn't reach Exact Online, so nothing was sent; try again.") from err
        except httpx.TransportError as err:
            # The request may have reached Exact, which may have booked it, and a retry would book it twice.
            raise ExactError(
                "Exact Online didn't answer in time, so this may or may not have been booked. Look it up"
                " before trying again."
            ) from err

    async def _get(self, url: str, params: dict[str, str] | None, accept: str = "application/json") -> httpx.Response:
        return await self._send("GET", url, params=params, accept=accept)

    async def _send(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json: dict | None = None,
        content: bytes | None = None,
        content_type: str | None = None,
        accept: str = "application/json",
    ) -> httpx.Response:
        # Retrying is safe for writes too: Exact refused the request, so nothing was booked.
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {await self._tokens.access_token()}", "Accept": accept}
            if content_type:
                headers["Content-Type"] = content_type
            response = await self._http.request(method, url, params=params, json=json, content=content, headers=headers)
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
            return response
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
