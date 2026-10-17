import functools
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import odata
from .client import ExactClient, ExactError
from .config import ConfigError
from .tokens import NotConnectedError, TokenRefreshError

Division = Annotated[
    int | None,
    Field(description="Division (administration) number. Omit for the connection's default; see list_divisions."),
]
Limit = Annotated[int, Field(ge=1, le=500, description="Maximum rows to return.")]
AccountId = Annotated[str, Field(description="Account ID (GUID) from search_accounts.")]

INVOICE_STATUS = {"draft": 10, "open": 20, "processed": 50}
ORDER_STATUS = {"open": 12, "partial": 20, "complete": 21, "cancelled": 45}

ACCOUNT_FIELDS = ["ID", "Code", "Name", "City", "Country", "Email", "Phone", "IsSales", "IsSupplier"]
OPEN_ITEM_FIELDS = [
    "AccountId",
    "AccountCode",
    "AccountName",
    "InvoiceNumber",
    "InvoiceDate",
    "DueDate",
    "Amount",
    "CurrencyCode",
    "Description",
    "YourRef",
]
INVOICE_FIELDS = [
    "InvoiceID",
    "InvoiceNumber",
    "InvoiceDate",
    "DueDate",
    "InvoiceTo",
    "InvoiceToName",
    "AmountDC",
    "VATAmountDC",
    "Currency",
    "Status",
    "StatusDescription",
    "Description",
    "YourRef",
]
ORDER_FIELDS = [
    "OrderID",
    "OrderNumber",
    "OrderDate",
    "DeliveryDate",
    "OrderedBy",
    "OrderedByName",
    "AmountDC",
    "Currency",
    "Status",
    "StatusDescription",
    "Description",
    "YourRef",
]

# Every failure a caller can act on, surfaced as the tool's error text rather than masked.
_USER_ERRORS = (ConfigError, NotConnectedError, TokenRefreshError, ExactError, ValueError)


def _date_range(field: str, start: date | None, end: date | None) -> list[str]:
    filters = []
    if start:
        filters.append(f"{field} ge {odata.datetime_literal(start)}")
    if end:
        filters.append(f"{field} le {odata.datetime_literal(end)}")
    return filters


def _totals(rows: list[dict], amount: str, currency: str) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        totals[row[currency]] += row[amount] or 0
    return {cur: round(total, 2) for cur, total in totals.items()}


def register(mcp: FastMCP, get_client: Callable[[], Awaitable[ExactClient]]) -> None:
    def tool(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except _USER_ERRORS as err:
                raise ToolError(str(err)) from err

        return mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))(wrapper)

    @tool
    async def list_divisions() -> dict:
        """The Exact Online administrations (divisions) this connection can read, and which
        one the other tools use when no division is given."""
        client = await get_client()
        me = await client.me()
        return {
            "user": me["FullName"],
            "default_division": await client.division(),
            "divisions": await client.list("system/Divisions", select=["Code", "Description"], limit=1000),
        }

    @tool
    async def search_accounts(
        query: Annotated[str | None, Field(description="Part of the account name.")] = None,
        role: Literal["any", "customer", "supplier"] = "any",
        limit: Limit = 25,
        division: Division = None,
    ) -> list[dict]:
        """Find customers and suppliers (Exact "accounts") by name."""
        filters = []
        if query:
            filters.append(f"substringof({odata.string(query)}, Name) eq true")
        if role == "customer":
            filters.append("IsSales eq true")
        elif role == "supplier":
            filters.append("IsSupplier eq true")
        client = await get_client()
        return await client.list(
            "crm/Accounts", select=ACCOUNT_FIELDS, filters=filters, orderby="Name", limit=limit, division=division
        )

    @tool
    async def get_account(account_id: AccountId, division: Division = None) -> dict:
        """One account's contact and registration details."""
        client = await get_client()
        rows = await client.list(
            "crm/Accounts",
            select=[*ACCOUNT_FIELDS, "AddressLine1", "Postcode", "VATNumber", "ChamberOfCommerce", "Website"],
            filters=[f"ID eq {odata.guid(account_id)}"],
            limit=1,
            division=division,
        )
        if not rows:
            raise ToolError(f"No account {account_id} in this division.")
        return rows[0]

    async def _open_items(resource, account_id, overdue_only, limit, division) -> dict:
        filters = []
        if account_id:
            filters.append(f"AccountId eq {odata.guid(account_id)}")
        if overdue_only:
            filters.append(f"DueDate lt {odata.datetime_literal(date.today())}")
        client = await get_client()
        rows = await client.list(
            resource, select=OPEN_ITEM_FIELDS, filters=filters, orderby="DueDate", limit=limit, division=division
        )
        return {
            "totals_by_currency": _totals(rows, "Amount", "CurrencyCode"),
            "items": rows,
            "truncated": len(rows) == limit,
        }

    @tool
    async def open_receivables(
        account_id: Annotated[str | None, Field(description="Only this customer (GUID).")] = None,
        overdue_only: bool = False,
        limit: Limit = 100,
        division: Division = None,
    ) -> dict:
        """Unpaid sales invoices (what customers owe), oldest due date first, with totals."""
        return await _open_items("read/financial/ReceivablesList", account_id, overdue_only, limit, division)

    @tool
    async def open_payables(
        account_id: Annotated[str | None, Field(description="Only this supplier (GUID).")] = None,
        overdue_only: bool = False,
        limit: Limit = 100,
        division: Division = None,
    ) -> dict:
        """Unpaid purchase invoices (what is owed to suppliers), oldest due date first, with totals."""
        return await _open_items("read/financial/PayablesList", account_id, overdue_only, limit, division)

    @tool
    async def receivables_aging(limit: Limit = 100, division: Division = None) -> list[dict]:
        """Outstanding receivables per customer, split into Exact's age buckets."""
        client = await get_client()
        return await client.list(
            "read/financial/AgingReceivablesList",
            select=[
                "AccountId",
                "AccountCode",
                "AccountName",
                "CurrencyCode",
                "TotalAmount",
                *(f"AgeGroup{n}{suffix}" for n in range(1, 5) for suffix in ("Description", "Amount")),
            ],
            orderby="TotalAmount desc",
            limit=limit,
            division=division,
        )

    @tool
    async def search_sales_invoices(
        account_id: Annotated[str | None, Field(description="Only invoices to this customer (GUID).")] = None,
        status: Literal["draft", "open", "processed"] | None = None,
        invoice_date_from: date | None = None,
        invoice_date_to: date | None = None,
        limit: Limit = 25,
        division: Division = None,
    ) -> list[dict]:
        """Sales invoices, newest first. Amounts are in the division's currency (AmountDC)."""
        filters = _date_range("InvoiceDate", invoice_date_from, invoice_date_to)
        if account_id:
            filters.append(f"InvoiceTo eq {odata.guid(account_id)}")
        if status:
            filters.append(f"Status eq {INVOICE_STATUS[status]}")
        client = await get_client()
        return await client.list(
            "salesinvoice/SalesInvoices",
            select=INVOICE_FIELDS,
            filters=filters,
            orderby="InvoiceDate desc",
            limit=limit,
            division=division,
        )

    @tool
    async def get_sales_invoice(
        invoice_id: Annotated[str, Field(description="InvoiceID (GUID) from search_sales_invoices.")],
        division: Division = None,
    ) -> dict:
        """One sales invoice with its lines."""
        client = await get_client()
        div = await client.division(division)
        rows = await client.list(
            "salesinvoice/SalesInvoices",
            select=[*INVOICE_FIELDS, "PaymentConditionDescription"],
            filters=[f"InvoiceID eq {odata.guid(invoice_id)}"],
            limit=1,
            division=div,
        )
        if not rows:
            raise ToolError(f"No sales invoice {invoice_id} in this division.")
        # Exact has no $expand, so lines are a second request.
        lines = await client.list(
            "salesinvoice/SalesInvoiceLines",
            select=[
                "LineNumber",
                "ItemCode",
                "ItemDescription",
                "Description",
                "Quantity",
                "UnitPrice",
                "AmountFC",
                "VATAmountFC",
                "VATCode",
            ],
            filters=[f"InvoiceID eq {odata.guid(invoice_id)}"],
            orderby="LineNumber",
            limit=500,
            division=div,
        )
        return {**rows[0], "lines": lines}

    @tool
    async def search_sales_orders(
        account_id: Annotated[str | None, Field(description="Only orders from this customer (GUID).")] = None,
        status: Literal["open", "partial", "complete", "cancelled"] | None = None,
        order_date_from: date | None = None,
        order_date_to: date | None = None,
        limit: Limit = 25,
        division: Division = None,
    ) -> list[dict]:
        """Sales orders, newest first."""
        filters = _date_range("OrderDate", order_date_from, order_date_to)
        if account_id:
            filters.append(f"OrderedBy eq {odata.guid(account_id)}")
        if status:
            filters.append(f"Status eq {ORDER_STATUS[status]}")
        client = await get_client()
        return await client.list(
            "salesorder/SalesOrders",
            select=ORDER_FIELDS,
            filters=filters,
            orderby="OrderDate desc",
            limit=limit,
            division=division,
        )

    @tool
    async def search_items(
        query: Annotated[str | None, Field(description="Part of the item code or description.")] = None,
        limit: Limit = 25,
        division: Division = None,
    ) -> list[dict]:
        """Products and services (Exact "items")."""
        filters = []
        if query:
            q = odata.string(query)
            filters.append(f"(substringof({q}, Code) eq true or substringof({q}, Description) eq true)")
        client = await get_client()
        return await client.list(
            "logistics/Items",
            select=["ID", "Code", "Description", "IsSalesItem", "IsPurchaseItem", "IsStockItem", "Unit"],
            filters=filters,
            orderby="Code",
            limit=limit,
            division=division,
        )

    @tool
    async def profit_and_loss(
        year: Annotated[int, Field(ge=1990, le=2100, description="Financial year.")],
        period_from: Annotated[int, Field(ge=1, le=12)] = 1,
        period_to: Annotated[int, Field(ge=1, le=12)] = 12,
        division: Division = None,
    ) -> dict:
        """Profit and loss per general ledger account over a range of periods. Exact amounts are
        debit-positive, so revenue shows negative; `result` flips that (positive = profit)."""
        if period_from > period_to:
            raise ToolError("period_from must not be after period_to.")
        client = await get_client()
        rows = await client.list(
            "financial/ReportingBalance",
            select=["GLAccountCode", "GLAccountDescription", "Amount"],
            filters=[
                f"ReportingYear eq {year}",
                f"ReportingPeriod ge {period_from}",
                f"ReportingPeriod le {period_to}",
                "BalanceType eq 'W'",
            ],
            limit=10_000,
            division=division,
        )
        accounts: dict[str, dict] = {}
        for row in rows:
            entry = accounts.setdefault(
                row["GLAccountCode"],
                {"code": row["GLAccountCode"], "description": row["GLAccountDescription"], "amount": 0.0},
            )
            entry["amount"] += row["Amount"] or 0
        lines = sorted(accounts.values(), key=lambda a: a["code"])
        for line in lines:
            line["amount"] = round(line["amount"], 2)
        return {
            "year": year,
            "periods": [period_from, period_to],
            "result": round(-sum(a["amount"] for a in lines), 2),
            "accounts": lines,
        }
