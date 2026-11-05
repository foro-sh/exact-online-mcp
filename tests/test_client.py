import httpx
import pytest

from exact_mcp.client import ExactError, RateLimitedError

from .conftest import BROKER, NOW, broker_token, odata

BASE = "https://start.exactonline.nl/api/v1"


async def test_list_follows_next_links_until_the_limit(client, exact):
    pages = {
        None: odata([{"Name": f"a{i}"} for i in range(60)], f"{BASE}/123/crm/Accounts?$skiptoken=x"),
        "x": odata([{"Name": f"b{i}"} for i in range(60)], f"{BASE}/123/crm/Accounts?$skiptoken=y"),
    }
    exact.handler = lambda r: httpx.Response(200, json=pages[r.url.params.get("$skiptoken")])

    rows = await client.list("crm/Accounts", select=["Name"], limit=100)

    assert len(rows) == 100
    assert len(exact.requests) == 2
    assert "%24top" not in str(exact.requests[0].url)
    assert exact.requests[1].url.params.get("$select") is None


async def test_small_limit_uses_top_and_sends_auth_and_json_headers(client, exact):
    exact.handler = lambda r: httpx.Response(200, json=odata([{"Name": "  Acme  "}]))

    rows = await client.list("crm/Accounts", select=["ID", "Name"], filters=["a", "b"], orderby="Name", limit=5)

    request = exact.requests[0]
    assert rows == [{"Name": "Acme"}]
    assert request.url.params["$top"] == "5"
    assert request.url.params["$filter"] == "a and b"
    assert request.url.params["$select"] == "ID,Name"
    assert request.headers["Authorization"] == "Bearer access-1"
    assert request.headers["Accept"] == "application/json"


async def test_explicit_division_overrides_the_default(client, exact):
    exact.handler = lambda r: httpx.Response(200, json=odata([]))
    await client.list("crm/Accounts", select=["ID"], limit=1, division=999)
    assert exact.requests[0].url.path == "/api/v1/999/crm/Accounts"


async def test_default_division_comes_from_me_once(client, exact):
    client._division = None

    def handler(request):
        if request.url.path.endswith("/current/Me"):
            return httpx.Response(200, json=odata([{"CurrentDivision": 42, "FullName": "D", "UserName": "d"}]))
        return httpx.Response(200, json=odata([]))

    exact.handler = handler
    await client.list("crm/Accounts", select=["ID"], limit=1)
    await client.list("crm/Accounts", select=["ID"], limit=1)

    paths = [r.url.path for r in exact.requests]
    assert paths == ["/api/v1/current/Me", "/api/v1/42/crm/Accounts", "/api/v1/42/crm/Accounts"]


async def test_minutely_rate_limit_is_waited_out_once(client, exact, sleeps):
    responses = iter(
        [
            httpx.Response(
                429,
                headers={
                    "X-RateLimit-Minutely-Remaining": "0",
                    "X-RateLimit-Minutely-Reset": str(int((NOW.timestamp() + 12) * 1000)),
                },
            ),
            httpx.Response(200, json=odata([{"ID": "x"}])),
        ]
    )
    exact.handler = lambda r: next(responses)

    assert await client.list("crm/Accounts", select=["ID"], limit=1) == [{"ID": "x"}]
    assert sleeps == [12.5]


async def test_daily_rate_limit_raises_with_the_reset_time(client, exact, sleeps):
    reset = int((NOW.timestamp() + 3600) * 1000)
    exact.handler = lambda r: httpx.Response(
        429, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)}
    )

    with pytest.raises(RateLimitedError, match="daily.*2026-10-17T13:00"):
        await client.list("crm/Accounts", select=["ID"], limit=1)
    assert sleeps == []


async def test_minutely_limit_without_a_reset_header_waits_a_whole_window(client, exact, sleeps):
    responses = iter(
        [
            httpx.Response(429, headers={"X-RateLimit-Minutely-Remaining": "0", "X-RateLimit-Remaining": "4000"}),
            httpx.Response(200, json=odata([{"ID": "x"}])),
        ]
    )
    exact.handler = lambda r: next(responses)

    assert await client.list("crm/Accounts", select=["ID"], limit=1) == [{"ID": "x"}]
    assert sleeps == [61.0]


async def test_minutely_limit_still_hit_after_waiting_says_so(client, exact, sleeps):
    exact.handler = lambda r: httpx.Response(429, headers={"X-RateLimit-Remaining": "4000"})

    with pytest.raises(RateLimitedError, match="per-minute"):
        await client.list("crm/Accounts", select=["ID"], limit=1)
    assert sleeps == [61.0]


async def test_unauthorised_reloads_the_token_and_retries_once(client, exact):
    responses = iter([httpx.Response(401), broker_token("access-2"), httpx.Response(200, json=odata([]))])
    exact.handler = lambda r: next(responses)

    assert await client.list("crm/Accounts", select=["ID"], limit=1) == []
    assert str(exact.requests[1].url) == f"{BROKER}/grants/exact"
    assert exact.requests[2].headers["Authorization"] == "Bearer access-2"


async def test_persistent_unauthorised_raises(client, exact):
    exact.handler = lambda r: broker_token() if r.url.host == "worker" else httpx.Response(401, text="Unauthorized")
    with pytest.raises(ExactError, match="401"):
        await client.list("crm/Accounts", select=["ID"], limit=1)


async def test_exact_error_message_is_surfaced(client, exact):
    exact.handler = lambda r: httpx.Response(
        400, json={"error": {"code": "", "message": {"lang": "", "value": "Invalid field: Nope"}}}
    )
    with pytest.raises(ExactError, match="Invalid field: Nope"):
        await client.list("crm/Accounts", select=["Nope"], limit=1)


async def test_forbidden_names_the_endpoint_and_the_likely_causes(client, exact):
    exact.handler = lambda r: httpx.Response(403, text="Forbidden")

    with pytest.raises(ExactError, match=r"403: Forbidden \(/api/v1/123/purchaseentry/PurchaseEntries\)\. .*package"):
        await client.list("purchaseentry/PurchaseEntries", select=["EntryID"], limit=1)


async def test_bulk_endpoints_never_get_top(client, exact):
    exact.handler = lambda r: httpx.Response(200, json=odata([{"ID": "x"}]))

    await client.list("bulk/Financial/GLAccounts", select=["ID"], limit=5)

    assert "$top" not in exact.requests[0].url.params


async def test_params_go_with_the_query(client, exact):
    exact.handler = lambda r: httpx.Response(200, json=odata([]))

    await client.list("read/financial/RevenueListByYear", select=["Period"], params={"year": "2026"}, limit=100)

    params = exact.requests[0].url.params
    assert params["year"] == "2026"
    assert params["$select"] == "Period"


async def test_a_bare_list_or_a_single_record_reads_as_rows(client, exact):
    responses = iter([httpx.Response(200, json={"d": [{"ID": "a"}]}), httpx.Response(200, json={"d": {"ID": "b"}})])
    exact.handler = lambda r: next(responses)

    assert await client.list("read/financial/AgingOverview", select=["ID"], limit=10) == [{"ID": "a"}]
    assert await client.list("read/financial/ProfitLossOverview", select=["ID"], limit=1) == [{"ID": "b"}]


async def test_metadata_asks_for_the_services_edmx(client, exact):
    exact.handler = lambda r: httpx.Response(200, text="<edmx:Edmx/>")

    assert await client.metadata("read/financial") == "<edmx:Edmx/>"
    assert exact.requests[0].url.path == "/api/v1/123/read/financial/$metadata"
    assert exact.requests[0].headers["Accept"] == "application/xml"
