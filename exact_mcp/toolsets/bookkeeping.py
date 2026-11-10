"""Booking in Exact: purchase and sales invoices, journal entries, new customers and suppliers,
documents, and matching open items. The only toolset that writes, so EXACT_TOOLSETS has to name
it; `all` leaves it out."""

import base64
import binascii
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from .. import matching, odata
from ..client import ExactError
from ..tools import JOURNAL_TYPES, VAT_CODE_FIELDS, Division, Limit
from .documents import MAX_ATTACHMENT_BYTES

# Exact books an invoice's total on the receivables or payables account and its VAT on a VAT
# account itself, and money only moves through a cash or bank journal.
CASH_TYPES = {10: "cash", 12: "bank", 14: "credit card", 16: "payment service"}
CONTROL_TYPES = {**CASH_TYPES, 20: "accounts receivable", 22: "accounts payable", 24: "VAT"}
OPEN_ITEM_TYPES = {20: "receivables", 22: "payables"}
# Community-documented: Exact doesn't list its document type codes.
DOCUMENT_TYPES = {"sales_invoice": 10, "purchase_invoice": 20}
# Total of an invoice against expected_total: a cent either way for rounding.
CENT = 0.011

DryRun = Annotated[bool, Field(description="Check everything and return what would be booked, without booking it.")]
CostCenter = Annotated[str | None, Field(description="Cost center code.")]
CostUnit = Annotated[str | None, Field(description="Cost unit code.")]
ProjectId = Annotated[str | None, Field(description="Project ID (GUID).")]


class InvoiceLine(BaseModel):
    gl_account_code: str = Field(description="GL account to book the line on; see suggest_booking.")
    amount: float = Field(description="Amount excluding VAT, in the invoice's currency. Negative on a credit note.")
    vat_code: str | None = Field(
        None, description="VAT code (see list_vat_codes). Default: the GL account's own VAT code."
    )
    vat_amount: float | None = Field(
        None, description="The VAT on the invoice for this line, when it differs from the VAT code's by rounding."
    )
    description: str | None = None
    cost_center: CostCenter = None
    cost_unit: CostUnit = None
    project_id: ProjectId = None


class JournalLine(BaseModel):
    gl_account_code: str
    amount: float = Field(description="Debit positive, credit negative. The lines must add up to zero.")
    description: str | None = None
    account_id: str | None = Field(
        None, description="Customer or supplier (GUID); required on a receivables or payables account."
    )
    cost_center: CostCenter = None
    cost_unit: CostUnit = None
    project_id: ProjectId = None


class OpenItem(BaseModel):
    journal_code: str
    entry_number: int


@dataclass(frozen=True)
class Side:
    resource: str
    lines: str
    party: str
    journal_type: str
    # What a VAT code's VATTransactionType must allow, besides B (both).
    vat_use: str


PURCHASE = Side("purchaseentry/PurchaseEntries", "PurchaseEntryLines", "Supplier", "purchase", "P")
SALES = Side("salesentry/SalesEntries", "SalesEntryLines", "Customer", "sales", "S")


async def _chart(client, division) -> dict[str, dict]:
    rows = await client.list(
        "bulk/Financial/GLAccounts",
        select=["ID", "Code", "Description", "Type", "IsBlocked", "VATCode"],
        limit=10_000,
        division=division,
    )
    return {row["Code"]: row for row in rows}


def _gl_account(chart: dict[str, dict], code: str, refused: dict[int, str]) -> dict:
    account = chart.get(code.strip())
    if account is None:
        raise ValueError(f"No GL account {code} in this division; see search_gl_accounts.")
    name = f"GL account {account['Code']} ({account['Description']})"
    if account["IsBlocked"]:
        raise ValueError(f"{name} is blocked.")
    if account["Type"] in refused:
        raise ValueError(f"{name} is a {refused[account['Type']]} account, which can't be booked on here.")
    return account


async def _journal(client, division, code: str | None, journal_type: str) -> str:
    rows = await client.list(
        "financial/Journals",
        select=["Code", "Description", "IsBlocked"],
        filters=[f"Type eq {JOURNAL_TYPES[journal_type]}"],
        limit=1000,
        division=division,
    )
    usable = {row["Code"]: row for row in rows if not row["IsBlocked"]}
    if code is not None:
        if code.strip() not in usable:
            raise ValueError(f"No open {journal_type} journal {code}; this division has {', '.join(usable) or 'none'}.")
        return code.strip()
    if len(usable) != 1:
        raise ValueError(
            f"This division has {len(usable) or 'no'} open {journal_type} journals"
            f"{' (' + ', '.join(usable) + ')' if usable else ''}; pass journal_code."
        )
    return next(iter(usable))


async def _vat_codes(client, division) -> dict[str, dict]:
    rows = await client.list("vat/VATCodes", select=VAT_CODE_FIELDS, limit=1000, division=division)
    return {row["Code"]: row for row in rows}


def _vat_on_invoice(line: InvoiceLine, vat: dict | None) -> float:
    if line.vat_amount is not None:
        return line.vat_amount
    # Reverse-charged VAT is declared, not invoiced; B and N codes add none.
    if vat is None or vat["Charged"] or vat["Type"] in ("B", "N"):
        return 0.0
    return round(line.amount * (vat["Percentage"] or 0), 2)


def _invoice_lines(lines: list[InvoiceLine], chart, vat_codes, side: Side) -> tuple[list[dict], list[dict]]:
    """Exact's lines for the entry, and a preview of each with the VAT it adds."""
    payload, preview = [], []
    for line in lines:
        account = _gl_account(chart, line.gl_account_code, CONTROL_TYPES)
        code = (line.vat_code or account["VATCode"] or "").strip() or None
        vat = None
        if code is not None:
            vat = vat_codes.get(code)
            if vat is None:
                raise ValueError(f"No VAT code {code} in this division; see list_vat_codes.")
            if vat["IsBlocked"]:
                raise ValueError(f"VAT code {code} ({vat['Description']}) is blocked.")
            if vat["VATTransactionType"] not in (side.vat_use, "B"):
                raise ValueError(f"VAT code {code} ({vat['Description']}) isn't for {side.journal_type} invoices.")
            if vat["Type"] == "I":
                # Whether AmountFC then includes VAT isn't documented for entry lines.
                raise ValueError(
                    f"VAT code {code} ({vat['Description']}) includes VAT in the amount; book the amount "
                    "excluding VAT with an excluding VAT code."
                )
        vat_amount = _vat_on_invoice(line, vat)
        entry = {"GLAccount": account["ID"], "AmountFC": line.amount}
        optional = {
            "VATCode": code,
            "VATAmountFC": line.vat_amount,
            "Description": line.description,
            "CostCenter": line.cost_center,
            "CostUnit": line.cost_unit,
            "Project": odata.parse_guid(line.project_id) if line.project_id else None,
        }
        payload.append({**entry, **{key: value for key, value in optional.items() if value is not None}})
        preview.append(
            {
                "gl_account_code": account["Code"],
                "gl_account_description": account["Description"],
                "description": line.description,
                "amount": line.amount,
                "vat_code": code,
                "vat_percentage": vat["Percentage"] if vat else None,
                "vat_amount": vat_amount,
            }
        )
    return payload, preview


def _totals(preview: list[dict]) -> dict:
    net = round(sum(line["amount"] for line in preview), 2)
    vat = round(sum(line["vat_amount"] for line in preview), 2)
    return {"net": net, "vat": vat, "total": round(net + vat, 2)}


def _created(record: dict, fields: list[str]) -> dict:
    return {field: record.get(field) for field in fields}


def register_prompts(mcp: FastMCP) -> None:
    tags = {"bookkeeping"}

    @mcp.prompt(tags=tags)
    def book_purchase_invoices() -> str:
        """Book the purchase invoices waiting in Exact's documents."""
        return (
            "Book the purchase invoices waiting in Exact Online, one at a time:\n"
            "1. unbooked_documents lists the purchase invoices nobody has booked yet. Read each with "
            "get_document (the documents toolset).\n"
            "2. Find the supplier with search_accounts (role supplier), by name, and check its VAT or "
            "chamber of commerce number with get_account. Only when there is none, create_account.\n"
            "3. suggest_booking shows how this supplier's invoices were booked before: use its GL "
            "account, VAT code and cost center unless the invoice is clearly about something else.\n"
            "4. book_purchase_invoice with dry_run true, the supplier's invoice number, every line "
            "excluding VAT, the document_id and the invoice's total as expected_total. If the total "
            "doesn't add up, fix the lines rather than dropping expected_total.\n"
            "5. Book it without dry_run. Skip and report any invoice you aren't sure of: a supplier "
            "you can't place, a GL account with no history, or a total you can't make add up.\n"
            "Finish with what you booked, the total per GL account, and what you skipped and why."
        )

    @mcp.prompt(tags=tags)
    def reconcile_open_items(side: Literal["receivables", "payables"] = "receivables") -> str:
        """Match payments against the invoices they settle."""
        return (
            f"Reconcile the open {side} in Exact Online:\n"
            f"1. reconciliation_suggestions with side {side} lists sets of open items that settle each "
            "other: a whole account that adds up to zero, a payment that names its invoices, or a "
            "payment with exactly one invoice of its amount.\n"
            "2. Match each `account_settled` and `reference` suggestion with match_open_items. Check an "
            "`amount` suggestion's dates and descriptions first, and leave it if they don't fit.\n"
            "3. Don't match anything in `unmatched`. List those payments with their candidates so "
            "someone can decide.\n"
            "Finish with how many sets you matched, their total, and what is left open."
        )


def register(tool, get_client) -> None:
    @tool
    async def unbooked_documents(
        kind: Literal["purchase_invoice", "sales_invoice"] = "purchase_invoice",
        date_from: date | None = None,
        limit: Limit = 25,
        division: Division = None,
    ) -> list[dict]:
        """Invoice documents in Exact that no entry is booked from yet, oldest first: the purchase
        invoices still to book. Read one's file with get_document (the documents toolset), then
        book it with book_purchase_invoice and its document_id."""
        filters = [f"Type eq {DOCUMENT_TYPES[kind]}", "FinancialTransactionEntryID eq null"]
        if date_from:
            filters.append(f"DocumentDate ge {odata.datetime_literal(date_from)}")
        client = await get_client()
        return await client.list(
            "documents/Documents",
            select=["ID", "Subject", "DocumentDate", "Account", "AccountName", "AmountFC", "Currency", "Created"],
            filters=filters,
            orderby="DocumentDate",
            limit=limit,
            division=division,
        )

    @tool
    async def suggest_booking(
        account_id: Annotated[str, Field(description="Supplier or customer (GUID) from search_accounts.")],
        role: Literal["supplier", "customer"] = "supplier",
        entries: Annotated[int, Field(ge=1, le=40, description="How many recent invoices to learn from.")] = 20,
        division: Division = None,
    ) -> dict:
        """How a supplier's (or customer's) invoices are usually booked: the account's default GL
        account, VAT code and payment condition, its recent invoices, and each combination of GL
        account, VAT code and cost center on their lines, most used first."""
        side = PURCHASE if role == "supplier" else SALES
        kind = "Purchase" if role == "supplier" else "Sales"
        client = await get_client()
        div = await client.division(division)
        accounts = await client.list(
            "crm/Accounts",
            select=["ID", "Code", "Name", f"GLAccount{kind}", f"{kind}VATCode", f"PaymentCondition{kind}"],
            filters=[f"ID eq {odata.guid(account_id)}"],
            limit=1,
            division=div,
        )
        if not accounts:
            raise ValueError(f"No account {account_id} in this division.")
        account = accounts[0]
        default_gl = None
        if account[f"GLAccount{kind}"]:
            gl = await client.list(
                "financial/GLAccounts",
                select=["Code", "Description"],
                filters=[f"ID eq {odata.guid(account[f'GLAccount{kind}'])}"],
                limit=1,
                division=div,
            )
            default_gl = gl[0] if gl else None
        recent = await client.list(
            side.resource,
            select=["EntryID", "EntryDate", "YourRef", "Description", "AmountFC", "Currency"],
            filters=[f"{side.party} eq {odata.guid(account_id)}"],
            orderby="EntryDate desc",
            limit=entries,
            division=div,
        )
        codings: dict[tuple, dict] = {}
        if recent:
            dates = {row["EntryID"]: row["EntryDate"] for row in recent}
            ids = " or ".join(f"EntryID eq {odata.guid(entry_id)}" for entry_id in dates)
            lines = await client.list(
                f"{side.resource.split('/')[0]}/{side.lines}",
                select=[
                    "EntryID",
                    "GLAccountCode",
                    "GLAccountDescription",
                    "VATCode",
                    "CostCenter",
                    "Description",
                    "AmountFC",
                ],
                filters=[f"({ids})"],
                limit=2000,
                division=div,
            )
            for line in lines:
                key = (line["GLAccountCode"], line["VATCode"], line["CostCenter"])
                coding = codings.setdefault(
                    key,
                    {
                        "gl_account_code": line["GLAccountCode"],
                        "gl_account_description": line["GLAccountDescription"],
                        "vat_code": line["VATCode"],
                        "cost_center": line["CostCenter"],
                        "entries": set(),
                        "total": 0.0,
                        "last_used": "",
                        "descriptions": [],
                    },
                )
                coding["entries"].add(line["EntryID"])
                coding["total"] += line["AmountFC"] or 0
                coding["last_used"] = max(coding["last_used"], dates.get(line["EntryID"]) or "")
                descriptions = coding["descriptions"]
                if line["Description"] and len(descriptions) < 3 and line["Description"] not in descriptions:
                    descriptions.append(line["Description"])
        ranked = []
        for coding in codings.values():
            used = len(coding.pop("entries"))
            ranked.append(
                {
                    **coding,
                    "invoices": used,
                    "share": round(used / len(recent), 2),
                    "total": round(coding["total"], 2),
                }
            )
        ranked.sort(key=lambda c: (c["invoices"], c["last_used"]), reverse=True)
        return {
            "account": {"id": account["ID"], "code": account["Code"], "name": account["Name"]},
            "defaults": {
                "gl_account": default_gl,
                "vat_code": account[f"{kind}VATCode"] or None,
                "payment_condition": account[f"PaymentCondition{kind}"] or None,
            },
            "recent_invoices": recent,
            "codings": ranked,
        }

    async def _book_invoice(
        side: Side,
        party_id: str,
        invoice_date: date,
        lines: list[InvoiceLine],
        header: dict,
        duplicate_filters: list[str],
        journal_code: str | None,
        expected_total: float | None,
        dry_run: bool,
        allow_duplicate: bool,
        division: int | None,
    ) -> dict:
        client = await get_client()
        div = await client.division(division)
        party = odata.parse_guid(party_id)
        journal = await _journal(client, div, journal_code, side.journal_type)
        payload, preview = _invoice_lines(lines, await _chart(client, div), await _vat_codes(client, div), side)
        totals = _totals(preview)
        if expected_total is not None and abs(totals["total"] - expected_total) > CENT:
            raise ValueError(
                f"The lines add up to {totals['total']:.2f} ({totals['net']:.2f} plus {totals['vat']:.2f} VAT), "
                f"not the invoice's total of {expected_total:.2f}. Check the amounts and VAT codes, or give a "
                "line the invoice's own vat_amount when it rounds VAT differently."
            )
        if not allow_duplicate:
            existing = await client.list(
                side.resource,
                select=["EntryID", "EntryNumber", "EntryDate", "YourRef", "InvoiceNumber", "AmountFC"],
                filters=duplicate_filters,
                limit=5,
                division=div,
            )
            if existing:
                raise ValueError(
                    f"This invoice looks booked already, as entry {existing[0]['EntryNumber']} of "
                    f"{existing[0]['EntryDate']}. Pass allow_duplicate only if it really is another invoice."
                )
        body = {
            "Journal": journal,
            side.party: party,
            "EntryDate": invoice_date.isoformat(),
            **{key: value for key, value in header.items() if value is not None},
            side.lines: payload,
        }
        result = {"journal": journal, **totals, "lines": preview}
        if dry_run:
            return {"booked": False, **result}
        created = await client.create(side.resource, body, div)
        return {
            "booked": True,
            **_created(created, ["EntryID", "EntryNumber", "InvoiceNumber", "AmountFC", "VATAmountFC", "Status"]),
            **result,
        }

    @tool(writes=True)
    async def book_purchase_invoice(
        supplier_id: Annotated[str, Field(description="Supplier (GUID) from search_accounts or create_account.")],
        supplier_invoice_number: Annotated[
            str, Field(description="The supplier's own invoice number, stored as YourRef. Catches double bookings.")
        ],
        invoice_date: date,
        lines: Annotated[list[InvoiceLine], Field(min_length=1)],
        expected_total: Annotated[
            float | None,
            Field(description="The invoice's total including VAT. Nothing is booked unless the lines add up to it."),
        ] = None,
        due_date: date | None = None,
        description: str | None = None,
        currency: Annotated[str | None, Field(description="Currency code. Default: the journal's.")] = None,
        payment_reference: Annotated[str | None, Field(description="The reference to pay the invoice with.")] = None,
        document_id: Annotated[
            str | None, Field(description="The invoice's document (GUID), from unbooked_documents or upload_document.")
        ] = None,
        journal_code: Annotated[
            str | None, Field(description="Purchase journal. Default: the division's only one.")
        ] = None,
        dry_run: DryRun = False,
        allow_duplicate: bool = False,
        division: Division = None,
    ) -> dict:
        """Book a purchase invoice (or, with negative amounts, a credit note) in the purchase
        journal. Exact books the total on the supplier's payables and the VAT from each line's VAT
        code; the preview's VAT is worked out the same way to check it against expected_total.
        Refuses an invoice whose supplier invoice number this supplier already has."""
        return await _book_invoice(
            PURCHASE,
            supplier_id,
            invoice_date,
            lines,
            {
                "YourRef": supplier_invoice_number,
                "DueDate": due_date.isoformat() if due_date else None,
                "Description": description or supplier_invoice_number,
                "Currency": currency,
                "PaymentReference": payment_reference,
                "Document": odata.parse_guid(document_id) if document_id else None,
            },
            [f"Supplier eq {odata.guid(supplier_id)}", f"YourRef eq {odata.string(supplier_invoice_number)}"],
            journal_code,
            expected_total,
            dry_run,
            allow_duplicate,
            division,
        )

    @tool(writes=True)
    async def book_sales_invoice(
        customer_id: Annotated[str, Field(description="Customer (GUID) from search_accounts or create_account.")],
        invoice_date: date,
        lines: Annotated[list[InvoiceLine], Field(min_length=1)],
        invoice_number: Annotated[
            int | None, Field(description="Our invoice number. Default: the journal's next one.")
        ] = None,
        reference: Annotated[
            str | None,
            Field(description="The invoice's reference elsewhere, such as another invoicing system's, as YourRef."),
        ] = None,
        expected_total: Annotated[
            float | None,
            Field(description="The invoice's total including VAT. Nothing is booked unless the lines add up to it."),
        ] = None,
        due_date: date | None = None,
        description: str | None = None,
        currency: Annotated[str | None, Field(description="Currency code. Default: the journal's.")] = None,
        payment_reference: str | None = None,
        document_id: Annotated[str | None, Field(description="The invoice's document (GUID).")] = None,
        journal_code: Annotated[
            str | None, Field(description="Sales journal. Default: the division's only one.")
        ] = None,
        dry_run: DryRun = False,
        allow_duplicate: bool = False,
        division: Division = None,
    ) -> dict:
        """Book a sales invoice made outside Exact, such as in a webshop or another invoicing
        system, in the sales journal (negative amounts for a credit note). It needs an
        invoice_number or a reference, so the same invoice can't be booked twice."""
        if invoice_number is None and not reference:
            raise ValueError("Give the invoice_number or a reference, so a second booking of it can be caught.")
        if invoice_number is not None:
            duplicates = [f"InvoiceNumber eq {invoice_number}"]
        else:
            duplicates = [f"Customer eq {odata.guid(customer_id)}", f"YourRef eq {odata.string(reference)}"]
        return await _book_invoice(
            SALES,
            customer_id,
            invoice_date,
            lines,
            {
                "InvoiceNumber": invoice_number,
                "YourRef": reference,
                "DueDate": due_date.isoformat() if due_date else None,
                "Description": description,
                "Currency": currency,
                "PaymentReference": payment_reference,
                "Document": odata.parse_guid(document_id) if document_id else None,
            },
            duplicates,
            journal_code,
            expected_total,
            dry_run,
            allow_duplicate,
            division,
        )

    @tool(writes=True)
    async def book_journal_entry(
        entry_date: date,
        lines: Annotated[list[JournalLine], Field(min_length=2)],
        description: Annotated[str, Field(description="What the entry is for; each line without its own gets it.")],
        journal_code: Annotated[
            str | None, Field(description="General journal. Default: the division's only one.")
        ] = None,
        currency: str | None = None,
        dry_run: DryRun = False,
        division: Division = None,
    ) -> dict:
        """Book a general journal (memorial) entry, such as an accrual, a depreciation, a
        reclassification or clearing a suspense account. The lines must balance: debit positive,
        credit negative, adding up to zero. Lines on receivables or payables need the customer or
        supplier, and cash and bank accounts are refused: money moves through their own journals.
        Without VAT: book VAT through an invoice."""
        if abs(total := sum(line.amount for line in lines)) >= matching.TOLERANCE:
            raise ValueError(f"The lines add up to {total:.2f}, not zero: debit and credit must balance.")
        client = await get_client()
        div = await client.division(division)
        journal = await _journal(client, div, journal_code, "general")
        periods = await client.list(
            "financial/FinancialPeriods",
            select=["FinYear", "FinPeriod"],
            filters=[
                f"StartDate le {odata.datetime_literal(entry_date)}",
                f"EndDate ge {odata.datetime_literal(entry_date)}",
            ],
            limit=1,
            division=div,
        )
        if not periods:
            raise ValueError(f"No financial period in Exact covers {entry_date}; check the division's financial years.")
        chart = await _chart(client, div)
        payload, preview = [], []
        for line in lines:
            account = _gl_account(chart, line.gl_account_code, CASH_TYPES)
            if account["Type"] in OPEN_ITEM_TYPES and not line.account_id:
                raise ValueError(f"GL account {account['Code']} holds open items, so its line needs an account_id.")
            optional = {
                "Account": odata.parse_guid(line.account_id) if line.account_id else None,
                "CostCenter": line.cost_center,
                "CostUnit": line.cost_unit,
                "Project": odata.parse_guid(line.project_id) if line.project_id else None,
            }
            payload.append(
                {
                    "GLAccount": account["ID"],
                    "AmountFC": line.amount,
                    "Date": entry_date.isoformat(),
                    "Description": line.description or description,
                    **{key: value for key, value in optional.items() if value is not None},
                }
            )
            preview.append(
                {
                    "gl_account_code": account["Code"],
                    "gl_account_description": account["Description"],
                    "amount": line.amount,
                    "description": line.description or description,
                }
            )
        body = {
            "JournalCode": journal,
            "FinancialYear": periods[0]["FinYear"],
            "FinancialPeriod": periods[0]["FinPeriod"],
            **({"Currency": currency} if currency else {}),
            "GeneralJournalEntryLines": payload,
        }
        result = {
            "journal": journal,
            "year": periods[0]["FinYear"],
            "period": periods[0]["FinPeriod"],
            "debit": round(sum(line.amount for line in lines if line.amount > 0), 2),
            "lines": preview,
        }
        if dry_run:
            return {"booked": False, **result}
        created = await client.create("generaljournalentry/GeneralJournalEntries", body, div)
        return {"booked": True, **_created(created, ["EntryID", "EntryNumber", "Status"]), **result}

    @tool(writes=True)
    async def create_account(
        name: str,
        role: Literal["supplier", "customer", "both"],
        vat_number: str | None = None,
        chamber_of_commerce: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        address_line1: str | None = None,
        postcode: str | None = None,
        city: str | None = None,
        country: Annotated[str | None, Field(description="Two-letter country code, such as NL.")] = None,
        default_vat_code: Annotated[
            str | None, Field(description="VAT code its invoices usually get, for the role's side.")
        ] = None,
        payment_condition: Annotated[str | None, Field(description="Payment condition code.")] = None,
        allow_duplicate: bool = False,
        dry_run: DryRun = False,
        division: Division = None,
    ) -> dict:
        """Create a supplier or customer. Refuses when an account with the same VAT number,
        chamber of commerce number or name exists, and returns those instead: search_accounts
        first, and create one only for a party Exact doesn't know."""
        client = await get_client()
        div = await client.division(division)
        if not allow_duplicate:
            same = [f"Name eq {odata.string(name.strip())}"]
            if vat_number:
                same.append(f"VATNumber eq {odata.string(vat_number.strip())}")
            if chamber_of_commerce:
                same.append(f"ChamberOfCommerce eq {odata.string(chamber_of_commerce.strip())}")
            existing = await client.list(
                "crm/Accounts",
                select=["ID", "Code", "Name", "VATNumber", "ChamberOfCommerce", "IsSales", "IsSupplier"],
                filters=[f"({' or '.join(same)})"],
                limit=10,
                division=div,
            )
            if existing:
                raise ValueError(
                    f"Exact already has {', '.join(a['Name'] + ' (' + a['ID'] + ')' for a in existing)}. Use that "
                    "account, or pass allow_duplicate if this really is another party."
                )
        supplier, customer = role in ("supplier", "both"), role in ("customer", "both")
        fields = {
            "Name": name.strip(),
            "IsSupplier": supplier,
            # Status C makes an account a customer.
            "Status": "C" if customer else None,
            "VATNumber": vat_number,
            "ChamberOfCommerce": chamber_of_commerce,
            "Email": email,
            "Phone": phone,
            "AddressLine1": address_line1,
            "Postcode": postcode,
            "City": city,
            "Country": country.upper() if country else None,
            "PurchaseVATCode": default_vat_code if supplier else None,
            "SalesVATCode": default_vat_code if customer else None,
            "PaymentConditionPurchase": payment_condition if supplier else None,
            "PaymentConditionSales": payment_condition if customer else None,
        }
        body = {key: value for key, value in fields.items() if value is not None}
        if dry_run:
            return {"created": False, "account": body}
        created = await client.create("crm/Accounts", body, div)
        return {"created": True, **_created(created, ["ID", "Code", "Name", "IsSupplier", "Status"])}

    @tool(writes=True)
    async def upload_document(
        file_name: Annotated[str, Field(description="The file's name, such as invoice-2026-114.pdf.")],
        content_base64: Annotated[str, Field(description="The file, Base64-encoded.")],
        kind: Literal["purchase_invoice", "sales_invoice"] = "purchase_invoice",
        subject: Annotated[str | None, Field(description="Default: the file name.")] = None,
        account_id: Annotated[str | None, Field(description="The supplier or customer it is from or to.")] = None,
        document_date: date | None = None,
        division: Division = None,
    ) -> dict:
        """Store an invoice's file in Exact as a document, to book with book_purchase_invoice's
        document_id, so the booking and its source stay together."""
        try:
            data = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as err:
            raise ValueError("content_base64 isn't valid Base64.") from err
        if not data:
            raise ValueError("The file is empty.")
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"The file is {len(data):,} bytes; at most {MAX_ATTACHMENT_BYTES:,} can be uploaded here.")
        client = await get_client()
        div = await client.division(division)
        fields = {
            "Subject": subject or file_name,
            "Type": DOCUMENT_TYPES[kind],
            "Account": odata.parse_guid(account_id) if account_id else None,
            "DocumentDate": document_date.isoformat() if document_date else None,
        }
        document = await client.create(
            "documents/Documents", {key: value for key, value in fields.items() if value is not None}, div
        )
        try:
            attachment = await client.create(
                "documents/DocumentAttachments",
                {"Document": document["ID"], "FileName": file_name, "Attachment": base64.b64encode(data).decode()},
                div,
            )
        except ExactError as err:
            raise ExactError(f"Document {document['ID']} was made, but its file wasn't added: {err}") from err
        return {"document_id": document["ID"], "attachment_id": attachment.get("ID"), "bytes": len(data)}

    @tool
    async def reconciliation_suggestions(
        side: Literal["receivables", "payables"] = "receivables",
        account_id: Annotated[str | None, Field(description="Only this customer or supplier (GUID).")] = None,
        limit: Annotated[int, Field(ge=1, le=2000, description="Open items to read; 60 a call.")] = 300,
        division: Division = None,
    ) -> dict:
        """Sets of open items that settle each other, ready for match_open_items: an account whose
        open items add up to zero, a payment whose description names the invoices it pays, or a
        payment with exactly one invoice of its amount. Payments with no clear invoice are listed
        as unmatched, with any invoices of the same amount as candidates."""
        filters = [f"AccountId eq {odata.guid(account_id)}"] if account_id else []
        client = await get_client()
        rows = await client.list(
            "read/financial/ReceivablesList" if side == "receivables" else "read/financial/PayablesList",
            select=[
                "AccountId",
                "AccountCode",
                "AccountName",
                "JournalCode",
                "EntryNumber",
                "InvoiceNumber",
                "InvoiceDate",
                "DueDate",
                "Amount",
                "CurrencyCode",
                "Description",
                "YourRef",
            ],
            filters=filters,
            orderby="AccountCode",
            limit=limit,
            division=division,
        )
        return {"complete": len(rows) < limit, **matching.suggest_matches(rows)}

    @tool(writes=True)
    async def match_open_items(
        account_id: Annotated[str, Field(description="The customer or supplier (GUID) the items are of.")],
        items: Annotated[
            list[OpenItem],
            Field(min_length=2, description="The entries to match, such as an invoice and its payment."),
        ],
        dry_run: DryRun = False,
        division: Division = None,
    ) -> dict:
        """Match open items of one customer or supplier that settle each other, such as an invoice
        and the bank payment of it, so neither is open any more. Their amounts on the receivables
        or payables account must add up to zero. Done through Exact's XML API, which REST has no
        equivalent of."""
        client = await get_client()
        div = await client.division(division)
        chart = await _chart(client, div)
        open_item_accounts = {code for code, row in chart.items() if row["Type"] in OPEN_ITEM_TYPES}
        wanted = {(item.journal_code.strip(), item.entry_number) for item in items}
        entries = " or ".join(f"EntryNumber eq {number}" for number in sorted({n for _, n in wanted}))
        lines = await client.list(
            "bulk/Financial/TransactionLines",
            select=[
                "FinancialYear",
                "FinancialPeriod",
                "JournalCode",
                "EntryNumber",
                "GLAccountCode",
                "AccountCode",
                "AmountDC",
            ],
            filters=[f"Account eq {odata.guid(account_id)}", f"({entries})"],
            limit=10_000,
            division=div,
        )
        sums: dict[tuple, float] = defaultdict(float)
        gl_codes, account_codes = set(), set()
        for line in lines:
            if (line["JournalCode"], line["EntryNumber"]) in wanted and line["GLAccountCode"] in open_item_accounts:
                key = (line["FinancialYear"], line["FinancialPeriod"], line["JournalCode"], line["EntryNumber"])
                sums[key] += line["AmountDC"] or 0
                gl_codes.add(line["GLAccountCode"])
                account_codes.add(line["AccountCode"])
        if missing := wanted - {(journal, entry) for _, _, journal, entry in sums}:
            raise ValueError(
                "This account has nothing on receivables or payables from "
                + ", ".join(f"journal {journal} entry {entry}" for journal, entry in sorted(missing))
                + "."
            )
        if len(gl_codes) != 1:
            raise ValueError(
                f"The items are on different GL accounts ({', '.join(sorted(gl_codes))}); match per account."
            )
        match_lines = [
            {"year": year, "period": period, "journal": journal, "entry": entry, "amount_dc": round(amount, 2)}
            for (year, period, journal, entry), amount in sorted(sums.items())
        ]
        if abs(left := sum(line["amount_dc"] for line in match_lines)) >= matching.TOLERANCE:
            raise ValueError(f"The items leave {left:.2f} open, and only a set that adds up to zero can be matched.")
        result = {"gl_account_code": gl_codes.pop(), "account_code": account_codes.pop(), "lines": match_lines}
        if dry_run:
            return {"matched": False, **result}
        answer = await client.upload_xml(
            "MatchSets", matching.match_sets_xml(result["gl_account_code"], result["account_code"], match_lines), div
        )
        messages = matching.upload_messages(answer)
        if not messages or any(message["type"] in matching.FAILED_MESSAGES for message in messages):
            reasons = "; ".join(message["description"] for message in messages) or "no answer for the match"
            raise ExactError(f"Exact didn't match the items: {reasons}")
        return {"matched": True, **result, "messages": [message["description"] for message in messages]}
