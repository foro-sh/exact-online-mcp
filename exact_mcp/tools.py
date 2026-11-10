import functools
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
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
# read/financial/Returns has no descriptions for its codes, so these are Exact's documented ones.
RETURN_TYPES = {
    31: "VAT",
    32: "EC sales list",
    33: "VAT listing",
    34: "Intrastat",
    35: "Fiscal fiches 281.50-325.50",
    37: "Fiscal fiche 281.30",
    38: "Fiscal fiche 281.50",
    39: "Fiscal fiche 281.71",
    46: "Income tax",
    47: "Corporate tax",
    146: "Payroll declaration",
    189: "Official return",
    200: "Annual statement",
}
RETURN_STATUS = {
    -10: "None",
    0: "Void",
    5: "Rejected",
    10: "Draft",
    20: "Open",
    30: "Approved",
    40: "Realized",
    50: "Processed",
}

JOURNAL_TYPES = {"cash": 10, "bank": 12, "payment_service": 16, "sales": 20, "purchase": 22, "general": 90}
VAT_CODE_FIELDS = ["Code", "Description", "Percentage", "Type", "VATTransactionType", "Charged", "IsBlocked"]

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
# Sales and purchase entries share these; each adds its party, Customer or Supplier.
ENTRY_FIELDS = [
    "EntryID",
    "EntryNumber",
    "InvoiceNumber",
    "EntryDate",
    "DueDate",
    "AmountDC",
    "VATAmountDC",
    "Currency",
    "Description",
    "YourRef",
    "Status",
    "StatusDescription",
    "Type",
    "TypeDescription",
    "Document",
]
AGING_FIELDS = [
    "AccountId",
    "AccountCode",
    "AccountName",
    "CurrencyCode",
    "TotalAmount",
    *(f"AgeGroup{n}{suffix}" for n in range(1, 5) for suffix in ("Description", "Amount")),
]
GL_ACCOUNT_FIELDS = [
    "ID",
    "Code",
    "Description",
    "Type",
    "TypeDescription",
    "BalanceType",
    "BalanceSide",
    "IsBlocked",
]
TRANSACTION_FIELDS = [
    "Date",
    "JournalCode",
    "JournalDescription",
    "EntryNumber",
    "GLAccountCode",
    "GLAccountDescription",
    "AccountCode",
    "AccountName",
    "Description",
    "AmountDC",
    "InvoiceNumber",
    "CostCenter",
    "ProjectCode",
]
BANK_LINE_FIELDS = [
    "Date",
    "EntryNumber",
    "Description",
    "AmountDC",
    "AccountCode",
    "AccountName",
    "GLAccountCode",
    "GLAccountDescription",
    "OurRef",
]
# Bulk endpoints return 1,000 lines a call, so this is at most 10 calls.
MAX_TRANSACTION_LINES = 10_000
# GL account types that hold money: cash, bank, credit card and payment services.
CASH_TYPES = {10, 12, 14, 16}

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


async def _gl_accounts(client: ExactClient, division: int | None) -> list[dict]:
    # The bulk endpoint returns the whole chart in a call or two, but can't filter on Description.
    return await client.list("bulk/Financial/GLAccounts", select=GL_ACCOUNT_FIELDS, limit=10_000, division=division)


async def _gl_totals(client, division, year, period_from, period_to, balance_type, filters=()) -> dict[str, dict]:
    """ReportingBalance summed per GL account code over a range of periods."""
    rows = await client.list(
        "financial/ReportingBalance",
        select=["GLAccountCode", "GLAccountDescription", "Amount"],
        filters=[
            f"ReportingYear eq {year}",
            f"ReportingPeriod ge {period_from}",
            f"ReportingPeriod le {period_to}",
            f"BalanceType eq '{balance_type}'",
            *filters,
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
    return accounts


async def _recent_periods(client: ExactClient, division: int | None) -> list[dict]:
    # Enough years to place today and find the previous year's last period, even when the
    # financial year doesn't follow the calendar.
    return await client.list(
        "financial/FinancialPeriods",
        select=["FinYear", "FinPeriod", "StartDate", "EndDate"],
        filters=[f"FinYear ge {date.today().year - 2}"],
        limit=1000,
        division=division,
    )


def _period_on(periods: list[dict], day: date) -> tuple[int, int]:
    for period in periods:
        if period["StartDate"][:10] <= day.isoformat() <= period["EndDate"][:10]:
            return period["FinYear"], period["FinPeriod"]
    raise ToolError(f"No financial period in Exact covers {day}; check the division's financial years.")


async def _balances(client, division, which, year, period, filters=()) -> list[dict]:
    """Balance-sheet accounts at the end of `period`: Exact's opening balance for the year
    (`which` is CurrentYear or PreviousYear, the only two it has) plus the movements since."""
    opening = await client.list(
        f"openingbalance/{which}/AfterEntry",
        select=["GLAccountCode", "GLAccountDescription", "Amount"],
        filters=filters,
        limit=10_000,
        division=division,
    )
    # Opening-balance entries (type 10) are already in the opening balance.
    movements = await _gl_totals(client, division, year, 1, period, "B", [*filters, "Type ne 10"])
    accounts: dict[str, dict] = {}

    def account(code, description):
        return accounts.setdefault(code, {"code": code, "description": description, "opening": 0.0, "movements": 0.0})

    for row in opening:
        account(row["GLAccountCode"], row["GLAccountDescription"])["opening"] += row["Amount"] or 0
    for code, moved in movements.items():
        account(code, moved["description"])["movements"] += moved["amount"]
    lines = sorted(accounts.values(), key=lambda a: a["code"])
    for line in lines:
        line["closing"] = round(line["opening"] + line["movements"], 2)
        line["opening"] = round(line["opening"], 2)
        line["movements"] = round(line["movements"], 2)
    return lines


READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
# Writes only add records (an entry, an account, a match), never change or delete one.
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)


def toolset_tools(mcp: FastMCP, toolset: str) -> Callable:
    """A decorator that registers a tool tagged `toolset`. Read-only unless `@tool(writes=True)`,
    which is what a foro project policy uses to tell a write from a read."""

    def register_tool(fn, writes: bool):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except _USER_ERRORS as err:
                raise ToolError(str(err)) from err

        return mcp.tool(tags={toolset}, annotations=WRITE if writes else READ)(wrapper)

    def tool(fn=None, *, writes: bool = False):
        if fn is None:
            return lambda fn: register_tool(fn, writes)
        return register_tool(fn, writes)

    return tool


def register(mcp: FastMCP, get_client: Callable[[], Awaitable[ExactClient]]) -> None:
    tool = toolset_tools(mcp, "core")

    @tool
    async def list_divisions() -> dict:
        """The Exact Online administrations (divisions) this connection can read, and which
        one the other tools use when no division is given. Status 1 is active, 0 inactive and
        2 archived; a BlockingStatus other than 0 means Exact is busy with the division, for
        example with a backup or a conversion."""
        client = await get_client()
        me = await client.me()
        return {
            "user": me["FullName"],
            "default_division": await client.division(),
            "divisions": await client.list(
                "system/Divisions", select=["Code", "Description", "Status", "BlockingStatus"], limit=1000
            ),
        }

    @tool
    async def financial_overview(division: Division = None) -> dict:
        """A first look at how a division is doing, in three calls: revenue, costs and result for
        the current period and year against last year, outstanding and overdue invoices per
        currency, and outstanding amounts per age bucket. Exact takes the current period from
        today's date and works out the result as revenue minus costs."""
        client = await get_client()
        div = await client.division(division)
        pnl = await client.list(
            "read/financial/ProfitLossOverview",
            select=[
                "CurrencyCode",
                "CurrentYear",
                "CurrentPeriod",
                "PreviousYear",
                "PreviousYearPeriod",
                *(
                    f"{measure}{when}"
                    for measure in ("Revenue", "Costs", "Result")
                    for when in ("CurrentPeriod", "CurrentYear", "PreviousYear", "PreviousYearPeriod")
                ),
            ],
            limit=1,
            division=div,
        )
        outstanding = await client.list(
            "read/financial/OutstandingInvoicesOverview",
            select=[
                "CurrencyCode",
                *(
                    f"{state}{side}Invoice{measure}"
                    for side in ("Receivable", "Payable")
                    for state in ("Outstanding", "Overdue")
                    for measure in ("Amount", "Count")
                ),
            ],
            limit=100,
            division=div,
        )
        aging = await client.list(
            "read/financial/AgingOverview",
            select=["AgeGroup", "AgeGroupDescription", "CurrencyCode", "AmountReceivable", "AmountPayable"],
            limit=100,
            division=div,
        )
        return {
            "profit_and_loss": pnl[0] if pnl else None,
            "outstanding_invoices": outstanding,
            "aging": aging,
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

    async def _aging(resource, limit, division) -> list[dict]:
        client = await get_client()
        return await client.list(
            resource, select=AGING_FIELDS, orderby="TotalAmount desc", limit=limit, division=division
        )

    @tool
    async def receivables_aging(limit: Limit = 100, division: Division = None) -> list[dict]:
        """Outstanding receivables per customer, split into Exact's age buckets."""
        return await _aging("read/financial/AgingReceivablesList", limit, division)

    @tool
    async def payables_aging(limit: Limit = 100, division: Division = None) -> list[dict]:
        """Outstanding payables per supplier, split into Exact's age buckets."""
        return await _aging("read/financial/AgingPayablesList", limit, division)

    @tool
    async def customer_payment_behaviour(
        account_id: Annotated[str | None, Field(description="Only this customer (GUID).")] = None,
        invoice_date_from: Annotated[
            date | None, Field(description="Invoices from this date. Default: a year ago.")
        ] = None,
        limit: Limit = 25,
        division: Division = None,
    ) -> dict:
        """How fast customers pay, slowest first: per customer, the invoices since
        invoice_date_from that are paid in full, the average days from invoice to last payment
        and past the due date, and the share paid late."""
        since = invoice_date_from or date.today() - timedelta(days=365)
        # Status 50 is matched: settled against a payment or another open item.
        filters = [f"InvoiceDate ge {odata.datetime_literal(since)}", "Status eq 50"]
        if account_id:
            filters.append(f"Account eq {odata.guid(account_id)}")
        client = await get_client()
        rows = await client.list(
            "bulk/Cashflow/Receivables",
            select=[
                "Account",
                "AccountCode",
                "AccountName",
                "InvoiceDate",
                "DueDate",
                "LastPaymentDate",
                "IsFullyPaid",
                "TransactionIsReversal",
            ],
            filters=filters,
            limit=MAX_TRANSACTION_LINES,
            division=division,
        )
        customers: dict[str, dict] = {}
        for row in rows:
            if not row["IsFullyPaid"] or row["TransactionIsReversal"] or not row["LastPaymentDate"]:
                continue
            invoiced = date.fromisoformat(row["InvoiceDate"][:10])
            paid = date.fromisoformat(row["LastPaymentDate"][:10])
            due = date.fromisoformat(row["DueDate"][:10]) if row["DueDate"] else invoiced
            customer = customers.setdefault(
                row["Account"],
                {"account_id": row["Account"], "code": row["AccountCode"], "name": row["AccountName"], "days": []},
            )
            customer["days"].append(((paid - invoiced).days, (paid - due).days))
        summary = []
        for customer in customers.values():
            days = customer.pop("days")
            summary.append(
                {
                    **customer,
                    "paid_invoices": len(days),
                    "average_days_to_pay": round(sum(to_pay for to_pay, _ in days) / len(days), 1),
                    "average_days_late": round(sum(late for _, late in days) / len(days), 1),
                    "paid_late_share": round(sum(1 for _, late in days if late > 0) / len(days), 2),
                }
            )
        summary.sort(key=lambda c: c["average_days_late"], reverse=True)
        return {
            "since": since.isoformat(),
            "complete": len(rows) < MAX_TRANSACTION_LINES,
            "customers": summary[:limit],
        }

    @tool
    async def search_sales_invoices(
        account_id: Annotated[str | None, Field(description="Only invoices to this customer (GUID).")] = None,
        status: Literal["draft", "open", "processed"] | None = None,
        invoice_date_from: date | None = None,
        invoice_date_to: date | None = None,
        limit: Limit = 25,
        division: Division = None,
    ) -> list[dict]:
        """Sales invoices made in Exact's invoicing module, newest first. Amounts are in the
        division's currency (AmountDC). Sales booked another way, such as from another invoicing
        system, are only in search_sales_entries."""
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

    async def _entries(resource, party, account_id, date_from, date_to, limit, division) -> list[dict]:
        filters = _date_range("EntryDate", date_from, date_to)
        if account_id:
            filters.append(f"{party} eq {odata.guid(account_id)}")
        client = await get_client()
        return await client.list(
            resource,
            select=[party, f"{party}Name", *ENTRY_FIELDS],
            filters=filters,
            orderby="EntryDate desc",
            limit=limit,
            division=division,
        )

    @tool
    async def search_sales_entries(
        account_id: Annotated[str | None, Field(description="Only entries for this customer (GUID).")] = None,
        invoice_date_from: date | None = None,
        invoice_date_to: date | None = None,
        limit: Limit = 25,
        division: Division = None,
    ) -> list[dict]:
        """Booked sales invoices and credit notes (Type 21) in the sales journal, newest first.
        Unlike search_sales_invoices, this includes invoices made outside Exact's invoicing module."""
        return await _entries(
            "salesentry/SalesEntries", "Customer", account_id, invoice_date_from, invoice_date_to, limit, division
        )

    @tool
    async def search_sales_orders(
        account_id: Annotated[str | None, Field(description="Only orders from this customer (GUID).")] = None,
        status: Literal["open", "partial", "complete", "cancelled"] | None = None,
        order_date_from: date | None = None,
        order_date_to: date | None = None,
        limit: Limit = 25,
        division: Division = None,
    ) -> list[dict]:
        """Sales orders, newest first. Only divisions with Exact's Wholesale & Distribution or
        Manufacturing package have them."""
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
    async def search_purchase_invoices(
        account_id: Annotated[str | None, Field(description="Only invoices from this supplier (GUID).")] = None,
        invoice_date_from: date | None = None,
        invoice_date_to: date | None = None,
        limit: Limit = 25,
        division: Division = None,
    ) -> list[dict]:
        """Booked purchase invoices and credit notes (Type 31), newest first."""
        return await _entries(
            "purchaseentry/PurchaseEntries", "Supplier", account_id, invoice_date_from, invoice_date_to, limit, division
        )

    @tool
    async def get_purchase_invoice(
        entry_id: Annotated[str, Field(description="EntryID (GUID) from search_purchase_invoices.")],
        division: Division = None,
    ) -> dict:
        """One booked purchase invoice with its lines, each with the GL account it is booked on."""
        client = await get_client()
        div = await client.division(division)
        rows = await client.list(
            "purchaseentry/PurchaseEntries",
            select=["Supplier", "SupplierName", *ENTRY_FIELDS, "PaymentConditionDescription"],
            filters=[f"EntryID eq {odata.guid(entry_id)}"],
            limit=1,
            division=div,
        )
        if not rows:
            raise ToolError(f"No purchase invoice {entry_id} in this division.")
        lines = await client.list(
            "purchaseentry/PurchaseEntryLines",
            select=[
                "LineNumber",
                "GLAccountCode",
                "GLAccountDescription",
                "Description",
                "Quantity",
                "AmountDC",
                "VATAmountDC",
                "VATCode",
                "CostCenter",
                "ProjectDescription",
            ],
            filters=[f"EntryID eq {odata.guid(entry_id)}"],
            orderby="LineNumber",
            limit=500,
            division=div,
        )
        return {**rows[0], "lines": lines}

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
    async def search_gl_accounts(
        query: Annotated[str | None, Field(description="Part of the GL account code or description.")] = None,
        limit: Limit = 100,
        division: Division = None,
    ) -> list[dict]:
        """The chart of accounts (general ledger accounts), by code. BalanceType B is the balance
        sheet and W profit and loss; TypeDescription says what an account is for, such as Bank."""
        client = await get_client()
        accounts = await _gl_accounts(client, division)
        if query:
            q = query.lower()
            accounts = [a for a in accounts if q in (a["Code"] or "").lower() or q in (a["Description"] or "").lower()]
        return sorted(accounts, key=lambda a: a["Code"] or "")[:limit]

    @tool
    async def search_transactions(
        gl_account_code: Annotated[
            str | None, Field(description="Only lines on this GL account code, such as one from profit_and_loss.")
        ] = None,
        account_id: Annotated[str | None, Field(description="Only lines for this customer or supplier (GUID).")] = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: Limit = 100,
        division: Division = None,
    ) -> dict:
        """General ledger transaction lines, newest first. The total and count cover every matching
        line, not only the ones returned, so they can be checked against profit_and_loss. AmountDC is
        debit-positive. If `complete` is false there were too many lines to read; narrow the filters."""
        filters = _date_range("Date", date_from, date_to)
        if account_id:
            filters.append(f"Account eq {odata.guid(account_id)}")
        client = await get_client()
        div = await client.division(division)
        if gl_account_code:
            code = gl_account_code.strip()
            ids = {a["Code"]: a["ID"] for a in await _gl_accounts(client, div)}
            if code not in ids:
                raise ToolError(f"No GL account {code} in this division; see search_gl_accounts.")
            # By ID, because the docs don't say whether GLAccountCode is space-padded like account codes.
            filters.append(f"GLAccount eq {odata.guid(ids[code])}")
        # The bulk endpoint isn't documented to sort, so read every match and sort here.
        rows = await client.list(
            "bulk/Financial/TransactionLines",
            select=TRANSACTION_FIELDS,
            filters=filters,
            limit=MAX_TRANSACTION_LINES,
            division=div,
        )
        rows.sort(key=lambda row: row["Date"] or "", reverse=True)
        return {
            "total_amount_dc": round(sum(row["AmountDC"] or 0 for row in rows), 2),
            "line_count": len(rows),
            "complete": len(rows) < MAX_TRANSACTION_LINES,
            "lines": rows[:limit],
        }

    @tool
    async def search_bank_transactions(
        query: Annotated[str | None, Field(description="Part of the line description.")] = None,
        account_id: Annotated[str | None, Field(description="Only lines for this customer or supplier (GUID).")] = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: Limit = 25,
        division: Division = None,
    ) -> list[dict]:
        """Bank statement lines from every bank account, newest first. AmountDC is positive for
        money in and negative for money out; GLAccountCode is what the line was booked against."""
        filters = _date_range("Date", date_from, date_to)
        if query:
            filters.append(f"substringof({odata.string(query)}, Description) eq true")
        if account_id:
            filters.append(f"Account eq {odata.guid(account_id)}")
        client = await get_client()
        return await client.list(
            "financialtransaction/BankEntryLines",
            select=BANK_LINE_FIELDS,
            filters=filters,
            orderby="Date desc",
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
        """Profit and loss per general ledger account over a range of financial periods (see
        financial_periods). Exact amounts are debit-positive, so revenue shows negative; `result`
        flips that (positive = profit)."""
        if period_from > period_to:
            raise ToolError("period_from must not be after period_to.")
        client = await get_client()
        accounts = await _gl_totals(client, division, year, period_from, period_to, "W")
        lines = sorted(accounts.values(), key=lambda a: a["code"])
        for line in lines:
            line["amount"] = round(line["amount"], 2)
        return {
            "year": year,
            "periods": [period_from, period_to],
            "result": round(-sum(a["amount"] for a in lines), 2),
            "accounts": lines,
        }

    @tool
    async def balance_sheet(
        year: Annotated[
            int | None,
            Field(ge=1990, le=2100, description="Financial year, the current or the previous one. Default: current."),
        ] = None,
        period: Annotated[
            int | None,
            Field(ge=1, le=12, description="Balances at the end of this period. Default: the current or last one."),
        ] = None,
        division: Division = None,
    ) -> dict:
        """Balance-sheet accounts at the end of a financial period: Exact's opening balance for the
        year plus the movements since. Amounts are debit-positive, so assets show positive and
        liabilities and equity negative. Exact only has opening balances for the current and the
        previous financial year. During a year the accounts don't net to zero: `total` is the
        result not yet booked to equity (positive = profit)."""
        client = await get_client()
        div = await client.division(division)
        periods = await _recent_periods(client, div)
        current_year, current_period = _period_on(periods, date.today())
        year = current_year if year is None else year
        if year == current_year:
            which, last = "CurrentYear", current_period
        elif year == current_year - 1:
            which = "PreviousYear"
            last = max((p["FinPeriod"] for p in periods if p["FinYear"] == year), default=None)
            if last is None:
                raise ToolError(f"Financial year {year} has no periods in this division.")
        else:
            raise ToolError(
                f"Exact only has opening balances for financial years {current_year - 1} and {current_year}."
            )
        period = last if period is None else period
        lines = await _balances(client, div, which, year, period)
        return {
            "year": year,
            "period": period,
            "total": round(sum(line["closing"] for line in lines), 2),
            "accounts": lines,
        }

    @tool
    async def cash_position(division: Division = None) -> dict:
        """The booked balance of every cash, bank, credit card and payment provider account up to
        the current financial period, with the total. Debit-positive, so money held is positive."""
        client = await get_client()
        div = await client.division(division)
        cash = [account for account in await _gl_accounts(client, div) if account["Type"] in CASH_TYPES]
        if not cash:
            return {"total": 0.0, "accounts": []}
        year, period = _period_on(await _recent_periods(client, div), date.today())
        ids = " or ".join(f"GLAccount eq {odata.guid(account['ID'])}" for account in cash)
        lines = await _balances(client, div, "CurrentYear", year, period, [f"({ids})"])
        return {
            "year": year,
            "period": period,
            "total": round(sum(line["closing"] for line in lines), 2),
            "accounts": lines,
        }

    @tool
    async def budget_vs_actual(
        year: Annotated[int, Field(ge=1990, le=2100, description="Financial year.")],
        period_from: Annotated[int, Field(ge=1, le=12)] = 1,
        period_to: Annotated[int, Field(ge=1, le=12)] = 12,
        scenario: Annotated[
            str | None, Field(description="Budget scenario code. Omit when the year has only one.")
        ] = None,
        division: Division = None,
    ) -> dict:
        """Budget against actual per profit-and-loss GL account over a range of financial periods.
        Both are debit-positive like profit_and_loss, and variance is actual minus budget; the two
        results flip the sign (positive = profit)."""
        if period_from > period_to:
            raise ToolError("period_from must not be after period_to.")
        client = await get_client()
        div = await client.division(division)
        rows = await client.list(
            "budget/Budgets",
            select=[
                "BudgetScenarioCode",
                "BudgetScenarioDescription",
                "GLAccountCode",
                "GLAccountDescription",
                "AmountDC",
            ],
            filters=[
                f"ReportingYear eq {year}",
                f"ReportingPeriod ge {period_from}",
                f"ReportingPeriod le {period_to}",
            ],
            limit=10_000,
            division=div,
        )
        scenarios = {row["BudgetScenarioCode"]: row["BudgetScenarioDescription"] for row in rows}
        if not scenarios:
            raise ToolError(f"No budget for {year} in this division.")
        if scenario is None and len(scenarios) > 1:
            raise ToolError(f"{year} has more than one budget scenario; pick one of {', '.join(sorted(scenarios))}.")
        scenario = next(iter(scenarios)) if scenario is None else scenario.strip()
        if scenario not in scenarios:
            raise ToolError(f"No budget scenario {scenario} for {year}; it has {', '.join(sorted(scenarios))}.")
        budget: dict[str, dict] = {}
        for row in rows:
            if row["BudgetScenarioCode"] == scenario:
                entry = budget.setdefault(
                    row["GLAccountCode"], {"description": row["GLAccountDescription"], "amount": 0.0}
                )
                entry["amount"] += row["AmountDC"] or 0
        actual = await _gl_totals(client, div, year, period_from, period_to, "W")
        lines = []
        for code in sorted(budget.keys() | actual.keys()):
            budgeted = round(budget[code]["amount"], 2) if code in budget else 0.0
            booked = round(actual[code]["amount"], 2) if code in actual else 0.0
            description = (actual.get(code) or budget[code])["description"]
            lines.append(
                {
                    "code": code,
                    "description": description,
                    "budget": budgeted,
                    "actual": booked,
                    "variance": round(booked - budgeted, 2),
                }
            )
        return {
            "year": year,
            "periods": [period_from, period_to],
            "scenario": {"code": scenario, "description": scenarios[scenario]},
            "budgeted_result": round(-sum(line["budget"] for line in lines), 2),
            "actual_result": round(-sum(line["actual"] for line in lines), 2),
            "accounts": lines,
        }

    @tool
    async def revenue_by_period(
        year: Annotated[int, Field(ge=1990, le=2100, description="Financial year.")],
        division: Division = None,
    ) -> dict:
        """Revenue per financial period of one year, from every revenue GL account, in one call.
        Call it for two years to compare them."""
        client = await get_client()
        rows = await client.list(
            "read/financial/RevenueListByYear",
            select=["Period", "Amount"],
            params={"year": str(year)},
            limit=100,
            division=division,
        )
        periods = sorted(
            ({"period": row["Period"], "amount": round(row["Amount"] or 0, 2)} for row in rows),
            key=lambda p: p["period"],
        )
        return {"year": year, "total": round(sum(p["amount"] for p in periods), 2), "periods": periods}

    @tool
    async def financial_periods(
        year: Annotated[int | None, Field(ge=1990, le=2100, description="Only this financial year.")] = None,
        division: Division = None,
    ) -> list[dict]:
        """Start and end date of each financial period. A financial year can differ from the calendar
        year, and periods can be quarters, so use this to turn dates into the year and periods that
        profit_and_loss and revenue_by_period take."""
        client = await get_client()
        return await client.list(
            "financial/FinancialPeriods",
            select=["FinYear", "FinPeriod", "StartDate", "EndDate"],
            filters=[f"FinYear eq {year}"] if year is not None else [],
            orderby="FinYear,FinPeriod",
            limit=1000,
            division=division,
        )

    @tool
    async def list_journals(
        type: Annotated[
            Literal["cash", "bank", "payment_service", "sales", "purchase", "general"] | None,
            Field(description="Only journals of this type."),
        ] = None,
        division: Division = None,
    ) -> list[dict]:
        """The journals entries are booked in, with their code and type. A bank or cash journal's
        GLAccountCode is the GL account its money is on, and other journals' is their suspense
        account."""
        client = await get_client()
        rows = await client.list(
            "financial/Journals",
            select=["Code", "Description", "Type", "Currency", "GLAccountCode", "BankAccountIBAN", "IsBlocked"],
            filters=[f"Type eq {JOURNAL_TYPES[type]}"] if type else [],
            orderby="Code",
            limit=1000,
            division=division,
        )
        names = {code: name for name, code in JOURNAL_TYPES.items()}
        for row in rows:
            row["TypeDescription"] = names.get(row["Type"], str(row["Type"]))
        return rows

    @tool
    async def list_vat_codes(
        use: Annotated[
            Literal["sales", "purchase"] | None, Field(description="Only codes that can be used for this.")
        ] = None,
        include_blocked: bool = False,
        division: Division = None,
    ) -> list[dict]:
        """VAT codes with their percentage (a fraction, so 0.21 is 21%) and Type: E is excluding VAT,
        I including, B 0% with only the base reported and N no VAT. VATTransactionType says what a
        code may be used for: S sales, P purchase, B both."""
        filters = [] if include_blocked else ["IsBlocked eq false"]
        if use:
            filters.append(f"(VATTransactionType eq '{use[0].upper()}' or VATTransactionType eq 'B')")
        client = await get_client()
        return await client.list(
            "vat/VATCodes",
            select=VAT_CODE_FIELDS,
            filters=filters,
            orderby="Code",
            limit=1000,
            division=division,
        )

    @tool
    async def tax_returns(
        year: Annotated[int | None, Field(ge=1990, le=2100, description="Only returns for this year.")] = None,
        division: Division = None,
    ) -> list[dict]:
        """VAT returns, EC sales lists and other tax returns with their period, due date, amount and
        status, latest due date first. DocumentViewUrl opens the return in Exact."""
        client = await get_client()
        rows = await client.list(
            "read/financial/Returns",
            select=[
                "Type",
                "Description",
                "Year",
                "Period",
                "PeriodDescription",
                "Frequency",
                "DueDate",
                "Amount",
                "Currency",
                "Status",
                "DocumentViewUrl",
            ],
            filters=[f"Year eq {year}"] if year is not None else [],
            orderby="DueDate desc",
            limit=500,
            division=division,
        )
        for row in rows:
            row["TypeDescription"] = RETURN_TYPES.get(row["Type"], str(row["Type"]))
            row["StatusDescription"] = RETURN_STATUS.get(row["Status"], str(row["Status"]))
        return rows
