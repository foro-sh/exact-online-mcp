import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from exact_mcp import tools
from exact_mcp.app import create_server

from .conftest import odata

INVOICE_ID = "8f7b2c1e-0000-4000-8000-000000000001"


@pytest.fixture
def server(client):
    async def get_client():
        return client

    mcp = FastMCP("test")
    tools.register(mcp, get_client)
    return mcp


async def call(server, name, args=None):
    async with Client(server) as mcp:
        return (await mcp.call_tool(name, args or {})).structured_content


async def test_every_tool_is_read_only(server):
    async with Client(server) as mcp:
        listed = await mcp.list_tools()
    assert len(listed) == 11
    assert all(t.annotations.read_only_hint for t in listed)


async def test_search_accounts_filters_by_name_and_role(server, exact):
    exact.handler = lambda r: httpx.Response(200, json=odata([{"ID": "1", "Name": "Acme"}]))

    await call(server, "search_accounts", {"query": "O'Neil", "role": "supplier", "limit": 10})

    params = exact.requests[0].url.params
    assert exact.requests[0].url.path == "/api/v1/123/crm/Accounts"
    assert params["$filter"] == "substringof('O''Neil', Name) eq true and IsSupplier eq true"
    assert params["$top"] == "10"


async def test_open_receivables_totals_per_currency(server, exact):
    exact.handler = lambda r: httpx.Response(
        200,
        json=odata(
            [
                {"Amount": 100.10, "CurrencyCode": "EUR"},
                {"Amount": 50.05, "CurrencyCode": "EUR"},
                {"Amount": 20, "CurrencyCode": "USD"},
            ]
        ),
    )

    result = await call(server, "open_receivables", {"overdue_only": True})

    assert result["totals_by_currency"] == {"EUR": 150.15, "USD": 20}
    assert result["truncated"] is False
    assert exact.requests[0].url.path == "/api/v1/123/read/financial/ReceivablesList"
    assert exact.requests[0].url.params["$filter"].startswith("DueDate lt datetime'")


async def test_get_sales_invoice_adds_its_lines(server, exact):
    def handler(request):
        if request.url.path.endswith("SalesInvoiceLines"):
            return httpx.Response(200, json=odata([{"LineNumber": 1, "Description": "Widget"}]))
        return httpx.Response(200, json=odata([{"InvoiceID": INVOICE_ID, "InvoiceNumber": 20260001}]))

    exact.handler = handler

    result = await call(server, "get_sales_invoice", {"invoice_id": INVOICE_ID})

    assert result["InvoiceNumber"] == 20260001
    assert result["lines"] == [{"LineNumber": 1, "Description": "Widget"}]
    assert exact.requests[1].url.params["$filter"] == f"InvoiceID eq guid'{INVOICE_ID}'"


async def test_search_sales_invoices_maps_status_and_dates(server, exact):
    exact.handler = lambda r: httpx.Response(200, json=odata([]))

    await call(server, "search_sales_invoices", {"status": "open", "invoice_date_from": "2026-01-01"})

    assert exact.requests[0].url.params["$filter"] == "InvoiceDate ge datetime'2026-01-01' and Status eq 20"


async def test_profit_and_loss_sums_per_account_and_flips_the_sign(server, exact):
    exact.handler = lambda r: httpx.Response(
        200,
        json=odata(
            [
                {"GLAccountCode": "8000", "GLAccountDescription": "Revenue", "Amount": -1000.0},
                {"GLAccountCode": "8000", "GLAccountDescription": "Revenue", "Amount": -500.0},
                {"GLAccountCode": "4000", "GLAccountDescription": "Wages", "Amount": 600.0},
            ]
        ),
    )

    result = await call(server, "profit_and_loss", {"year": 2026, "period_from": 1, "period_to": 3})

    assert result["result"] == 900.0
    assert result["accounts"] == [
        {"code": "4000", "description": "Wages", "amount": 600.0},
        {"code": "8000", "description": "Revenue", "amount": -1500.0},
    ]
    assert "ReportingPeriod le 3" in exact.requests[0].url.params["$filter"]


async def test_bad_id_is_a_tool_error_not_an_injected_filter(server, exact):
    async with Client(server) as mcp:
        with pytest.raises(ToolError, match="not an Exact Online ID"):
            await mcp.call_tool("get_account", {"account_id": "x') or (1 eq 1"})
    assert exact.requests == []


async def test_unconfigured_server_answers_with_setup_instructions(monkeypatch):
    for name in ("FORO_UPSTREAM_BROKER_URL", "FORO_UPSTREAM_BROKER_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    async with Client(create_server()) as mcp:
        assert len(await mcp.list_tools()) == 11
        with pytest.raises(ToolError, match="foro's upstream broker.*FORO_UPSTREAM_BROKER_URL"):
            await mcp.call_tool("list_divisions", {})
