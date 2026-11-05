"""Sales and purchase orders and stock, for divisions with Exact's Wholesale & Distribution or
Manufacturing package."""

from datetime import date
from typing import Annotated, Literal

from fastmcp.exceptions import ToolError
from pydantic import Field

from .. import odata
from ..tools import ORDER_FIELDS, Division, Limit, _date_range

PURCHASE_ORDER_STATUS = {"open": 10, "partial": 20, "complete": 30, "cancelled": 40}
# PurchaseOrders has no descriptions for its status codes.
PURCHASE_ORDER_STATUS_NAMES = {code: name for name, code in PURCHASE_ORDER_STATUS.items()}
# ItemWarehouses isn't a bulk endpoint, so checking reorder points reads at most 17 pages.
MAX_STOCK_ROWS = 1000


def register(tool, get_client) -> None:
    @tool
    async def get_sales_order(
        order_id: Annotated[str, Field(description="OrderID (GUID) from search_sales_orders.")],
        division: Division = None,
    ) -> dict:
        """One sales order with its lines, including how much of each line is delivered and
        invoiced."""
        client = await get_client()
        div = await client.division(division)
        rows = await client.list(
            "salesorder/SalesOrders",
            select=ORDER_FIELDS,
            filters=[f"OrderID eq {odata.guid(order_id)}"],
            limit=1,
            division=div,
        )
        if not rows:
            raise ToolError(f"No sales order {order_id} in this division.")
        lines = await client.list(
            "salesorder/SalesOrderLines",
            select=[
                "LineNumber",
                "ItemCode",
                "ItemDescription",
                "Description",
                "Quantity",
                "QuantityDelivered",
                "QuantityInvoiced",
                "UnitPrice",
                "AmountDC",
                "DeliveryDate",
            ],
            filters=[f"OrderID eq {odata.guid(order_id)}"],
            orderby="LineNumber",
            limit=500,
            division=div,
        )
        return {**rows[0], "lines": lines}

    @tool
    async def search_purchase_orders(
        account_id: Annotated[str | None, Field(description="Only orders to this supplier (GUID).")] = None,
        status: Literal["open", "partial", "complete", "cancelled"] | None = None,
        order_date_from: date | None = None,
        order_date_to: date | None = None,
        limit: Limit = 25,
        division: Division = None,
    ) -> list[dict]:
        """Purchase orders, newest first, with how far each is received and invoiced. Professional
        Services (Plus and up) has them too."""
        filters = _date_range("OrderDate", order_date_from, order_date_to)
        if account_id:
            filters.append(f"Supplier eq {odata.guid(account_id)}")
        if status:
            filters.append(f"OrderStatus eq {PURCHASE_ORDER_STATUS[status]}")
        client = await get_client()
        rows = await client.list(
            "purchaseorder/PurchaseOrders",
            select=[
                "PurchaseOrderID",
                "OrderNumber",
                "OrderDate",
                "ReceiptDate",
                "Supplier",
                "SupplierName",
                "AmountDC",
                "Currency",
                "Description",
                "YourRef",
                "OrderStatus",
                "ReceiptStatus",
                "InvoiceStatus",
            ],
            filters=filters,
            orderby="OrderDate desc",
            limit=limit,
            division=division,
        )
        for row in rows:
            for field in ("OrderStatus", "ReceiptStatus", "InvoiceStatus"):
                row[f"{field}Description"] = PURCHASE_ORDER_STATUS_NAMES.get(row[field])
        return rows

    @tool
    async def stock_levels(
        query: Annotated[str | None, Field(description="Part of the item code or description.")] = None,
        below_reorder_point: Annotated[
            bool, Field(description="Only items whose projected stock is below their reorder point.")
        ] = False,
        limit: Limit = 50,
        division: Division = None,
    ) -> dict:
        """Stock per item and warehouse: in stock (including stock committed to sales orders),
        planned in and out, projected, and the reorder point. Plus packages and up only."""
        filters = []
        if query:
            q = odata.string(query)
            filters.append(f"(substringof({q}, ItemCode) eq true or substringof({q}, ItemDescription) eq true)")
        if below_reorder_point:
            filters.append("ReorderPoint gt 0")
        fetch = MAX_STOCK_ROWS if below_reorder_point else limit
        client = await get_client()
        rows = await client.list(
            "inventory/ItemWarehouses",
            select=[
                "ItemCode",
                "ItemDescription",
                "WarehouseCode",
                "WarehouseDescription",
                "CurrentStock",
                "PlannedStockIn",
                "PlannedStockOut",
                "ProjectedStock",
                "ReservedStock",
                "SafetyStock",
                "ReorderPoint",
                "MaximumStock",
            ],
            filters=filters,
            orderby="ItemCode",
            limit=fetch,
            division=division,
        )
        fetched = len(rows)
        if below_reorder_point:
            # The docs don't say Exact can filter one field against another, so compare here.
            rows = [row for row in rows if (row["ProjectedStock"] or 0) < (row["ReorderPoint"] or 0)]
        return {"items": rows[:limit], "truncated": len(rows) > limit or fetched == fetch}
