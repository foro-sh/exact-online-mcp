"""Projects, their hours, result and billing, for divisions with Exact's project module."""

from datetime import date
from typing import Annotated

from fastmcp.exceptions import ToolError
from pydantic import Field

from .. import odata
from ..tools import MAX_TRANSACTION_LINES, Division, Limit, _date_range, _gl_accounts

ProjectId = Annotated[str, Field(description="Project ID (GUID) from search_projects.")]
# TimeTransactions isn't a bulk endpoint, so this is at most 34 pages.
MAX_HOUR_LINES = 2000
HOUR_STATUS_FINAL = 20
INVOICE_TERM_STATUS = {10: "Draft", 20: "Open", 30: "Processing", 50: "Processed"}


def register(tool, get_client) -> None:
    @tool
    async def search_projects(
        query: Annotated[str | None, Field(description="Part of the project code or description.")] = None,
        account_id: Annotated[str | None, Field(description="Only projects for this customer (GUID).")] = None,
        limit: Limit = 25,
        division: Division = None,
    ) -> list[dict]:
        """Projects with their type (fixed price, time and material, non-billable, prepaid or
        campaign), customer, start and end date, and budget."""
        filters = []
        if query:
            q = odata.string(query)
            filters.append(f"(substringof({q}, Code) eq true or substringof({q}, Description) eq true)")
        if account_id:
            filters.append(f"Account eq {odata.guid(account_id)}")
        client = await get_client()
        return await client.list(
            "project/Projects",
            select=[
                "ID",
                "Code",
                "Description",
                "TypeDescription",
                "AccountCode",
                "AccountName",
                "StartDate",
                "EndDate",
                "BudgetedAmount",
                "BudgetedCosts",
                "BudgetedRevenue",
                "SalesTimeQuantity",
                "ClassificationDescription",
            ],
            filters=filters,
            orderby="Code",
            limit=limit,
            division=division,
        )

    @tool
    async def project_hours(
        project_id: ProjectId,
        date_from: date | None = None,
        date_to: date | None = None,
        division: Division = None,
    ) -> dict:
        """Hours booked on a project per hour type, how many are final (approved), and their value
        at the hour price. Not broken down per person."""
        filters = [f"Project eq {odata.guid(project_id)}", *_date_range("Date", date_from, date_to)]
        client = await get_client()
        rows = await client.list(
            "project/TimeTransactions",
            select=["ItemDescription", "Quantity", "AmountFC", "Currency", "HourStatus"],
            filters=filters,
            limit=MAX_HOUR_LINES,
            division=division,
        )
        hour_types: dict[str, dict] = {}
        for row in rows:
            entry = hour_types.setdefault(
                row["ItemDescription"],
                {"hour_type": row["ItemDescription"], "hours": 0.0, "final_hours": 0.0, "amount": 0.0},
            )
            entry["hours"] += row["Quantity"] or 0
            if row["HourStatus"] == HOUR_STATUS_FINAL:
                entry["final_hours"] += row["Quantity"] or 0
            entry["amount"] += row["AmountFC"] or 0
        lines = sorted(hour_types.values(), key=lambda entry: entry["hours"], reverse=True)
        for line in lines:
            for field in ("hours", "final_hours", "amount"):
                line[field] = round(line[field], 2)
        return {
            "hours": round(sum(line["hours"] for line in lines), 2),
            "final_hours": round(sum(line["final_hours"] for line in lines), 2),
            "currencies": sorted({row["Currency"] for row in rows if row["Currency"]}),
            "hour_types": lines,
            "complete": len(rows) < MAX_HOUR_LINES,
        }

    @tool
    async def project_result(project_id: ProjectId, division: Division = None) -> dict:
        """A project's result from the general ledger: the amount booked on the project per
        profit-and-loss GL account, debit-positive, and the result (positive = profit), next to
        the project's budgeted revenue and costs."""
        client = await get_client()
        div = await client.division(division)
        projects = await client.list(
            "project/Projects",
            select=["Code", "Description", "BudgetedRevenue", "BudgetedCosts"],
            filters=[f"ID eq {odata.guid(project_id)}"],
            limit=1,
            division=div,
        )
        if not projects:
            raise ToolError(f"No project {project_id} in this division.")
        profit_and_loss = {
            account["Code"] for account in await _gl_accounts(client, div) if account["BalanceType"] == "W"
        }
        rows = await client.list(
            "bulk/Financial/TransactionLines",
            select=["GLAccountCode", "GLAccountDescription", "AmountDC"],
            filters=[f"Project eq {odata.guid(project_id)}"],
            limit=MAX_TRANSACTION_LINES,
            division=div,
        )
        accounts: dict[str, dict] = {}
        for row in rows:
            if row["GLAccountCode"] in profit_and_loss:
                entry = accounts.setdefault(
                    row["GLAccountCode"],
                    {"code": row["GLAccountCode"], "description": row["GLAccountDescription"], "amount": 0.0},
                )
                entry["amount"] += row["AmountDC"] or 0
        lines = sorted(accounts.values(), key=lambda a: a["code"])
        for line in lines:
            line["amount"] = round(line["amount"], 2)
        return {
            **projects[0],
            "result": round(-sum(line["amount"] for line in lines), 2),
            "accounts": lines,
            "complete": len(rows) < MAX_TRANSACTION_LINES,
        }

    @tool
    async def upcoming_project_billing(
        project_id: Annotated[str | None, Field(description="Only this project (GUID).")] = None,
        invoice_date_to: date | None = None,
        limit: Limit = 50,
        division: Division = None,
    ) -> list[dict]:
        """Invoice terms of fixed-price and prepaid projects that aren't invoiced yet (draft or
        open), by invoice date."""
        filters = ["InvoiceStatus lt 30", *_date_range("InvoiceDate", None, invoice_date_to)]
        if project_id:
            filters.append(f"Project eq {odata.guid(project_id)}")
        client = await get_client()
        rows = await client.list(
            "project/InvoiceTerms",
            select=[
                "Project",
                "ProjectDescription",
                "Description",
                "Amount",
                "Percentage",
                "InvoiceDate",
                "ExecutionFromDate",
                "ExecutionToDate",
                "InvoiceStatus",
                "ItemDescription",
            ],
            filters=filters,
            orderby="InvoiceDate",
            limit=limit,
            division=division,
        )
        for row in rows:
            row["InvoiceStatusDescription"] = INVOICE_TERM_STATUS.get(row["InvoiceStatus"])
        return rows
