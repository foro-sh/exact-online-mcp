"""Booking in Exact: purchase and sales invoices, journal entries, new customers and suppliers,
documents, and matching open items. The only toolset that writes, so EXACT_TOOLSETS has to name
it; `all` leaves it out."""

import base64
import binascii
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import AfterValidator, BaseModel, Field

from .. import matching, odata
from ..client import ExactError
from ..tools import JOURNAL_TYPES, VAT_CODE_FIELDS, Division, Limit
from .documents import MAX_ATTACHMENT_BYTES

# Exact books an invoice's total on the receivables or payables account and its VAT on a VAT
# account itself, and money only moves through a cash or bank journal.
CASH_TYPES = {10: "cash", 12: "bank", 14: "credit card", 16: "payment service"}
CONTROL_TYPES = {**CASH_TYPES, 20: "accounts receivable", 22: "accounts payable", 24: "VAT"}
OPEN_ITEM_TYPES = {20: "receivables", 22: "payables"}
# Community-documented: Exact doesn't list its document type codes, so upload_document checks the
# type with documents/DocumentTypes before using it.
DOCUMENT_TYPES = {"sales_invoice": 10, "purchase_invoice": 20}


def _cents(value: float | None) -> float | None:
    # Exact rounds an amount to cents itself, after the check that the lines balance.
    if value is not None and abs(value * 100 - round(value * 100)) > 1e-6:
        raise ValueError(f"{value} has more than two decimals; give amounts in cents.")
    return value


Cents = AfterValidator(_cents)
DryRun = Annotated[bool, Field(description="Check everything and return what would be booked, without booking it.")]
CostCenter = Annotated[str | None, Field(description="Cost center code.")]
CostUnit = Annotated[str | None, Field(description="Cost unit code.")]
ProjectId = Annotated[str | None, Field(description="Project ID (GUID).")]


class InvoiceLine(BaseModel):
    gl_account_code: str = Field(description="GL account to book the line on; see suggest_booking.")
    amount: Annotated[float, Cents] = Field(
        description="Amount excluding VAT, in the invoice's currency. Negative on a credit note."
    )
    vat_code: str | None = Field(
        None, description="VAT code (see list_vat_codes). Default: the GL account's own VAT code."
    )
    vat_amount: Annotated[float | None, Cents] = Field(
        None, description="The VAT on the invoice for this line, when it differs from the VAT code's by rounding."
    )
    description: str | None = None
    cost_center: CostCenter = None
    cost_unit: CostUnit = None
    project_id: ProjectId = None


class JournalLine(BaseModel):
    gl_account_code: str
    amount: Annotated[float, Cents] = Field(
        description="Debit positive, credit negative. The lines must add up to zero."
    )
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
    invoice_type: int
    credit_note_type: int
    # The crm/Accounts flag an account needs to be booked on this side, and what it's called.
    party_flag: str
    party_role: str


PURCHASE = Side(
    "purchaseentry/PurchaseEntries", "PurchaseEntryLines", "Supplier", "purchase", "P", 30, 31, "IsSupplier", "supplier"
)
SALES = Side("salesentry/SalesEntries", "SalesEntryLines", "Customer", "sales", "S", 20, 21, "IsSales", "customer")


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
        kind = refused[account["Type"]]
        article = "an" if kind[0] in "aeiou" else "a"
        raise ValueError(f"{name} is {article} {kind} account, which can't be booked on here.")
    return account


async def _journal(client, division, code: str | None, journal_type: str) -> dict:
    rows = await client.list(
        "financial/Journals",
        select=["Code", "Description", "IsBlocked", "AllowVAT"],
        filters=[f"Type eq {JOURNAL_TYPES[journal_type]}"],
        limit=1000,
        division=division,
    )
    usable = {row["Code"]: row for row in rows if not row["IsBlocked"]}
    if code is not None:
        if code.strip() not in usable:
            raise ValueError(f"No open {journal_type} journal {code}; this division has {', '.join(usable) or 'none'}.")
        return usable[code.strip()]
    if len(usable) != 1:
        raise ValueError(
            f"This division has {len(usable) or 'no'} open {journal_type} journals"
            f"{' (' + ', '.join(usable) + ')' if usable else ''}; pass journal_code."
        )
    return next(iter(usable.values()))


async def _vat_codes(client, division) -> dict[str, dict]:
    rows = await client.list("vat/VATCodes", select=VAT_CODE_FIELDS, limit=1000, division=division)
    return {row["Code"]: row for row in rows}


async def _party(client, division, side: Side, party_id: str) -> dict:
    """The supplier or customer an invoice is for, checked before Exact refuses it."""
    rows = await client.list(
        "crm/Accounts",
        select=["ID", "Name", side.party_flag],
        filters=[f"ID eq {odata.guid(party_id)}"],
        limit=1,
        division=division,
    )
    if not rows:
        raise ValueError(f"No account {party_id} in this division; see search_accounts.")
    if not rows[0][side.party_flag]:
        raise ValueError(
            f"{rows[0]['Name']} isn't a {side.party_role} in Exact, so Exact won't book this on it. Use the "
            f"{side.party_role}, or make this account one in Exact first."
        )
    return rows[0]


async def _check_cost_codes(client, division, lines) -> None:
    """Every cost center and cost unit on the lines exists, checked before Exact refuses them."""
    for resource, field, kind in (
        ("hrm/Costcenters", "cost_center", "cost center"),
        ("hrm/Costunits", "cost_unit", "cost unit"),
    ):
        codes = {getattr(line, field).strip() for line in lines if getattr(line, field)}
        if not codes:
            continue
        rows = await client.list(
            resource,
            select=["Code"],
            filters=[f"({' or '.join(f'Code eq {odata.string(code)}' for code in sorted(codes))})"],
            limit=len(codes),
            division=division,
        )
        if missing := codes - {row["Code"].strip() for row in rows}:
            raise ValueError(f"No {kind} {', '.join(sorted(missing))} in this division.")


def _vat_code(code: str) -> str:
    # Exact holds VAT codes padded to three characters, and the clients that book send them so.
    return code.ljust(3)


def _rate(vat: dict) -> float:
    # Exact's docs don't give Percentage's unit. No VAT rate reaches 100%, so above 1 it is a percent.
    percentage = vat["Percentage"] or 0
    return percentage / 100 if percentage > 1 else percentage


def _vat_on_invoice(line: InvoiceLine, vat: dict | None) -> float:
    if line.vat_amount is not None:
        return line.vat_amount
    # Reverse-charged VAT is declared, not invoiced; B and N codes add none.
    if vat is None or vat["Charged"] or vat["Type"] in ("B", "N"):
        return 0.0
    return round(line.amount * _rate(vat), 2)


def _invoice_lines(
    lines: list[InvoiceLine], chart, vat_codes, side: Side, description: str | None
) -> tuple[list[dict], list[dict]]:
    """Exact's lines for the entry, and a preview of each with the VAT it adds. A line without
    a description gets the invoice's."""
    payload, preview = [], []
    for line in lines:
        line_description = line.description or description
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
            "VATCode": _vat_code(code) if code else None,
            "VATAmountFC": line.vat_amount,
            "Description": line_description,
            "CostCenter": line.cost_center,
            "CostUnit": line.cost_unit,
            "Project": odata.parse_guid(line.project_id) if line.project_id else None,
        }
        payload.append({**entry, **{key: value for key, value in optional.items() if value is not None}})
        preview.append(
            {
                "gl_account_code": account["Code"],
                "gl_account_description": account["Description"],
                "description": line_description,
                "amount": line.amount,
                "vat_code": code,
                "vat_rate": _rate(vat) if vat else None,
                "vat_amount": vat_amount,
            }
        )
    return payload, preview


def _totals(preview: list[dict]) -> dict:
    net = round(sum(line["amount"] for line in preview), 2)
    vat = round(sum(line["vat_amount"] for line in preview), 2)
    return {"net": net, "vat": vat, "total": round(net + vat, 2)}


def _vat_number(number: str) -> str:
    """The VAT number without spaces or dots. A Dutch one must pass one of the Belastingdienst's
    two checks, as Exact refuses it otherwise, but only after the dry run."""
    number = re.sub(r"[\s.]", "", number).upper()
    # ponytail: only Dutch numbers are checked; other countries are left to Exact.
    if number.startswith("NL"):
        digits = number[2:11]
        valid = re.fullmatch(r"NL\d{9}B\d{2}", number) is not None and (
            # The old eleven check, or since 2020 mod 97 over the number with N=23, L=21 and B=11.
            (sum(int(d) * (9 - i) for i, d in enumerate(digits[:8])) - int(digits[8])) % 11 == 0
            or int(f"2321{digits}11{number[12:]}") % 97 == 1
        )
        if not valid:
            raise ValueError(f"{number} isn't a valid Dutch VAT number, such as NL123456782B01.")
    return number


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
            select=[
                "ID",
                "Subject",
                "TypeDescription",
                "DocumentDate",
                "Account",
                "AccountName",
                "AmountFC",
                "Currency",
                "Created",
            ],
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
        account, VAT code and cost center on their lines, most used first. A coding with
        vat_included true was booked with a VAT code that includes VAT, which the book tools don't
        take: book it with the excluding code of the same rate."""
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
        included = (
            {code for code, vat in (await _vat_codes(client, div)).items() if vat["Type"] == "I"} if codings else set()
        )
        ranked = []
        for coding in codings.values():
            used = len(coding.pop("entries"))
            ranked.append(
                {
                    **coding,
                    "vat_included": (coding["vat_code"] or "").strip() in included,
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
        await _party(client, div, side, party_id)
        journal = (await _journal(client, div, journal_code, side.journal_type))["Code"]
        await _check_cost_codes(client, div, lines)
        payload, preview = _invoice_lines(
            lines, await _chart(client, div), await _vat_codes(client, div), side, header.get("Description")
        )
        totals = _totals(preview)
        # Exact books exactly what the lines add up to, so a cent off is a wrong payable or receivable.
        if expected_total is not None and totals["total"] != round(expected_total, 2):
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
            "EntryDate": odata.post_date(invoice_date),
            # Set rather than left to Exact: its docs don't say it tells a credit note by its sign.
            "Type": side.credit_note_type if totals["net"] < 0 else side.invoice_type,
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
        # A stray space would get past the check for a second booking.
        supplier_invoice_number = supplier_invoice_number.strip()
        return await _book_invoice(
            PURCHASE,
            supplier_id,
            invoice_date,
            lines,
            {
                "YourRef": supplier_invoice_number,
                "DueDate": odata.post_date(due_date) if due_date else None,
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
        reference = reference.strip() if reference else None
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
                "DueDate": odata.post_date(due_date) if due_date else None,
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
        allow_duplicate: bool = False,
        division: Division = None,
    ) -> dict:
        """Book a general journal (memorial) entry, such as an accrual, a depreciation, a
        reclassification or clearing a suspense account. The lines must balance: debit positive,
        credit negative, adding up to zero. Lines on receivables or payables need the customer or
        supplier, and cash and bank accounts are refused: money moves through their own journals.
        Without VAT: book VAT through an invoice. Refuses an entry whose first line the journal
        already has on that day, so a retry doesn't book it twice."""
        if abs(total := sum(line.amount for line in lines)) >= matching.TOLERANCE:
            raise ValueError(f"The lines add up to {total:.2f}, not zero: debit and credit must balance.")
        client = await get_client()
        div = await client.division(division)
        journal_row = await _journal(client, div, journal_code, "general")
        journal = journal_row["Code"]
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
        await _check_cost_codes(client, div, lines)
        payload, preview = [], []
        for line in lines:
            account = _gl_account(chart, line.gl_account_code, CASH_TYPES)
            if account["Type"] in OPEN_ITEM_TYPES and not line.account_id:
                raise ValueError(f"GL account {account['Code']} holds open items, so its line needs an account_id.")
            if journal_row["AllowVAT"] and account["VATCode"]:
                # A VAT journal applies the account's default VAT code to a line without one, which
                # would add VAT the lines don't balance for.
                raise ValueError(
                    f"Journal {journal} books VAT, and GL account {account['Code']} has VAT code "
                    f"{account['VATCode']}, so Exact would add VAT to the line. Use a general journal "
                    "without VAT, or book it as an invoice."
                )
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
                    "Date": odata.post_date(entry_date),
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
        if not allow_duplicate:
            first = preview[0]
            existing = await client.list(
                "financialtransaction/TransactionLines",
                select=["EntryNumber", "Date"],
                filters=[
                    f"JournalCode eq {odata.string(journal)}",
                    f"Date eq {odata.datetime_literal(entry_date)}",
                    f"GLAccountCode eq {odata.string(first['gl_account_code'])}",
                    f"AmountFC eq {first['amount']}",
                    f"Description eq {odata.string(first['description'])}",
                ],
                limit=1,
                division=div,
            )
            if existing:
                raise ValueError(
                    f"This entry looks booked already, as entry {existing[0]['EntryNumber']} of {entry_date}. "
                    "Pass allow_duplicate only if it really is another one."
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
        vat_number = _vat_number(vat_number) if vat_number else None
        client = await get_client()
        div = await client.division(division)
        if not allow_duplicate:
            same = [f"Name eq {odata.string(name.strip())}"]
            if vat_number:
                same.append(f"VATNumber eq {odata.string(vat_number)}")
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
            "PurchaseVATCode": _vat_code(default_vat_code) if default_vat_code and supplier else None,
            "SalesVATCode": _vat_code(default_vat_code) if default_vat_code and customer else None,
            "PaymentConditionPurchase": payment_condition if supplier else None,
            "PaymentConditionSales": payment_condition if customer else None,
        }
        body = {key: value for key, value in fields.items() if value is not None}
        if dry_run:
            return {"created": False, "account": body}
        created = await client.create("crm/Accounts", body, div)
        # Exact's answer to the POST leaves most fields empty, so say what was sent.
        return {"created": True, "ID": created.get("ID"), "account": body}

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
        types = await client.list(
            "documents/DocumentTypes",
            select=["ID", "Description", "DocumentIsCreatable"],
            filters=[f"ID eq {DOCUMENT_TYPES[kind]}"],
            limit=1,
            division=div,
        )
        if not types or not types[0]["DocumentIsCreatable"]:
            raise ValueError(
                f"Exact has no document type {DOCUMENT_TYPES[kind]} that can be created for a {kind.replace('_', ' ')}."
            )
        fields = {
            "Subject": subject or file_name,
            "Type": DOCUMENT_TYPES[kind],
            "Account": odata.parse_guid(account_id) if account_id else None,
            "DocumentDate": odata.post_date(document_date) if document_date else None,
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
        return {
            "document_id": document["ID"],
            "attachment_id": attachment.get("ID"),
            "type": types[0]["Description"],
            "bytes": len(data),
        }

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
        gl_code = next(iter(gl_codes))
        open_rows = await client.list(
            "read/financial/ReceivablesList" if chart[gl_code]["Type"] == 20 else "read/financial/PayablesList",
            select=["JournalCode", "EntryNumber"],
            filters=[f"AccountId eq {odata.guid(account_id)}"],
            limit=2000,
            division=div,
        )
        if closed := wanted - {(row["JournalCode"].strip(), row["EntryNumber"]) for row in open_rows}:
            raise ValueError(
                "Not open any more, so already matched or paid: "
                + ", ".join(f"journal {journal} entry {entry}" for journal, entry in sorted(closed))
                + "."
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
