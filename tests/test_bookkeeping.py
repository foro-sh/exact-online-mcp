import base64
import json

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from exact_mcp import matching, toolsets
from exact_mcp.app import create_server

from .conftest import odata
from .test_tools import ACCOUNT_ID, call

SUPPLIER_ID = "8f7b2c1e-0000-4000-8000-000000000021"
COSTS_ID = "8f7b2c1e-0000-4000-8000-000000000022"
DEBTORS_ID = "8f7b2c1e-0000-4000-8000-000000000023"
DOCUMENT_ID = "8f7b2c1e-0000-4000-8000-000000000024"
ENTRY_ID = "8f7b2c1e-0000-4000-8000-000000000025"

CHART = [
    {"ID": COSTS_ID, "Code": "4500", "Description": "Software", "Type": 121, "IsBlocked": False, "VATCode": "2"},
    {"ID": DEBTORS_ID, "Code": "1300", "Description": "Debtors", "Type": 20, "IsBlocked": False, "VATCode": None},
    {"ID": "b", "Code": "1100", "Description": "Bank", "Type": 12, "IsBlocked": False, "VATCode": None},
    {"ID": "c", "Code": "4600", "Description": "Old", "Type": 121, "IsBlocked": True, "VATCode": None},
]
VAT_CODES = [
    {"Code": "2", "Description": "21% excl", "Percentage": 0.21, "Type": "E", "VATTransactionType": "B",
     "Charged": False, "IsBlocked": False},
    {"Code": "7", "Description": "Reverse charge", "Percentage": 0.21, "Type": "E", "VATTransactionType": "P",
     "Charged": True, "IsBlocked": False},
    {"Code": "5", "Description": "21% incl", "Percentage": 0.21, "Type": "I", "VATTransactionType": "B",
     "Charged": False, "IsBlocked": False},
    {"Code": "9", "Description": "Sales only", "Percentage": 0.09, "Type": "E", "VATTransactionType": "S",
     "Charged": False, "IsBlocked": False},
]  # fmt: skip


@pytest.fixture
def server(client):
    async def get_client():
        return client

    mcp = FastMCP("test")
    toolsets.register(mcp, get_client)
    return mcp


class Books:
    """Answers each GET with the rows for its resource, and each POST with what it was sent."""

    def __init__(self, rows: dict[str, list[dict]]):
        self.rows = {
            "Journals": [{"Code": "60", "Description": "Purchase", "IsBlocked": False, "AllowVAT": True}],
            "DocumentTypes": [{"ID": 20, "Description": "Purchase invoice", "DocumentIsCreatable": True}],
            **rows,
        }
        self.rows.setdefault("GLAccounts", CHART)
        self.rows.setdefault("VATCodes", VAT_CODES)
        # What the matched entries leave open, unless a test says otherwise.
        self.rows.setdefault(
            "ReceivablesList",
            [
                {"JournalCode": r["JournalCode"], "EntryNumber": r["EntryNumber"]}
                for r in rows.get("TransactionLines", [])
                if "JournalCode" in r
            ],
        )
        self.party = {"ID": SUPPLIER_ID, "Name": "Acme", "IsSupplier": True, "IsSales": True}
        self.posted: list[tuple[str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[1]
        if request.method == "POST":
            if name == "XMLUpload.aspx":
                self.posted.append((request.url.params["Topic"], request.content.decode()))
                return httpx.Response(200, text=self.rows["xml_answer"])
            body = json.loads(request.content)
            self.posted.append((name, body))
            return httpx.Response(201, json={"d": {"EntryID": ENTRY_ID, "EntryNumber": 26700001, "ID": DOCUMENT_ID}})
        if (
            name == "Accounts"
            and "Accounts" not in self.rows
            and request.url.params.get("$filter", "").startswith("ID eq")
        ):
            # The supplier or customer an invoice is booked on.
            return httpx.Response(200, json=odata([self.party] if self.party else []))
        return httpx.Response(200, json=odata(self.rows.get(name, [])))


def purchase(**overrides) -> dict:
    return {
        "supplier_id": SUPPLIER_ID,
        "supplier_invoice_number": "INV-114",
        "invoice_date": "2026-10-01",
        "lines": [{"gl_account_code": "4500", "amount": 100.0, "description": "Licences"}],
        **overrides,
    }


async def test_book_purchase_invoice_posts_the_entry_with_its_lines(server, exact):
    exact.handler = books = Books({})

    result = await call(server, "book_purchase_invoice", purchase(expected_total=121, document_id=DOCUMENT_ID))

    assert result["booked"] is True
    assert result["EntryNumber"] == 26700001
    assert (result["net"], result["vat"], result["total"]) == (100.0, 21.0, 121.0)
    [(resource, body)] = books.posted
    assert resource == "PurchaseEntries"
    assert body == {
        "Journal": "60",
        "Supplier": SUPPLIER_ID,
        "EntryDate": "2026-10-01T00:00:00Z",
        "Type": 30,
        "YourRef": "INV-114",
        "Description": "INV-114",
        "Document": DOCUMENT_ID,
        # The GL account's own VAT code, sent so the preview and the booking agree.
        "PurchaseEntryLines": [{"GLAccount": COSTS_ID, "AmountFC": 100.0, "VATCode": "2  ", "Description": "Licences"}],
    }
    duplicate_check = next(r for r in exact.requests if r.url.path.endswith("PurchaseEntries") and r.method == "GET")
    assert duplicate_check.url.params["$filter"] == f"Supplier eq guid'{SUPPLIER_ID}' and YourRef eq 'INV-114'"


async def test_a_dry_run_checks_everything_and_books_nothing(server, exact):
    exact.handler = books = Books({})

    result = await call(server, "book_purchase_invoice", purchase(dry_run=True))

    assert result["booked"] is False
    assert result["lines"][0]["vat_amount"] == 21.0
    assert books.posted == []


async def test_nothing_is_booked_when_the_lines_miss_the_invoice_total(server, exact):
    exact.handler = books = Books({})

    with pytest.raises(ToolError, match=r"add up to 121.00 \(100.00 plus 21.00 VAT\), not .* 120.00"):
        await call(server, "book_purchase_invoice", purchase(expected_total=120))
    assert books.posted == []


async def test_a_total_a_cent_off_is_refused_too(server, exact):
    exact.handler = books = Books({})
    line = {"gl_account_code": "4500", "amount": 109.5}

    with pytest.raises(ToolError, match=r"add up to 132.49 .* not .* 132.50"):
        await call(server, "book_purchase_invoice", purchase(lines=[line], expected_total=132.5))
    assert books.posted == []


async def test_a_line_without_a_description_gets_the_invoice_description(server, exact):
    exact.handler = books = Books({})
    line = {"gl_account_code": "4500", "amount": 100.0}

    result = await call(server, "book_purchase_invoice", purchase(lines=[line], description="KPN October"))

    assert result["lines"][0]["description"] == "KPN October"
    assert books.posted[0][1]["PurchaseEntryLines"][0]["Description"] == "KPN October"


async def test_an_invoice_line_on_receivables_is_refused(server, exact):
    exact.handler = Books({})
    line = {"gl_account_code": "1300", "amount": 100.0}

    with pytest.raises(ToolError, match=r"1300 \(Debtors\) is an accounts receivable account"):
        await call(server, "book_purchase_invoice", purchase(lines=[line]))


async def test_the_invoice_own_vat_amount_overrides_the_rounding(server, exact):
    exact.handler = books = Books({})
    line = {"gl_account_code": "4500", "amount": 100.0, "vat_amount": 20.99}

    result = await call(server, "book_purchase_invoice", purchase(lines=[line], expected_total=120.99))

    assert result["total"] == 120.99
    assert books.posted[0][1]["PurchaseEntryLines"][0]["VATAmountFC"] == 20.99


async def test_a_negative_invoice_is_booked_as_a_credit_note(server, exact):
    exact.handler = books = Books({})
    line = {"gl_account_code": "4500", "amount": -100.0}

    result = await call(server, "book_purchase_invoice", purchase(lines=[line], expected_total=-121))

    assert result["total"] == -121.0
    assert books.posted[0][1]["Type"] == 31


async def test_a_vat_percentage_given_in_percent_reads_as_a_rate(server, exact):
    exact.handler = Books({"VATCodes": [{**VAT_CODES[0], "Percentage": 21.0}]})

    result = await call(server, "book_purchase_invoice", purchase(dry_run=True))

    assert (result["vat"], result["lines"][0]["vat_rate"]) == (21.0, 0.21)


async def test_reverse_charged_vat_is_not_on_the_invoice(server, exact):
    exact.handler = Books({})
    line = {"gl_account_code": "4500", "amount": 100.0, "vat_code": "7"}

    result = await call(server, "book_purchase_invoice", purchase(lines=[line], expected_total=100))

    assert result["vat"] == 0.0


async def test_an_invoice_already_booked_for_the_supplier_is_refused(server, exact):
    exact.handler = books = Books({"PurchaseEntries": [{"EntryNumber": 26700000, "EntryDate": "2026-10-02"}]})

    with pytest.raises(ToolError, match="booked already, as entry 26700000"):
        await call(server, "book_purchase_invoice", purchase())
    assert books.posted == []


@pytest.mark.parametrize(
    ("line", "error"),
    [
        ({"gl_account_code": "9999", "amount": 1}, "No GL account 9999"),
        ({"gl_account_code": "4600", "amount": 1}, r"4600 \(Old\) is blocked"),
        ({"gl_account_code": "1300", "amount": 1}, "accounts receivable account"),
        ({"gl_account_code": "4500", "amount": 1, "vat_code": "8"}, "No VAT code 8"),
        ({"gl_account_code": "4500", "amount": 1, "vat_code": "9"}, "isn't for purchase invoices"),
        ({"gl_account_code": "4500", "amount": 1, "vat_code": "5"}, "includes VAT in the amount"),
    ],
)
async def test_invoice_lines_are_checked_before_anything_is_booked(server, exact, line, error):
    exact.handler = books = Books({})

    with pytest.raises(ToolError, match=error):
        await call(server, "book_purchase_invoice", purchase(lines=[line]))
    assert books.posted == []


async def test_an_invoice_is_refused_on_an_account_that_isnt_that_party(server, exact):
    exact.handler = books = Books({})
    books.party = {**books.party, "IsSupplier": False}

    with pytest.raises(ToolError, match="Acme isn't a supplier in Exact"):
        await call(server, "book_purchase_invoice", purchase(dry_run=True))

    books.party = None
    with pytest.raises(ToolError, match=f"No account {SUPPLIER_ID} in this division"):
        await call(server, "book_purchase_invoice", purchase(dry_run=True))


async def test_a_cost_center_exact_doesnt_have_is_refused(server, exact):
    exact.handler = Books({"Costcenters": [{"Code": "100"}]})
    lines = [{"gl_account_code": "4500", "amount": 50, "cost_center": "100"}, {**COSTS, "cost_center": "NOPE"}]

    with pytest.raises(ToolError, match="No cost center NOPE in this division"):
        await call(server, "book_purchase_invoice", purchase(lines=lines, dry_run=True))


async def test_an_amount_with_more_than_two_decimals_is_refused(server, exact):
    exact.handler = Books({})

    with pytest.raises(ToolError, match="more than two decimals"):
        await call(server, "book_purchase_invoice", purchase(lines=[{**COSTS, "amount": 10.004}]))


async def test_a_supplier_invoice_number_with_spaces_is_still_caught_as_booked(server, exact):
    exact.handler = Books({"PurchaseEntries": [{"EntryNumber": 26700001, "EntryDate": "2026-10-01"}]})

    with pytest.raises(ToolError, match="looks booked already"):
        await call(server, "book_purchase_invoice", purchase(supplier_invoice_number=" INV-114 "))

    check = next(r for r in exact.requests if r.url.path.endswith("PurchaseEntries"))
    assert check.url.params["$filter"].endswith("YourRef eq 'INV-114'")


async def test_a_division_with_several_purchase_journals_needs_one_named(server, exact):
    journals = [{"Code": "60", "IsBlocked": False}, {"Code": "61", "IsBlocked": False}]
    exact.handler = Books({"Journals": journals})

    with pytest.raises(ToolError, match=r"2 open purchase journals \(60, 61\); pass journal_code"):
        await call(server, "book_purchase_invoice", purchase())


async def test_book_sales_invoice_needs_a_number_or_reference_and_checks_it(server, exact):
    exact.handler = books = Books({"Journals": [{"Code": "70", "IsBlocked": False}]})

    with pytest.raises(ToolError, match="invoice_number or a reference"):
        await call(server, "book_sales_invoice", {"customer_id": ACCOUNT_ID, "invoice_date": "2026-10-01",
                                                   "lines": [{"gl_account_code": "4500", "amount": 50}]})  # fmt: skip
    result = await call(
        server,
        "book_sales_invoice",
        {
            "customer_id": ACCOUNT_ID,
            "invoice_date": "2026-10-01",
            "reference": "in_1Q2w3E",
            "lines": [{"gl_account_code": "4500", "amount": 50, "vat_code": "9"}],
        },
    )

    assert result["total"] == 54.5
    [(resource, body)] = books.posted
    assert resource == "SalesEntries"
    assert body["Customer"] == ACCOUNT_ID
    assert body["YourRef"] == "in_1Q2w3E"
    assert body["SalesEntryLines"][0]["VATCode"] == "9  "


GENERAL = [{"Code": "90", "IsBlocked": False, "AllowVAT": False}]


def journal_entry(lines, **overrides) -> dict:
    return {"entry_date": "2026-10-31", "description": "Accrue October rent", "lines": lines, **overrides}


async def test_book_journal_entry_books_a_balanced_entry_in_its_period(server, exact):
    exact.handler = books = Books({"Journals": GENERAL, "FinancialPeriods": [{"FinYear": 2026, "FinPeriod": 10}]})
    lines = [
        {"gl_account_code": "4500", "amount": 1500},
        {"gl_account_code": "1300", "amount": -1500, "account_id": ACCOUNT_ID, "description": "Owed"},
    ]

    result = await call(server, "book_journal_entry", journal_entry(lines))

    assert (result["year"], result["period"], result["debit"]) == (2026, 10, 1500.0)
    [(resource, body)] = books.posted
    assert resource == "GeneralJournalEntries"
    assert body["JournalCode"] == "90"
    assert (body["FinancialYear"], body["FinancialPeriod"]) == (2026, 10)
    assert body["GeneralJournalEntryLines"] == [
        {"GLAccount": COSTS_ID, "AmountFC": 1500.0, "Date": "2026-10-31T00:00:00Z",
         "Description": "Accrue October rent"},
        {"GLAccount": DEBTORS_ID, "AmountFC": -1500.0, "Date": "2026-10-31T00:00:00Z", "Description": "Owed",
         "Account": ACCOUNT_ID},
    ]  # fmt: skip


COSTS = {"gl_account_code": "4500", "amount": 10}


async def test_a_journal_entry_the_journal_already_has_is_refused(server, exact):
    exact.handler = books = Books(
        {
            "Journals": GENERAL,
            "FinancialPeriods": [{"FinYear": 2026, "FinPeriod": 10}],
            "TransactionLines": [{"EntryNumber": 26900004, "Date": "2026-10-31"}],
        }
    )
    lines = [COSTS, {"gl_account_code": "4500", "amount": -10, "description": "Back"}]

    with pytest.raises(ToolError, match="looks booked already, as entry 26900004"):
        await call(server, "book_journal_entry", journal_entry(lines))

    check = next(r for r in exact.requests if r.url.path.endswith("TransactionLines"))
    assert check.url.params["$filter"] == (
        "JournalCode eq '90' and Date eq datetime'2026-10-31' and GLAccountCode eq '4500' and "
        "AmountFC eq 10.0 and Description eq 'Accrue October rent'"
    )
    assert books.posted == []
    await call(server, "book_journal_entry", journal_entry(lines, allow_duplicate=True))
    assert len(books.posted) == 1


@pytest.mark.parametrize(
    ("lines", "error"),
    [
        ([COSTS, {"gl_account_code": "1300", "amount": -9.99, "account_id": ACCOUNT_ID}], "add up to 0.01, not zero"),
        ([COSTS, {"gl_account_code": "1300", "amount": -10}], "needs an account_id"),
        ([COSTS, {"gl_account_code": "1100", "amount": -10}], "bank account"),
    ],
)
async def test_an_unbalanced_or_misdirected_journal_entry_is_refused(server, exact, lines, error):
    exact.handler = books = Books({"Journals": GENERAL, "FinancialPeriods": [{"FinYear": 2026, "FinPeriod": 10}]})

    with pytest.raises(ToolError, match=error):
        await call(server, "book_journal_entry", journal_entry(lines))
    assert books.posted == []


async def test_a_journal_that_books_vat_refuses_an_account_with_a_vat_code(server, exact):
    journals = [{**GENERAL[0], "AllowVAT": True}]
    exact.handler = books = Books({"Journals": journals, "FinancialPeriods": [{"FinYear": 2026, "FinPeriod": 10}]})
    lines = [
        {"gl_account_code": "4500", "amount": 10},
        {"gl_account_code": "1300", "amount": -10, "account_id": ACCOUNT_ID},
    ]

    with pytest.raises(ToolError, match="Exact would add VAT"):
        await call(server, "book_journal_entry", journal_entry(lines))
    assert books.posted == []


async def test_create_account_refuses_a_party_exact_already_has(server, exact):
    exact.handler = books = Books({"Accounts": [{"ID": ACCOUNT_ID, "Name": "Acme B.V."}]})

    with pytest.raises(ToolError, match="already has Acme B.V."):
        await call(
            server, "create_account", {"name": "Acme B.V.", "role": "supplier", "vat_number": "NL 1234.56782B01"}
        )

    params = next(r for r in exact.requests if r.url.path.endswith("Accounts")).url.params
    assert params["$filter"] == "(Name eq 'Acme B.V.' or VATNumber eq 'NL123456782B01')"
    assert books.posted == []


async def test_create_account_makes_a_supplier_with_its_purchase_defaults(server, exact):
    exact.handler = books = Books({})

    result = await call(
        server, "create_account", {"name": "Acme", "role": "supplier", "country": "nl", "default_vat_code": "2"}
    )

    assert result == {
        "created": True,
        "ID": DOCUMENT_ID,
        "account": {"Name": "Acme", "IsSupplier": True, "Country": "NL", "PurchaseVATCode": "2  "},
    }
    assert books.posted == [
        ("Accounts", {"Name": "Acme", "IsSupplier": True, "Country": "NL", "PurchaseVATCode": "2  "})
    ]


@pytest.mark.parametrize("number", ["NL999999999B01", "NL12345678B01", "NL123456782X01"])
async def test_create_account_refuses_an_invalid_dutch_vat_number(server, exact, number):
    exact.handler = books = Books({})

    with pytest.raises(ToolError, match="isn't a valid Dutch VAT number"):
        await call(server, "create_account", {"name": "Acme", "role": "supplier", "vat_number": number})
    assert books.posted == []


async def test_create_account_takes_a_dutch_vat_number_that_passes_the_new_check(server, exact):
    exact.handler = books = Books({})

    await call(server, "create_account", {"name": "Acme", "role": "supplier", "vat_number": "nl000099998b57"})

    assert books.posted[0][1]["VATNumber"] == "NL000099998B57"


async def test_upload_document_stores_the_file_as_a_purchase_invoice(server, exact):
    exact.handler = books = Books({})
    pdf = base64.b64encode(b"%PDF-1.7").decode()

    result = await call(server, "upload_document", {"file_name": "inv.pdf", "content_base64": pdf})

    assert result == {"document_id": DOCUMENT_ID, "attachment_id": DOCUMENT_ID, "type": "Purchase invoice", "bytes": 8}
    assert books.posted == [
        ("Documents", {"Subject": "inv.pdf", "Type": 20}),
        ("DocumentAttachments", {"Document": DOCUMENT_ID, "FileName": "inv.pdf", "Attachment": pdf}),
    ]


async def test_upload_document_checks_the_document_type_first(server, exact):
    exact.handler = books = Books({"DocumentTypes": []})
    pdf = base64.b64encode(b"%PDF-1.7").decode()

    with pytest.raises(ToolError, match="no document type 20"):
        await call(server, "upload_document", {"file_name": "inv.pdf", "content_base64": pdf})
    assert books.posted == []


async def test_upload_document_refuses_what_isnt_base64(server, exact):
    with pytest.raises(ToolError, match="isn't valid Base64"):
        await call(server, "upload_document", {"file_name": "inv.pdf", "content_base64": "not base64!"})
    assert exact.requests == []


async def test_unbooked_documents_are_invoices_without_an_entry(server, exact):
    exact.handler = Books({})

    await call(server, "unbooked_documents", {"date_from": "2026-10-01"})

    assert exact.requests[0].url.params["$filter"] == (
        "Type eq 20 and FinancialTransactionEntryID eq null and DocumentDate ge datetime'2026-10-01'"
    )


async def test_suggest_booking_ranks_how_the_supplier_was_booked(server, exact):
    entries = [
        {"EntryID": "e1", "EntryDate": "2026-10-01", "AmountFC": 121},
        {"EntryID": "e2", "EntryDate": "2026-09-01", "AmountFC": 121},
        {"EntryID": "e3", "EntryDate": "2026-08-01", "AmountFC": 50},
    ]
    entries = [{**e, "EntryID": f"8f7b2c1e-0000-4000-8000-00000000003{n}"} for n, e in enumerate(entries)]
    line = {"GLAccountDescription": "Software", "VATCode": "2", "CostCenter": None, "Description": "Licences"}
    exact.handler = Books(
        {
            "Accounts": [{"ID": SUPPLIER_ID, "Code": "2001", "Name": "Acme", "GLAccountPurchase": COSTS_ID,
                          "PurchaseVATCode": "2", "PaymentConditionPurchase": "30"}],
            "GLAccounts": [{"Code": "4500", "Description": "Software"}],
            "PurchaseEntries": entries,
            "PurchaseEntryLines": [
                {**line, "EntryID": entries[0]["EntryID"], "GLAccountCode": "4500", "AmountFC": 100},
                {**line, "EntryID": entries[1]["EntryID"], "GLAccountCode": "4500", "AmountFC": 100},
                {**line, "EntryID": entries[2]["EntryID"], "GLAccountCode": "4510", "AmountFC": 50, "VATCode": "5"},
            ],
        }
    )  # fmt: skip

    result = await call(server, "suggest_booking", {"account_id": SUPPLIER_ID})

    assert result["defaults"] == {
        "gl_account": {"Code": "4500", "Description": "Software"},
        "vat_code": "2",
        "payment_condition": "30",
    }
    top = result["codings"][0]
    assert (top["gl_account_code"], top["invoices"], top["share"], top["total"]) == ("4500", 2, 0.67, 200.0)
    assert [c["vat_included"] for c in result["codings"]] == [False, True]
    assert top["last_used"] == "2026-10-01"
    lines_request = next(r for r in exact.requests if r.url.path.endswith("PurchaseEntryLines"))
    assert lines_request.url.params["$filter"].startswith(f"(EntryID eq guid'{entries[0]['EntryID']}' or ")


def open_item(entry, amount, invoice=None, description="", account="a1", ref=None) -> dict:
    return {
        "AccountId": account,
        "AccountName": account.upper(),
        "CurrencyCode": "EUR",
        "JournalCode": "70" if amount > 0 else "20",
        "EntryNumber": entry,
        "InvoiceNumber": invoice,
        "Amount": amount,
        "Description": description,
        "YourRef": ref,
    }


def test_suggest_matches_from_a_settled_account_a_reference_and_a_unique_amount():
    items = [
        open_item(1, 100, account="settled"),
        open_item(2, -100, account="settled"),
        # Two invoices of 50; the payment names one of them.
        open_item(10, 50, invoice=2026001),
        open_item(11, 50, invoice=2026002),
        open_item(12, 80, invoice=2026003),
        open_item(13, -50, description="Betaling factuur 2026002"),
        open_item(14, -80, description="Payment"),
        # Two invoices of 30 and a payment that names neither.
        open_item(20, 30, invoice=1, account="b"),
        open_item(21, 30, invoice=2, account="b"),
        open_item(22, -30, description="Payment", account="b"),
        open_item(23, 99, invoice=3, account="b"),
    ]

    result = matching.suggest_matches(items)

    found = [(s["reason"], [i["EntryNumber"] for i in s["items"]]) for s in result["suggestions"]]
    assert found == [("account_settled", [1, 2]), ("amount", [14, 12]), ("reference", [13, 11])]
    [left] = result["unmatched"]
    assert left["EntryNumber"] == 22
    assert [c["EntryNumber"] for c in left["candidates"]] == [20, 21]


def test_a_payment_with_no_invoice_open_is_listed_as_unmatched():
    # More payments than invoices, and an account with only a payment: still payments.
    items = [open_item(1, 100), open_item(2, -40), open_item(3, -30), open_item(4, -8875, account="b")]

    result = matching.suggest_matches(items)

    assert result["suggestions"] == []
    assert [(item["EntryNumber"], item["candidates"]) for item in result["unmatched"]] == [(2, []), (3, []), (4, [])]


def test_a_payment_booked_against_an_invoice_is_a_reference():
    items = [open_item(1, 40, invoice=7), open_item(2, 40, invoice=8), {**open_item(3, -40), "InvoiceNumber": 8}]

    [suggestion] = matching.suggest_matches(items)["suggestions"]

    assert (suggestion["reason"], [i["EntryNumber"] for i in suggestion["items"]]) == ("reference", [3, 2])


def test_match_sets_xml_and_upload_messages():
    xml = matching.match_sets_xml(
        "1300", "16001092", [{"year": 2026, "period": 7, "journal": "20", "entry": 26200003, "amount_dc": -12.1}]
    )

    assert xml == (
        '<?xml version="1.0" encoding="utf-8"?><eExact><MatchSets><MatchSet><GLAccount code="1300" />'
        '<Account code="16001092" /><MatchLines><MatchLine finyear="2026" finperiod="7" journal="20" '
        'entry="26200003" amountdc="-12.10" /></MatchLines></MatchSet></MatchSets></eExact>'
    )
    answer = (
        '<eExact><Messages><Message type="0"><Description> Not balanced </Description></Message></Messages></eExact>'
    )
    assert matching.upload_messages(answer) == [{"type": 0, "description": "Not balanced"}]


def transaction_line(journal, entry, gl, amount, year=2026, period=10) -> dict:
    return {"FinancialYear": year, "FinancialPeriod": period, "JournalCode": journal, "EntryNumber": entry,
            "GLAccountCode": gl, "AccountCode": "16001092", "AmountDC": amount}  # fmt: skip


MATCH = {
    "account_id": ACCOUNT_ID,
    "items": [{"journal_code": "70", "entry_number": 26700001}, {"journal_code": "20", "entry_number": 26200003}],
}


async def test_match_open_items_uploads_one_balanced_match_set(server, exact):
    exact.handler = books = Books(
        {
            "TransactionLines": [
                transaction_line("70", 26700001, "1300", 121.0),
                # The invoice's revenue and VAT lines aren't on receivables, so they aren't matched.
                transaction_line("70", 26700001, "8000", -100.0),
                transaction_line("20", 26200003, "1300", -121.0, period=11),
            ],
            "xml_answer": '<eExact><Messages><Message type="2"><Description>Created</Description></Message>'
            "</Messages></eExact>",
        }
    )

    result = await call(server, "match_open_items", MATCH)

    assert result["matched"] is True
    assert result["lines"] == [
        {"year": 2026, "period": 10, "journal": "70", "entry": 26700001, "amount_dc": 121.0},
        {"year": 2026, "period": 11, "journal": "20", "entry": 26200003, "amount_dc": -121.0},
    ]
    [(topic, xml)] = books.posted
    assert topic == "MatchSets"
    assert '<GLAccount code="1300" /><Account code="16001092" />' in xml
    upload = next(r for r in exact.requests if r.method == "POST")
    assert upload.url.params["_Division_"] == "123"
    lines_request = next(r for r in exact.requests if r.url.path.endswith("TransactionLines"))
    assert lines_request.url.params["$filter"] == (
        f"Account eq guid'{ACCOUNT_ID}' and (EntryNumber eq 26200003 or EntryNumber eq 26700001)"
    )


async def test_match_open_items_refuses_a_set_that_does_not_settle(server, exact):
    exact.handler = books = Books(
        {
            "TransactionLines": [
                transaction_line("70", 26700001, "1300", 121.0),
                transaction_line("20", 26200003, "1300", -100.0),
            ]
        }
    )

    with pytest.raises(ToolError, match="leave 21.00 open"):
        await call(server, "match_open_items", MATCH)
    assert books.posted == []


async def test_match_open_items_names_an_item_with_nothing_open(server, exact):
    exact.handler = Books({"TransactionLines": [transaction_line("70", 26700001, "1300", 121.0)]})

    with pytest.raises(ToolError, match="nothing on receivables or payables from journal 20 entry 26200003"):
        await call(server, "match_open_items", MATCH)


async def test_match_open_items_refuses_an_item_that_is_no_longer_open(server, exact):
    lines = [transaction_line("70", 26700001, "1300", 121.0), transaction_line("20", 26200003, "1300", -121.0)]
    exact.handler = books = Books(
        {"TransactionLines": lines, "ReceivablesList": [{"JournalCode": "70", "EntryNumber": 26700001}]}
    )

    with pytest.raises(ToolError, match="already matched or paid: journal 20 entry 26200003"):
        await call(server, "match_open_items", {**MATCH, "dry_run": True})
    assert books.posted == []


async def test_exact_refusing_the_match_is_a_tool_error(server, exact):
    exact.handler = Books(
        {
            "TransactionLines": [
                transaction_line("70", 26700001, "1300", 121.0),
                transaction_line("20", 26200003, "1300", -121.0),
            ],
            "xml_answer": '<eExact><Messages><Message type="0"><Description>Entry is already matched</Description>'
            "</Message></Messages></eExact>",
        }
    )

    with pytest.raises(ToolError, match="didn't match the items: Entry is already matched"):
        await call(server, "match_open_items", MATCH)


async def test_bookkeeping_only_shows_when_named(monkeypatch):
    monkeypatch.setenv("EXACT_TOOLSETS", "bookkeeping")

    server = create_server()
    assert "bookkeeping toolset is on" in server.instructions
    async with Client(server) as mcp:
        assert {p.name for p in await mcp.list_prompts()} == {"book_purchase_invoices", "reconcile_open_items"}

    monkeypatch.setenv("EXACT_TOOLSETS", "all")
    server = create_server()
    assert "bookkeeping" not in server.instructions
    async with Client(server) as mcp:
        assert await mcp.list_prompts() == []
