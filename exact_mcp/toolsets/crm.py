"""Quotations, opportunities and contact people."""

from datetime import date
from typing import Annotated, Literal

from pydantic import Field

from .. import odata
from ..tools import AccountId, Division, Limit, _date_range

QUOTATION_STATUS = {"rejected": 5, "draft": 20, "open": 25, "printed": 40, "accepted": 50}
OPPORTUNITY_STATUS = {"open": 1, "won": 2, "lost": 3}


def register(tool, get_client) -> None:
    @tool
    async def search_quotations(
        account_id: Annotated[str | None, Field(description="Only quotations for this customer (GUID).")] = None,
        status: Literal["draft", "open", "printed", "accepted", "rejected"] | None = None,
        quotation_date_from: date | None = None,
        quotation_date_to: date | None = None,
        limit: Limit = 25,
        division: Division = None,
    ) -> list[dict]:
        """Sales quotations, newest first, each in its latest version."""
        filters = _date_range("QuotationDate", quotation_date_from, quotation_date_to)
        if account_id:
            filters.append(f"OrderAccount eq {odata.guid(account_id)}")
        if status:
            filters.append(f"Status eq {QUOTATION_STATUS[status]}")
        client = await get_client()
        rows = await client.list(
            "crm/Quotations",
            select=[
                "QuotationID",
                "QuotationNumber",
                "VersionNumber",
                "QuotationDate",
                "ClosingDate",
                "DueDate",
                "OrderAccount",
                "OrderAccountName",
                "AmountDC",
                "Currency",
                "Description",
                "YourRef",
                "Status",
                "StatusDescription",
            ],
            filters=filters,
            orderby="QuotationDate desc",
            limit=limit,
            division=division,
        )
        # Exact keeps a row per version of a quotation.
        latest: dict[int, dict] = {}
        for row in rows:
            seen = latest.get(row["QuotationNumber"])
            if seen is None or (row["VersionNumber"] or 0) > (seen["VersionNumber"] or 0):
                latest[row["QuotationNumber"]] = row
        return list(latest.values())

    @tool
    async def search_opportunities(
        status: Literal["open", "won", "lost"] | None = "open",
        account_id: Annotated[str | None, Field(description="Only opportunities with this customer (GUID).")] = None,
        close_date_from: date | None = None,
        close_date_to: date | None = None,
        limit: Limit = 50,
        division: Division = None,
    ) -> dict:
        """Sales opportunities (open ones unless asked otherwise) by expected close date, with
        their stage and probability, and their total amount."""
        filters = _date_range("CloseDate", close_date_from, close_date_to)
        if status:
            filters.append(f"OpportunityStatus eq {OPPORTUNITY_STATUS[status]}")
        if account_id:
            filters.append(f"Account eq {odata.guid(account_id)}")
        client = await get_client()
        rows = await client.list(
            "crm/Opportunities",
            select=[
                "ID",
                "Name",
                "AccountName",
                "AmountDC",
                "CloseDate",
                "Probability",
                "OpportunityStageDescription",
                "OpportunityStatus",
                "LeadSourceDescription",
                "ReasonCodeDescription",
            ],
            filters=filters,
            orderby="CloseDate",
            limit=limit,
            division=division,
        )
        return {
            "total_amount_dc": round(sum(row["AmountDC"] or 0 for row in rows), 2),
            "opportunities": rows,
            "truncated": len(rows) == limit,
        }

    @tool
    async def list_contacts(account_id: AccountId, division: Division = None) -> list[dict]:
        """The contact people at one customer or supplier, with their job title and business email
        and phone; the main contact first."""
        client = await get_client()
        rows = await client.list(
            "crm/Contacts",
            # Contacts also hold private and identity details, which this never selects.
            select=["FullName", "JobTitleDescription", "IsMainContact", "BusinessEmail", "BusinessPhone"],
            filters=[f"Account eq {odata.guid(account_id)}"],
            limit=100,
            division=division,
        )
        return sorted(rows, key=lambda row: not row["IsMainContact"])
