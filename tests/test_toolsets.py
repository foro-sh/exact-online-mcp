import base64
import json

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from exact_mcp import config, toolsets
from exact_mcp.app import create_server

from .conftest import odata
from .test_odata_and_config import EDMX
from .test_tools import ACCOUNT_ID, GL_ACCOUNT_ID, by_resource, call

ORDER_ID = "8f7b2c1e-0000-4000-8000-000000000011"
PROJECT_ID = "8f7b2c1e-0000-4000-8000-000000000012"
DOCUMENT_ID = "8f7b2c1e-0000-4000-8000-000000000013"
TYPE_ID = "8f7b2c1e-0000-4000-8000-000000000014"


@pytest.fixture
def server(client):
    async def get_client():
        return client

    mcp = FastMCP("test")
    toolsets.register(mcp, get_client)
    return mcp


async def tool_names(mcp) -> set[str]:
    async with Client(mcp) as session:
        return {tool.name for tool in await session.list_tools()}


async def test_only_the_core_tools_are_on_by_default(monkeypatch):
    monkeypatch.delenv("EXACT_TOOLSETS", raising=False)
    assert len(await tool_names(create_server())) == 28


async def test_exact_toolsets_switches_on_the_toolsets_it_names(monkeypatch):
    monkeypatch.setenv("EXACT_TOOLSETS", "orders,crm")

    listed = await tool_names(create_server())

    assert {"get_sales_order", "stock_levels", "list_contacts"} <= listed
    assert "project_hours" not in listed
    assert len(listed) == 28 + 3 + 3


async def test_all_switches_every_read_toolset_on(monkeypatch):
    monkeypatch.setenv("EXACT_TOOLSETS", "all")
    listed = await tool_names(create_server())
    assert len(listed) == 28 + 16
    assert "book_purchase_invoice" not in listed


async def test_every_toolset_is_known_and_only_bookkeeping_writes(server):
    async with Client(server) as mcp:
        listed = await mcp.list_tools()
    assert set(toolsets.MODULES) == set(config.TOOLSETS)
    writes = {t.name for t in listed if not t.annotations.read_only_hint}
    assert {t.name for t in listed if "bookkeeping" in t.meta["fastmcp"]["tags"]} > writes
    assert writes == {
        "book_purchase_invoice",
        "book_sales_invoice",
        "book_journal_entry",
        "create_account",
        "upload_document",
        "match_open_items",
    }
    assert len(listed) == 16 + 9


async def test_get_sales_order_adds_its_lines(server, exact):
    def handler(request):
        if request.url.path.endswith("SalesOrderLines"):
            return httpx.Response(200, json=odata([{"LineNumber": 1, "Quantity": 5, "QuantityDelivered": 2}]))
        return httpx.Response(200, json=odata([{"OrderID": ORDER_ID, "OrderNumber": 7}]))

    exact.handler = handler

    result = await call(server, "get_sales_order", {"order_id": ORDER_ID})

    assert result["OrderNumber"] == 7
    assert result["lines"] == [{"LineNumber": 1, "Quantity": 5, "QuantityDelivered": 2}]
    assert exact.requests[1].url.params["$filter"] == f"OrderID eq guid'{ORDER_ID}'"


async def test_search_purchase_orders_filters_on_status_and_names_each_status(server, exact):
    exact.handler = lambda r: httpx.Response(
        200, json=odata([{"OrderStatus": 20, "ReceiptStatus": 30, "InvoiceStatus": 10}])
    )

    rows = (await call(server, "search_purchase_orders", {"status": "partial", "account_id": ACCOUNT_ID}))["result"]

    assert rows == [
        {
            "OrderStatus": 20,
            "ReceiptStatus": 30,
            "InvoiceStatus": 10,
            "OrderStatusDescription": "partial",
            "ReceiptStatusDescription": "complete",
            "InvoiceStatusDescription": "open",
        }
    ]
    assert exact.requests[0].url.params["$filter"] == f"Supplier eq guid'{ACCOUNT_ID}' and OrderStatus eq 20"


async def test_stock_levels_compares_projected_stock_with_the_reorder_point(server, exact):
    exact.handler = lambda r: httpx.Response(
        200,
        json=odata(
            [
                {"ItemCode": "A", "ProjectedStock": 2.0, "ReorderPoint": 5.0},
                {"ItemCode": "B", "ProjectedStock": 9.0, "ReorderPoint": 5.0},
            ]
        ),
    )

    result = await call(server, "stock_levels", {"below_reorder_point": True})

    assert [item["ItemCode"] for item in result["items"]] == ["A"]
    assert result["truncated"] is False
    assert exact.requests[0].url.params["$filter"] == "ReorderPoint gt 0"


async def test_project_hours_add_up_per_hour_type(server, exact):
    def hours(hour_type, quantity, amount, status):
        return {
            "ItemDescription": hour_type,
            "Quantity": quantity,
            "AmountFC": amount,
            "Currency": "EUR",
            "HourStatus": status,
        }

    exact.handler = lambda r: httpx.Response(
        200,
        json=odata([hours("Design", 3.0, 300.0, 20), hours("Design", 2.0, 200.0, 10), hours("Build", 8.0, 640.0, 20)]),
    )

    result = await call(server, "project_hours", {"project_id": PROJECT_ID})

    assert (result["hours"], result["final_hours"], result["currencies"]) == (13.0, 11.0, ["EUR"])
    assert result["hour_types"] == [
        {"hour_type": "Build", "hours": 8.0, "final_hours": 8.0, "amount": 640.0},
        {"hour_type": "Design", "hours": 5.0, "final_hours": 3.0, "amount": 500.0},
    ]
    assert exact.requests[0].url.params["$filter"] == f"Project eq guid'{PROJECT_ID}'"


async def test_project_result_counts_profit_and_loss_accounts_only(server, exact):
    exact.handler = by_resource(
        {
            "Projects": [{"Code": "P1", "Description": "Website", "BudgetedRevenue": 1000.0, "BudgetedCosts": 600.0}],
            "GLAccounts": [
                {"ID": GL_ACCOUNT_ID, "Code": "8000", "BalanceType": "W"},
                {"ID": GL_ACCOUNT_ID, "Code": "1300", "BalanceType": "B"},
            ],
            "TransactionLines": [
                {"GLAccountCode": "8000", "GLAccountDescription": "Revenue", "AmountDC": -900.0},
                {"GLAccountCode": "1300", "GLAccountDescription": "Debtors", "AmountDC": 900.0},
            ],
        }
    )

    result = await call(server, "project_result", {"project_id": PROJECT_ID})

    assert (result["result"], result["BudgetedRevenue"]) == (900.0, 1000.0)
    assert result["accounts"] == [{"code": "8000", "description": "Revenue", "amount": -900.0}]


async def test_upcoming_project_billing_reads_draft_and_open_terms(server, exact):
    exact.handler = lambda r: httpx.Response(200, json=odata([{"InvoiceStatus": 20}]))

    rows = (await call(server, "upcoming_project_billing", {"invoice_date_to": "2026-12-31"}))["result"]

    assert rows[0]["InvoiceStatusDescription"] == "Open"
    assert exact.requests[0].url.params["$filter"] == "InvoiceStatus lt 30 and InvoiceDate le datetime'2026-12-31'"


async def test_search_quotations_keeps_each_quotations_latest_version(server, exact):
    exact.handler = lambda r: httpx.Response(
        200,
        json=odata(
            [
                {"QuotationNumber": 7, "VersionNumber": 1, "AmountDC": 100.0},
                {"QuotationNumber": 7, "VersionNumber": 2, "AmountDC": 120.0},
                {"QuotationNumber": 8, "VersionNumber": 1, "AmountDC": 50.0},
            ]
        ),
    )

    rows = (await call(server, "search_quotations", {"status": "open"}))["result"]

    assert [(row["QuotationNumber"], row["VersionNumber"]) for row in rows] == [(7, 2), (8, 1)]
    assert exact.requests[0].url.params["$filter"] == "Status eq 25"


async def test_search_opportunities_defaults_to_open_ones_with_their_total(server, exact):
    exact.handler = lambda r: httpx.Response(200, json=odata([{"AmountDC": 1000.0}, {"AmountDC": 250.5}]))

    result = await call(server, "search_opportunities")

    assert result["total_amount_dc"] == 1250.5
    assert exact.requests[0].url.params["$filter"] == "OpportunityStatus eq 1"


async def test_list_contacts_selects_business_details_only_and_the_main_contact_first(server, exact):
    exact.handler = lambda r: httpx.Response(
        200, json=odata([{"FullName": "Other", "IsMainContact": False}, {"FullName": "Main", "IsMainContact": True}])
    )

    rows = (await call(server, "list_contacts", {"account_id": ACCOUNT_ID}))["result"]

    assert [row["FullName"] for row in rows] == ["Main", "Other"]
    assert exact.requests[0].url.params["$select"] == (
        "FullName,JobTitleDescription,IsMainContact,BusinessEmail,BusinessPhone"
    )


async def test_subscription_mrr_divides_each_line_by_its_invoice_period(server, exact):
    def subscription(entry, customer, start, end):
        return {
            "EntryID": entry,
            "Description": f"{customer} plan",
            "OrderedByName": customer,
            "SubscriptionType": TYPE_ID,
            "SubscriptionTypeDescription": "Quarterly",
            "StartDate": start,
            "EndDate": end,
        }

    exact.handler = by_resource(
        {
            "Subscriptions": [
                subscription("s1", "Acme", "2026-01-01", None),
                subscription("s2", "Globex", "2025-01-01", "2025-12-31"),
            ],
            "SubscriptionTypes": [{"ID": TYPE_ID, "InvoicePeriod": 1, "InvoicePeriodUnit": "qt"}],
            "SubscriptionLines": [
                {"EntryID": "s1", "AmountDC": 300.0, "FromDate": "2026-01-01", "ToDate": None},
                {"EntryID": "s2", "AmountDC": 900.0, "FromDate": "2025-01-01", "ToDate": None},
            ],
        }
    )

    result = await call(server, "subscription_mrr", {"as_of": "2026-06-01"})

    assert (result["mrr"], result["arr"], result["subscriptions"]) == (100.0, 1200.0, 1)
    assert result["largest"] == [
        {"customer": "Acme", "description": "Acme plan", "type": "Quarterly", "monthly_amount": 100.0}
    ]


async def test_search_documents_filters_by_booked_invoice_and_subject(server, exact):
    exact.handler = lambda r: httpx.Response(200, json=odata([]))

    await call(server, "search_documents", {"entry_id": DOCUMENT_ID, "query": "invoice"})

    assert exact.requests[0].url.params["$filter"] == (
        f"FinancialTransactionEntryID eq guid'{DOCUMENT_ID}' and substringof('invoice', Subject) eq true"
    )


ATTACHMENT_URL = "https://start.exactonline.nl/docs/SysAttachment.aspx?ID=a1&_Division_=1"


async def test_get_document_returns_its_attachment_as_a_file(server, exact):
    pdf = b"%PDF-1.7 test"

    def handler(request):
        if request.url.path == "/docs/SysAttachment.aspx":
            return httpx.Response(200, content=pdf)
        return httpx.Response(
            200, json=odata([{"ID": "a1", "FileName": "invoice.pdf", "FileSize": len(pdf), "Url": ATTACHMENT_URL}])
        )

    exact.handler = handler

    async with Client(server) as mcp:
        result = await mcp.call_tool("get_document", {"document_id": DOCUMENT_ID})

    text, file = result.content
    assert json.loads(text.text)["attachments"][0]["FileName"] == "invoice.pdf"
    assert file.resource.mime_type == "application/pdf"
    assert base64.b64decode(file.resource.blob) == pdf
    assert exact.requests[0].url.params["$filter"] == f"Document eq guid'{DOCUMENT_ID}'"
    assert exact.requests[1].headers["Authorization"].startswith("Bearer ")


async def test_get_document_sends_the_token_only_to_exact(server, exact):
    url = "https://elsewhere.example/file.pdf"
    exact.handler = lambda r: httpx.Response(
        200, json=odata([{"ID": "a1", "FileName": "x.pdf", "FileSize": 1, "Url": url}])
    )

    with pytest.raises(ToolError, match="Not downloading"):
        await call(server, "get_document", {"document_id": DOCUMENT_ID})
    assert len(exact.requests) == 1


async def test_recent_changes_counts_lines_per_journal_and_deletions_per_kind(server, exact):
    exact.handler = by_resource(
        {
            "TransactionLines": [
                {"Modified": "2026-11-02T09:00:00+00:00", "JournalCode": "70", "JournalDescription": "Sales"},
                {"Modified": "2026-11-03T10:00:00+00:00", "JournalCode": "70", "JournalDescription": "Sales"},
                {"Modified": "2026-11-02T08:00:00+00:00", "JournalCode": "20", "JournalDescription": "Bank"},
            ],
            "Deleted": [
                {"EntityType": 13, "DeletedDate": "2026-11-02T11:00:00+00:00"},
                # An employee, which is left out, and a deletion from before the day asked for.
                {"EntityType": 42, "DeletedDate": "2026-11-02T11:00:00+00:00"},
                {"EntityType": 13, "DeletedDate": "2026-10-01T11:00:00+00:00"},
            ],
        }
    )

    result = await call(server, "recent_changes", {"since": "2026-11-01"})

    assert result["changed_lines"] == 3
    assert result["changed_lines_per_journal"] == [
        {"journal": "70", "description": "Sales", "lines": 2},
        {"journal": "20", "description": "Bank", "lines": 1},
    ]
    assert result["latest_changed_lines"][0]["Modified"].startswith("2026-11-03")
    assert result["deleted"] == {"SalesInvoices": 1}
    assert exact.requests[0].url.params["$filter"] == "Modified ge datetime'2026-11-01'"
    assert exact.requests[1].url.params["$filter"] == "Timestamp gt 1"


async def test_query_reads_an_allowed_resource_with_the_agents_odata(server, exact):
    exact.handler = lambda r: httpx.Response(200, json=odata([{"City": "Utrecht"}]))

    rows = (
        await call(
            server, "query", {"resource": "crm/Addresses", "select": ["City"], "filter": "Type eq 1", "orderby": "City"}
        )
    )["result"]

    assert rows == [{"City": "Utrecht"}]
    params = exact.requests[0].url.params
    assert exact.requests[0].url.path == "/api/v1/123/crm/Addresses"
    assert (params["$select"], params["$filter"], params["$orderby"]) == ("City", "Type eq 1", "City")


async def test_query_refuses_payroll_and_identity_fields_before_calling_exact(server, exact):
    async with Client(server) as mcp:
        with pytest.raises(ToolError, match="doesn't read payroll"):
            await mcp.call_tool("query", {"resource": "payroll/Employees", "select": ["ID"]})
        with pytest.raises(ToolError, match="identify people"):
            await mcp.call_tool("query", {"resource": "crm/Contacts", "select": ["FullName", "BirthDate"]})
    assert exact.requests == []


async def test_describe_resource_lists_the_fields_query_may_read(server, exact):
    with_creator = EDMX.replace(
        '<Property Name="City" Type="Edm.String"/>',
        '<Property Name="City" Type="Edm.String"/><Property Name="Creator" Type="Edm.Guid"/>',
    )
    exact.handler = lambda r: httpx.Response(200, text=with_creator)

    fields = (await call(server, "describe_resource", {"resource": "crm/Addresses"}))["result"]

    assert fields == [{"name": "ID", "type": "Edm.Guid"}, {"name": "City", "type": "Edm.String"}]
    assert exact.requests[0].url.path == "/api/v1/123/crm/$metadata"
