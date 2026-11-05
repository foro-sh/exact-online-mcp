"""Documents in Exact, such as scanned purchase invoices, and their files."""

import base64
import mimetypes
from datetime import date
from typing import Annotated

from fastmcp.exceptions import ToolError
from fastmcp.utilities.types import File, Image
from pydantic import Field

from .. import odata
from ..tools import Division, Limit, _date_range

# Files go into the agent's context, so a document's attachments can't be bigger than this.
MAX_ATTACHMENT_BYTES = 10_000_000


def _file(name: str, data: bytes) -> File | Image:
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    kind, subtype = mime.split("/", 1)
    return Image(data=data, format=subtype) if kind == "image" else File(data=data, name=name, format=subtype)


def register(tool, get_client) -> None:
    @tool
    async def search_documents(
        account_id: Annotated[
            str | None, Field(description="Only documents of this customer or supplier (GUID).")
        ] = None,
        entry_id: Annotated[
            str | None,
            Field(description="Only documents of this booked invoice: its EntryID from search_purchase_invoices."),
        ] = None,
        query: Annotated[str | None, Field(description="Part of the document's subject.")] = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: Limit = 25,
        division: Division = None,
    ) -> list[dict]:
        """Documents in Exact, such as scanned purchase invoices and sent sales invoices, newest
        first. Read one's files with get_document."""
        filters = _date_range("DocumentDate", date_from, date_to)
        if account_id:
            filters.append(f"Account eq {odata.guid(account_id)}")
        if entry_id:
            filters.append(f"FinancialTransactionEntryID eq {odata.guid(entry_id)}")
        if query:
            filters.append(f"substringof({odata.string(query)}, Subject) eq true")
        client = await get_client()
        return await client.list(
            "documents/Documents",
            select=[
                "ID",
                "Subject",
                "TypeDescription",
                "DocumentDate",
                "AccountName",
                "FinancialTransactionEntryID",
                "SalesInvoiceNumber",
            ],
            filters=filters,
            orderby="DocumentDate desc",
            limit=limit,
            division=division,
        )

    @tool
    async def get_document(
        document_id: Annotated[
            str,
            Field(description="Document ID (GUID) from search_documents, or the Document of a booked invoice."),
        ],
        division: Division = None,
    ) -> list:
        """A document's attachments as files the agent can read, such as an invoice's PDF."""
        client = await get_client()
        div = await client.division(division)
        filters = [f"Document eq {odata.guid(document_id)}"]
        attachments = await client.list(
            "documents/DocumentAttachments",
            select=["ID", "FileName", "FileSize", "Url"],
            filters=filters,
            limit=50,
            division=div,
        )
        if not attachments:
            raise ToolError(f"Document {document_id} has no attachments in this division.")
        if sum(row["FileSize"] or 0 for row in attachments) > MAX_ATTACHMENT_BYTES:
            return [{"attachments": attachments, "note": "Too large to read here; open the Url in Exact."}]
        # A second call, so the size is known before the files come.
        contents = await client.list(
            "documents/DocumentAttachments", select=["ID", "Attachment"], filters=filters, limit=50, division=div
        )
        data = {row["ID"]: base64.b64decode(row["Attachment"] or "") for row in contents}
        return [
            {"attachments": attachments},
            *(_file(row["FileName"] or "attachment", data.get(row["ID"], b"")) for row in attachments),
        ]
