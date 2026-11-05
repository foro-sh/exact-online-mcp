"""What changed in the books since a date."""

from collections import defaultdict
from datetime import date
from typing import Annotated

from pydantic import Field

from .. import odata
from ..tools import MAX_TRANSACTION_LINES, Division, Limit

# sync/Deleted's entity types as Exact documents them, without the HR and payroll ones and the
# bank accounts, which can be employees'.
DELETED_TYPES = {
    1: "TransactionLines",
    2: "Accounts",
    3: "Addresses",
    4: "Attachments",
    5: "Contacts",
    6: "Documents",
    7: "GLAccounts",
    8: "ItemPrices",
    9: "Items",
    10: "PaymentTerms",
    12: "SalesOrders",
    13: "SalesInvoices",
    14: "TimeCostTransactions",
    15: "StockPositions",
    16: "GoodsDeliveries",
    17: "GoodsDeliveryLines",
    18: "GLClassifications",
    19: "ItemWarehouses",
    20: "StorageLocationStockPositions",
    21: "Projects",
    22: "PurchaseOrders",
    23: "Subscriptions",
    24: "SubscriptionLines",
    25: "ProjectWBS",
    26: "ProjectPlanning",
    28: "SerialBatchNumbers",
    29: "StockSerialBatchNumbers",
    30: "ItemAccounts",
    31: "DiscountTables",
    32: "SalesOrderHeaders",
    33: "SalesOrderLines",
    34: "QuotationHeaders",
    35: "QuotationLines",
    36: "ShopOrders",
    37: "ShopOrderMaterialPlans",
    38: "ShopOrderRoutingStepPlans",
    41: "ItemStorageLocations",
    50: "ShopOrderPurchasePlanning",
    51: "ShopOrderSubOrders",
    53: "RequirementIssues",
    54: "BillOfMaterialMaterials",
    55: "BillOfMaterialVersions",
    60: "PurchaseEntries",
    61: "PurchaseEntryLines",
    62: "SalesEntries",
    63: "SalesEntryLines",
}


def register(tool, get_client) -> None:
    @tool
    async def recent_changes(
        since: Annotated[date, Field(description="Changes on or after this day. Deletions go back two months.")],
        limit: Limit = 50,
        division: Division = None,
    ) -> dict:
        """What changed in the books since a day: how many transaction lines were created or
        changed per journal, the latest of them, and how many records of each kind were deleted."""
        client = await get_client()
        div = await client.division(division)
        lines = await client.list(
            "bulk/Financial/TransactionLines",
            select=[
                "Modified",
                "Date",
                "JournalCode",
                "JournalDescription",
                "EntryNumber",
                "GLAccountCode",
                "AccountName",
                "Description",
                "AmountDC",
            ],
            filters=[f"Modified ge {odata.datetime_literal(since)}"],
            limit=MAX_TRANSACTION_LINES,
            division=div,
        )
        journals: dict[str, dict] = {}
        for line in lines:
            journal = journals.setdefault(
                line["JournalCode"],
                {"journal": line["JournalCode"], "description": line["JournalDescription"], "lines": 0},
            )
            journal["lines"] += 1
        lines.sort(key=lambda line: line["Modified"] or "", reverse=True)
        # Exact keeps deletions for two months and only filters them on its row version.
        deleted = await client.list(
            "sync/Deleted",
            select=["EntityType", "DeletedDate"],
            filters=["Timestamp gt 1"],
            limit=MAX_TRANSACTION_LINES,
            division=div,
        )
        deletions: dict[str, int] = defaultdict(int)
        for row in deleted:
            if row["EntityType"] in DELETED_TYPES and (row["DeletedDate"] or "")[:10] >= since.isoformat():
                deletions[DELETED_TYPES[row["EntityType"]]] += 1
        return {
            "changed_lines": len(lines),
            "changed_lines_per_journal": sorted(journals.values(), key=lambda j: j["lines"], reverse=True),
            "latest_changed_lines": lines[:limit],
            "deleted": dict(sorted(deletions.items())),
            "complete": len(lines) < MAX_TRANSACTION_LINES and len(deleted) < MAX_TRANSACTION_LINES,
        }
