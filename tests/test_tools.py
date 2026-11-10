from datetime import date

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from exact_mcp import tools
from exact_mcp.app import create_server

from .conftest import odata

INVOICE_ID = "8f7b2c1e-0000-4000-8000-000000000001"
ACCOUNT_ID = "8f7b2c1e-0000-4000-8000-000000000002"
ENTRY_ID = "8f7b2c1e-0000-4000-8000-000000000003"
GL_ACCOUNT_ID = "8f7b2c1e-0000-4000-8000-000000000004"


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
    assert len(listed) == 28
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


async def test_search_sales_entries_filters_by_customer_and_invoice_date(server, exact):
    exact.handler = lambda r: httpx.Response(200, json=odata([]))

    await call(server, "search_sales_entries", {"account_id": ACCOUNT_ID, "invoice_date_from": "2026-01-01"})

    params = exact.requests[0].url.params
    assert exact.requests[0].url.path == "/api/v1/123/salesentry/SalesEntries"
    assert params["$filter"] == f"EntryDate ge datetime'2026-01-01' and Customer eq guid'{ACCOUNT_ID}'"
    assert params["$select"].startswith("Customer,CustomerName,EntryID,")


async def test_get_purchase_invoice_adds_its_lines(server, exact):
    def handler(request):
        if request.url.path.endswith("PurchaseEntryLines"):
            return httpx.Response(200, json=odata([{"LineNumber": 1, "GLAccountCode": "4000"}]))
        return httpx.Response(200, json=odata([{"EntryID": ENTRY_ID, "SupplierName": "Acme"}]))

    exact.handler = handler

    result = await call(server, "get_purchase_invoice", {"entry_id": ENTRY_ID})

    assert result["SupplierName"] == "Acme"
    assert result["lines"] == [{"LineNumber": 1, "GLAccountCode": "4000"}]
    assert exact.requests[1].url.params["$filter"] == f"EntryID eq guid'{ENTRY_ID}'"


async def test_search_gl_accounts_matches_code_or_description_in_the_server(server, exact):
    exact.handler = lambda r: httpx.Response(
        200,
        json=odata(
            [
                {"ID": GL_ACCOUNT_ID, "Code": "8000", "Description": "Revenue"},
                {"ID": GL_ACCOUNT_ID, "Code": "1100", "Description": "Bank"},
                {"ID": GL_ACCOUNT_ID, "Code": "4000", "Description": "Wages"},
            ]
        ),
    )

    by_description = (await call(server, "search_gl_accounts", {"query": "BANK"}))["result"]
    by_code = (await call(server, "search_gl_accounts", {"query": "000"}))["result"]

    assert [a["Code"] for a in by_description] == ["1100"]
    assert [a["Code"] for a in by_code] == ["4000", "8000"]
    assert exact.requests[0].url.path == "/api/v1/123/bulk/Financial/GLAccounts"
    assert "$filter" not in exact.requests[0].url.params


async def test_search_transactions_totals_every_line_and_filters_on_the_gl_account_id(server, exact):
    def handler(request):
        if request.url.path.endswith("GLAccounts"):
            return httpx.Response(200, json=odata([{"ID": GL_ACCOUNT_ID, "Code": "4000", "Description": "Wages"}]))
        return httpx.Response(
            200,
            json=odata(
                [
                    {"Date": "2026-01-15", "AmountDC": 100.0},
                    {"Date": "2026-03-15", "AmountDC": -20.0},
                    {"Date": "2026-02-15", "AmountDC": 50.5},
                ]
            ),
        )

    exact.handler = handler

    result = await call(
        server, "search_transactions", {"gl_account_code": " 4000 ", "date_from": "2026-01-01", "limit": 2}
    )

    assert result["total_amount_dc"] == 130.5
    assert result["line_count"] == 3
    assert result["complete"] is True
    assert [line["Date"] for line in result["lines"]] == ["2026-03-15", "2026-02-15"]
    lines_request = exact.requests[1]
    assert lines_request.url.path == "/api/v1/123/bulk/Financial/TransactionLines"
    assert lines_request.url.params["$filter"] == f"Date ge datetime'2026-01-01' and GLAccount eq guid'{GL_ACCOUNT_ID}'"


async def test_search_transactions_with_an_unknown_gl_code_says_so(server, exact):
    exact.handler = lambda r: httpx.Response(200, json=odata([{"ID": GL_ACCOUNT_ID, "Code": "4000"}]))

    async with Client(server) as mcp:
        with pytest.raises(ToolError, match="No GL account 9999"):
            await mcp.call_tool("search_transactions", {"gl_account_code": "9999"})
    assert len(exact.requests) == 1


async def test_financial_overview_reads_all_three_overviews(server, exact):
    rows = {
        "ProfitLossOverview": [{"CurrentYear": 2026, "RevenueCurrentYear": 1000.0}],
        "OutstandingInvoicesOverview": [{"CurrencyCode": "EUR", "OverdueReceivableInvoiceCount": 2}],
        "AgingOverview": [{"AgeGroup": 1, "AmountReceivable": 500.0}],
    }
    exact.handler = lambda r: httpx.Response(200, json=odata(rows[r.url.path.rsplit("/", 1)[1]]))

    result = await call(server, "financial_overview")

    assert result == {
        "profit_and_loss": {"CurrentYear": 2026, "RevenueCurrentYear": 1000.0},
        "outstanding_invoices": [{"CurrencyCode": "EUR", "OverdueReceivableInvoiceCount": 2}],
        "aging": [{"AgeGroup": 1, "AmountReceivable": 500.0}],
    }
    assert "RevenuePreviousYearPeriod" in exact.requests[0].url.params["$select"]


async def test_revenue_by_period_passes_the_year_and_totals_the_periods(server, exact):
    exact.handler = lambda r: httpx.Response(
        200, json=odata([{"Period": 2, "Amount": 200.004}, {"Period": 1, "Amount": 100.0}])
    )

    result = await call(server, "revenue_by_period", {"year": 2026})

    assert result == {
        "year": 2026,
        "total": 300.0,
        "periods": [{"period": 1, "amount": 100.0}, {"period": 2, "amount": 200.0}],
    }
    assert exact.requests[0].url.params["year"] == "2026"


async def test_tax_returns_name_their_type_and_status(server, exact):
    exact.handler = lambda r: httpx.Response(200, json=odata([{"Type": 31, "Status": 20}, {"Type": 999, "Status": 50}]))

    rows = (await call(server, "tax_returns", {"year": 2026}))["result"]

    labels = [(row["TypeDescription"], row["StatusDescription"]) for row in rows]
    assert labels == [("VAT", "Open"), ("999", "Processed")]
    assert exact.requests[0].url.params["$filter"] == "Year eq 2026"


def periods_around_today() -> list[dict]:
    year = date.today().year
    return [
        {"FinYear": year - 1, "FinPeriod": 12, "StartDate": f"{year - 1}-12-01", "EndDate": f"{year - 1}-12-31"},
        {"FinYear": year, "FinPeriod": 3, "StartDate": f"{year}-01-01", "EndDate": f"{year}-12-31"},
    ]


def by_resource(rows: dict[str, list[dict]]):
    """A handler answering each request with the rows for the last part of its path."""
    return lambda request: httpx.Response(200, json=odata(rows[request.url.path.rsplit("/", 1)[1]]))


async def test_balance_sheet_adds_this_years_movements_to_the_opening_balance(server, exact):
    exact.handler = by_resource(
        {
            "FinancialPeriods": periods_around_today(),
            "AfterEntry": [
                {"GLAccountCode": "1100", "GLAccountDescription": "Bank", "Amount": 1000.0},
                {"GLAccountCode": "0500", "GLAccountDescription": "Equity", "Amount": -1000.0},
            ],
            "ReportingBalance": [
                {"GLAccountCode": "1100", "GLAccountDescription": "Bank", "Amount": 250.0},
                {"GLAccountCode": "1300", "GLAccountDescription": "Debtors", "Amount": 100.0},
            ],
        }
    )

    result = await call(server, "balance_sheet")

    assert (result["year"], result["period"], result["total"]) == (date.today().year, 3, 350.0)
    assert result["accounts"] == [
        {"code": "0500", "description": "Equity", "opening": -1000.0, "movements": 0.0, "closing": -1000.0},
        {"code": "1100", "description": "Bank", "opening": 1000.0, "movements": 250.0, "closing": 1250.0},
        {"code": "1300", "description": "Debtors", "opening": 0.0, "movements": 100.0, "closing": 100.0},
    ]
    assert exact.requests[1].url.path.endswith("/openingbalance/CurrentYear/AfterEntry")
    movements = exact.requests[2].url.params["$filter"]
    assert "ReportingPeriod le 3 and BalanceType eq 'B'" in movements
    assert movements.endswith("Type ne 10")


async def test_balance_sheet_refuses_a_year_without_an_opening_balance(server, exact):
    exact.handler = by_resource({"FinancialPeriods": periods_around_today()})

    async with Client(server) as mcp:
        with pytest.raises(ToolError, match="only has opening balances"):
            await mcp.call_tool("balance_sheet", {"year": date.today().year - 5})


async def test_cash_position_reads_only_cash_and_bank_accounts(server, exact):
    exact.handler = by_resource(
        {
            "GLAccounts": [
                {"ID": GL_ACCOUNT_ID, "Code": "1100", "Description": "Bank", "Type": 12},
                {"ID": ENTRY_ID, "Code": "8000", "Description": "Revenue", "Type": 110},
            ],
            "FinancialPeriods": periods_around_today(),
            "AfterEntry": [{"GLAccountCode": "1100", "GLAccountDescription": "Bank", "Amount": 500.0}],
            "ReportingBalance": [{"GLAccountCode": "1100", "GLAccountDescription": "Bank", "Amount": -120.5}],
        }
    )

    result = await call(server, "cash_position")

    assert result["total"] == 379.5
    assert exact.requests[2].url.params["$filter"] == f"(GLAccount eq guid'{GL_ACCOUNT_ID}')"
    assert f"(GLAccount eq guid'{GL_ACCOUNT_ID}')" in exact.requests[3].url.params["$filter"]


async def test_budget_vs_actual_needs_a_scenario_when_there_are_several(server, exact):
    def budget(code, description, amount):
        return {
            "BudgetScenarioCode": code,
            "BudgetScenarioDescription": description,
            "GLAccountCode": "8000",
            "GLAccountDescription": "Revenue",
            "AmountDC": amount,
        }

    exact.handler = by_resource(
        {
            "Budgets": [budget("B1", "Base", -1200.0), budget("B2", "Stretch", -1500.0)],
            "ReportingBalance": [
                {"GLAccountCode": "8000", "GLAccountDescription": "Revenue", "Amount": -1000.0},
                {"GLAccountCode": "4000", "GLAccountDescription": "Wages", "Amount": 300.0},
            ],
        }
    )

    async with Client(server) as mcp:
        with pytest.raises(ToolError, match="pick one of B1, B2"):
            await mcp.call_tool("budget_vs_actual", {"year": 2026})
    result = await call(server, "budget_vs_actual", {"year": 2026, "scenario": "B1"})

    assert result["scenario"] == {"code": "B1", "description": "Base"}
    assert result["accounts"] == [
        {"code": "4000", "description": "Wages", "budget": 0.0, "actual": 300.0, "variance": 300.0},
        {"code": "8000", "description": "Revenue", "budget": -1200.0, "actual": -1000.0, "variance": 200.0},
    ]
    assert (result["budgeted_result"], result["actual_result"]) == (1200.0, 700.0)


async def test_customer_payment_behaviour_puts_the_slowest_payers_first(server, exact):
    def receivable(account, invoiced, due, paid, fully_paid=True, reversal=False):
        return {
            "Account": account,
            "AccountCode": account,
            "AccountName": account,
            "InvoiceDate": invoiced,
            "DueDate": due,
            "LastPaymentDate": paid,
            "IsFullyPaid": fully_paid,
            "TransactionIsReversal": reversal,
        }

    exact.handler = lambda r: httpx.Response(
        200,
        json=odata(
            [
                receivable("A", "2026-01-01", "2026-01-31", "2026-02-10"),
                receivable("A", "2026-02-01", "2026-03-03", "2026-02-20"),
                receivable("B", "2026-01-01", "2026-01-15", "2026-03-01"),
                receivable("B", "2026-01-05", "2026-01-20", "2026-01-10", fully_paid=False),
                receivable("B", "2026-01-06", "2026-01-20", "2026-01-10", reversal=True),
            ]
        ),
    )

    result = await call(server, "customer_payment_behaviour", {"invoice_date_from": "2026-01-01"})

    assert [(c["account_id"], c["paid_invoices"]) for c in result["customers"]] == [("B", 1), ("A", 2)]
    assert result["customers"][1] == {
        "account_id": "A",
        "code": "A",
        "name": "A",
        "paid_invoices": 2,
        "average_days_to_pay": 29.5,
        "average_days_late": -0.5,
        "paid_late_share": 0.5,
    }
    assert exact.requests[0].url.path == "/api/v1/123/bulk/Cashflow/Receivables"
    assert exact.requests[0].url.params["$filter"] == "InvoiceDate ge datetime'2026-01-01' and Status eq 50"


async def test_bad_id_is_a_tool_error_not_an_injected_filter(server, exact):
    async with Client(server) as mcp:
        with pytest.raises(ToolError, match="not an Exact Online ID"):
            await mcp.call_tool("get_account", {"account_id": "x') or (1 eq 1"})
    assert exact.requests == []


async def test_unconfigured_server_answers_with_setup_instructions(monkeypatch):
    for name in ("FORO_UPSTREAM_BROKER_URL", "FORO_UPSTREAM_BROKER_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    async with Client(create_server()) as mcp:
        assert len(await mcp.list_tools()) == 28 + 16 + 9
        with pytest.raises(ToolError, match="foro's upstream broker.*FORO_UPSTREAM_BROKER_URL"):
            await mcp.call_tool("list_divisions", {})


async def test_list_journals_names_each_type(server, exact):
    exact.handler = lambda r: httpx.Response(200, json=odata([{"Code": "20", "Type": 12}, {"Code": "X", "Type": 99}]))

    rows = (await call(server, "list_journals", {"type": "bank"}))["result"]

    assert [row["TypeDescription"] for row in rows] == ["bank", "99"]
    assert exact.requests[0].url.params["$filter"] == "Type eq 12"


async def test_list_vat_codes_for_purchases_includes_codes_for_both(server, exact):
    exact.handler = lambda r: httpx.Response(200, json=odata([]))

    await call(server, "list_vat_codes", {"use": "purchase"})

    assert exact.requests[0].url.params["$filter"] == (
        "IsBlocked eq false and (VATTransactionType eq 'P' or VATTransactionType eq 'B')"
    )
