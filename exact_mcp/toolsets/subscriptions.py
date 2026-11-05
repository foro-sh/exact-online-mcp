"""Recurring revenue from Exact subscriptions."""

from collections import defaultdict
from datetime import date
from typing import Annotated

from pydantic import Field

from .. import odata
from ..tools import Division, Limit

# Months in one unit of a subscription type's invoice period.
MONTHS = {"wk": 12 / 52, "mm": 1, "qt": 3, "hy": 6, "yy": 12}
# Neither endpoint is bulk, so each read is at most 34 pages.
MAX_ROWS = 2000


def _covers(start: str | None, end: str | None, day: str) -> bool:
    return (start is None or start[:10] <= day) and (end is None or end[:10] >= day)


def register(tool, get_client) -> None:
    @tool
    async def subscription_mrr(
        as_of: Annotated[date | None, Field(description="The day to measure. Default: today.")] = None,
        limit: Limit = 25,
        division: Division = None,
    ) -> dict:
        """Monthly recurring revenue from the Exact subscriptions running on a day: each running
        line's amount divided by its subscription type's invoice period in months, with ARR and the
        largest subscriptions. It takes a line's AmountDC as its amount per invoice period."""
        day = as_of or date.today()
        on = day.isoformat()
        client = await get_client()
        div = await client.division(division)
        subscriptions = await client.list(
            "subscription/Subscriptions",
            select=[
                "EntryID",
                "Description",
                "OrderedByName",
                "SubscriptionType",
                "SubscriptionTypeDescription",
                "StartDate",
                "EndDate",
            ],
            filters=[f"StartDate le {odata.datetime_literal(day)}"],
            limit=MAX_ROWS,
            division=div,
        )
        running = {row["EntryID"]: row for row in subscriptions if _covers(row["StartDate"], row["EndDate"], on)}
        types = await client.list(
            "subscription/SubscriptionTypes",
            select=["ID", "InvoicePeriod", "InvoicePeriodUnit"],
            limit=1000,
            division=div,
        )
        months = {row["ID"]: (row["InvoicePeriod"] or 0) * MONTHS.get(row["InvoicePeriodUnit"], 0) for row in types}
        lines = await client.list(
            "subscription/SubscriptionLines",
            select=["EntryID", "AmountDC", "FromDate", "ToDate"],
            limit=MAX_ROWS,
            division=div,
        )
        monthly: dict[str, float] = defaultdict(float)
        unknown_period = set()
        for line in lines:
            subscription = running.get(line["EntryID"])
            if subscription is None or not _covers(line["FromDate"], line["ToDate"], on):
                continue
            period = months.get(subscription["SubscriptionType"])
            if not period:
                unknown_period.add(line["EntryID"])
                continue
            monthly[line["EntryID"]] += (line["AmountDC"] or 0) / period
        mrr = round(sum(monthly.values()), 2)
        largest = sorted(monthly.items(), key=lambda item: item[1], reverse=True)[:limit]
        return {
            "as_of": on,
            "mrr": mrr,
            "arr": round(mrr * 12, 2),
            "subscriptions": len(monthly),
            "without_a_known_invoice_period": len(unknown_period),
            "largest": [
                {
                    "customer": running[entry]["OrderedByName"],
                    "description": running[entry]["Description"],
                    "type": running[entry]["SubscriptionTypeDescription"],
                    "monthly_amount": round(amount, 2),
                }
                for entry, amount in largest
            ],
            "complete": len(subscriptions) < MAX_ROWS and len(lines) < MAX_ROWS,
        }
