# Exact Online MCP

An [MCP](https://modelcontextprotocol.io) server for [Exact Online](https://www.exact.com), built
to deploy on [foro.sh](https://foro.sh). It lets an agent work with customers and suppliers,
sales and purchase invoices, open receivables and payables, the general ledger and bank
transactions, profit and loss, revenue, VAT returns, sales orders and items.

It only reads, unless the `bookkeeping` toolset is switched on. That toolset lets the agent do the
bookkeeping: book purchase and sales invoices and journal entries, add suppliers and customers,
store invoice files, and match payments to the invoices they settle.

## Tools

| Tool | What it answers |
| --- | --- |
| `list_divisions` | Which administrations this connection can read, and the default one |
| `financial_overview` | Revenue, costs and result against last year, outstanding and overdue invoices, and aging |
| `search_accounts` / `get_account` | Customers and suppliers by name; one account's details |
| `open_receivables` / `open_payables` | Unpaid sales / purchase invoices, with totals per currency |
| `receivables_aging` / `payables_aging` | Outstanding amounts per customer / supplier in Exact's age buckets |
| `customer_payment_behaviour` | How fast each customer pays: days to pay, days late, and the share paid late |
| `search_sales_invoices` / `get_sales_invoice` | Invoices made in Exact's invoicing module, by customer, status and date; one invoice with its lines |
| `search_sales_entries` | Booked sales invoices and credit notes, including ones made outside Exact |
| `search_sales_orders` | Sales orders by customer, status and date (Wholesale & Distribution and Manufacturing only) |
| `search_purchase_invoices` / `get_purchase_invoice` | Booked purchase invoices by supplier and date; one invoice with the GL account of each line |
| `search_items` | Products and services by code or description |
| `search_gl_accounts` | The chart of accounts, by code or description |
| `search_transactions` | General ledger lines by GL account, customer or supplier, and date, with the total of every match |
| `search_bank_transactions` | Bank statement lines by description, customer or supplier, and date |
| `profit_and_loss` | P&L per ledger account over a range of periods |
| `balance_sheet` | Balance-sheet accounts at the end of a period of the current or previous financial year |
| `cash_position` | The booked balance of every cash, bank, credit card and payment provider account |
| `budget_vs_actual` | Budget against actual per P&L account over a range of periods |
| `revenue_by_period` | Revenue per period of one financial year |
| `financial_periods` | The start and end date of each financial period |
| `list_journals` | The journals entries are booked in, by type, with their bank or suspense account |
| `list_vat_codes` | VAT codes with their percentage, type and whether they're for sales or purchases |
| `tax_returns` | VAT and other tax returns, with their due date and status |

Every tool takes an optional `division`. Without it, the server uses `EXACT_DIVISION`, or else
the user's current division in Exact.

### Toolsets

The tools above are always on. These groups stay off until `EXACT_TOOLSETS` on the project's Env
tab names them, such as `orders,projects`, or `all`. `all` leaves out `bookkeeping`, the only
toolset that writes, so it has to be named: `all,bookkeeping`. Most need a module that only some
Exact packages have.

| Toolset | Tools | Needs |
| --- | --- | --- |
| `orders` | `get_sales_order`, `search_purchase_orders`, `stock_levels` | Wholesale & Distribution or Manufacturing; stock from Plus up |
| `projects` | `search_projects`, `project_hours`, `project_result`, `upcoming_project_billing` | The project module |
| `crm` | `search_quotations`, `search_opportunities`, `list_contacts` | |
| `subscriptions` | `subscription_mrr` | The subscription module |
| `documents` | `search_documents`, `get_document` | |
| `changes` | `recent_changes` | |
| `query` | `describe_resource`, `query` | |
| `bookkeeping` | `unbooked_documents`, `suggest_booking`, `book_purchase_invoice`, `book_sales_invoice`, `book_journal_entry`, `create_account`, `upload_document`, `reconciliation_suggestions`, `match_open_items` | An Exact user allowed to book entries and add accounts and documents (see below) |

`list_contacts` only reads a contact's name, job title and business email and phone.
`project_hours` adds hours up per project and hour type, not per person.

`query` reads any other Exact resource with OData's `$select`, `$filter` and `$orderby`, and
`describe_resource` lists a resource's fields. Neither reads payroll, HR, user or mailbox
resources, contact people (`list_contacts` gives their business details), or a field that
identifies a person, names a user or holds a file (see `exact_mcp/scope.py`).

### Bookkeeping

| Tool | What it does |
| --- | --- |
| `unbooked_documents` | Purchase (or sales) invoice documents in Exact that no entry is booked from yet: the to-do list |
| `suggest_booking` | How a supplier's or customer's invoices were booked before: its defaults, and each GL account, VAT code and cost center on its recent invoices, most used first |
| `book_purchase_invoice` | Books a purchase invoice or credit note, linked to its document |
| `book_sales_invoice` | Books a sales invoice made outside Exact, such as in a webshop |
| `book_journal_entry` | Books a balanced general journal entry: accruals, depreciation, reclassifications, clearing a suspense account |
| `create_account` | Adds a supplier or customer |
| `upload_document` | Stores an invoice's file in Exact, to book with its `document_id` |
| `reconciliation_suggestions` | Sets of open receivables or payables that settle each other: an account that adds up to zero, a payment that names its invoices, or a payment with exactly one invoice of its amount |
| `match_open_items` | Matches such a set in Exact, so the invoice and its payment are no longer open |

It also adds two prompts that walk an agent through the usual work: `book_purchase_invoices`
and `reconcile_open_items`. The first reads each invoice's file with `get_document`, so switch on
`documents,bookkeeping` together.

What keeps it from booking something wrong:

- Every write tool takes `dry_run`. It checks what the server can check and shows what would be
  booked, without booking it. Exact can still refuse a booking for what isn't checked, such as a
  closed period or an inactive currency.
- Everything is checked before anything is sent:
  - Every GL account must exist and not be blocked. An invoice line can't go on a receivables,
    payables, VAT, cash or bank account, and a journal entry line can't go on cash or bank.
  - Every VAT code must exist, not be blocked, and be for the right side (sales or purchases).
  - An invoice's supplier or customer must exist and be one in Exact.
  - Every cost center and cost unit must exist.
  - Amounts have at most two decimals.
  - A journal entry must balance.
  - Open items only match when they are still open and add up to zero.
  - A Dutch VAT number on a new account must pass the Belastingdienst's check.
- `expected_total` is the invoice's own total. The server works out each line's VAT from its VAT
  code, as Exact does, and refuses an invoice whose lines don't add up to it.
- An invoice that looks booked already is refused:
  - A purchase invoice when the supplier already has its invoice number.
  - A sales invoice when its invoice number or reference is already booked.
  - A journal entry when the journal already has its first line on that day.

  The same goes for a supplier or customer whose name, VAT number or chamber of commerce number
  Exact already has. `allow_duplicate` overrides this.
- If Exact doesn't answer a write in time, the server doesn't send it again, because Exact may
  have booked it. It says to look the booking up first.
- Write tools only add: an entry, an account, a document or a match. They never change or delete
  one. A wrong booking is corrected the usual way, with a reversal or a journal entry.

Matching goes through Exact's XML API (`docs/XMLUpload.aspx?Topic=MatchSets`), because the REST
API has no way to match. The Exact app's access token works for both. Entries booked through the
API aren't matched automatically, so `match_open_items` is the only way to match them without
Exact's own screens.

Exact has told API users that its XML APIs will be discontinued. In July 2026 it said MatchSets
isn't being phased out yet and has no end date, but that it can't guarantee MatchSets keeps
working, and no REST replacement is planned yet
([picqer/exact-php-client#127](https://github.com/picqer/exact-php-client/issues/127)). Watch
Exact's breaking-changes page: if MatchSets stops working, `match_open_items` stops with it,
while every other bookkeeping tool keeps working.

## How the connection works

Exact Online uses OAuth 2 with a 10-minute access token, and it issues a new refresh token on
every refresh. The old one stops working. The server never holds the refresh token. It lives
in an upstream grant on the foro project:

- foro stores the grant encrypted, and refreshes it under a row lock, so two replicas, or a
  redeploy overlapping the old container, never spend the same refresh token twice.
- The server asks foro's upstream broker for an access token (`FORO_UPSTREAM_BROKER_URL`,
  which foro injects into every container) and caches it until it expires.
- If a refresh is ever lost, foro marks the grant dead on the project's tabs, and every tool
  says to reconnect.

You connect it once from the foro dashboard, and from then on foro refreshes it.

## Setup

**1. Register an Exact app.** In the [Exact App Center](https://apps.exactonline.com), under
*Manage* → *Apps*, register an app for your region with redirect URI
`https://foro.sh/api/upstream-grants/callback`. Note the client ID and secret. An app
registered here has no rights of its own: its token can do whatever the Exact user who connects
it in step 3 may do. For the `bookkeeping` toolset, that user's roles in Exact must allow booking
entries and adding accounts and documents in the division.

**2. Add the secrets on foro.** In the project's Env tab:

| Name | Value |
| --- | --- |
| `EXACT_CLIENT_SECRET` | From step 1, saved as a secret. foro sends it when it refreshes |
| `EXACT_REGION` | `nl` (default), `be`, `de`, `uk`, `fr`, `es` or `us` |
| `EXACT_DIVISION` | Optional: the default division number |

**3. Connect.** Deploy, then under *Upstream grants* on the same Env tab, click *Connect* on the
`exact` row. The server declares the grant in its `pyproject.toml`, so the endpoints are filled
in; add your client ID from step 1. Outside the Netherlands, change `start.exactonline.nl` in
both URLs to your region's host. Sign in to Exact and approve access, and you're back on the
Env tab. Redeploy if you changed a secret in step 2.

Until the grant exists, the server still deploys, and every tool says how to connect it.

## Limits

- Exact allows about 60 calls a minute and 5,000 a day per company.
  - The server waits out the per-minute limit inside a call.
  - When the daily limit is hit, it says when the limit resets.
- Most endpoints return at most 60 rows per request, so a large `limit` costs several calls.
  - `search_gl_accounts` and `search_transactions` use Exact's bulk endpoints, which return
    1,000 rows a call. They read every match, up to 10,000 transaction lines, so their totals
    are complete.
  - Given a GL account code, `search_transactions` first reads the chart of accounts to find it.
- `profit_and_loss`, `balance_sheet`, `cash_position` and `budget_vs_actual` read
  `ReportingBalance`, which has no bulk endpoint, so a large administration takes several calls.
- `get_sales_invoice` and `get_purchase_invoice` make two calls, one of them for the lines.
- `financial_overview` makes three calls.
- `project_hours` and `subscription_mrr` read endpoints without a bulk version, up to 2,000 rows
  each. `stock_levels` checks reorder points on up to 1,000 rows.
- `get_document` makes one call for the list of files and one per file. It returns at most
  10 MB of files, which go into the agent's context, and `upload_document` takes at most 10 MB.
- Booking an invoice makes about six calls: the supplier or customer, the journals, the chart of
  accounts, the VAT codes, the check for a double booking, and the booking itself, plus one for
  cost centers and one for cost units when lines name them. `match_open_items` makes four, and
  `upload_document` three: the document type, the document and its file.
  `reconciliation_suggestions` reads open items 60 a call.

## Development

```sh
uv sync
uv run pytest
uv run ruff format . && uv run ruff check .
```

## Status: not yet run against a live Exact company

The tests run against recorded API shapes, not a real administration.

Checked against [Exact's REST reference](https://start.exactonline.nl/docs/HlpRestAPIResources.aspx):

- Every endpoint path and `$select` field in `exact_mcp/tools.py`, including the
  `AgingReceivablesList` age groups and `ReportingBalance` (`BalanceType` `W` = profit & loss).
- The sales invoice status codes (10 draft, 20 open, 50 processed) and the sales order status
  codes (12 open, 20 partial, 21 complete, 45 cancelled).
- The tax return type and status codes, which `read/financial/Returns` sends without
  descriptions.
- Bulk endpoints page 1,000 rows and need a `$select`. Bulk `GLAccounts` can't filter on
  `Description`, so `search_gl_accounts` matches in the server.
- `SalesInvoices` only holds invoices made in Exact's invoicing module, and `SalesEntries` and
  `PurchaseEntries` hold the booked invoices.
- The division `Status` (1 active, 0 inactive, 2 archived) and `BlockingStatus` codes.
- Opening balances only exist for the current and the previous financial year.
- Bulk `Cashflow/Receivables` can filter on `Status` but not on `IsFullyPaid`, so
  `customer_payment_behaviour` checks that in the server.
- The purchase order, quotation, opportunity, hour and invoice term status codes, and the entity
  types of `sync/Deleted`.
- For bookkeeping:
  - The fields `book_*` sends, and which are mandatory: `Journal`, the party and the lines for an
    invoice; `JournalCode` and the lines for a journal entry; `AmountFC`, `GLAccount` and
    `EntryID` (filled by Exact) on a line.
  - A line without a `VATCode` gets its GL account's default. That's why invoice lines always
    send one, and why `book_journal_entry` refuses a VAT journal with an account that has a VAT
    code.
  - A `Charged` VAT code is a reverse-charge (verlegd) or EU transaction, which puts no VAT on
    the invoice.
  - The entry types: 30 and 31 a purchase invoice and credit note, 20 and 21 sales.
  - `DocumentAttachments` takes `Document`, `FileName` and a Base64 `Attachment`.
  - `documents/DocumentTypes` says whether a document type can be created, which
    `upload_document` checks first.
  - `ReceivablesList` and `PayablesList` hold the entry's journal and entry number, and give a
    cashflow entry the invoice number of the invoice it was booked against.
  - The XML API answers an upload with a `Message` per record, of type 0 (error), 1 (warning),
    2 (success) or 3 (fatal).

Consistent across third-party sources, but not in Exact's own docs:

- The access token lasts 10 minutes, and Exact refuses a refresh before its last ~30 seconds.
  foro hands a token out as expired 30 seconds early, so its refreshes land right at the
  start of that window. Unverified: whether Exact answers a refresh that comes too early with
  `invalid_grant`, which makes foro mark the grant dead, or with an error foro retries.
- Every refresh rotates the refresh token, and the old one stops working.
- For bookkeeping:
  - The XML API takes the REST API's OAuth token as `Authorization: Bearer`, with `Topic` and
    `_Division_` in the query.
  - A POST takes dates as ISO 8601, not the `/Date(ms)/` form Exact answers with. The server
    sends midnight UTC (`2026-10-01T00:00:00Z`), as the Python and Go clients do.
  - VAT codes are sent padded to three characters (`2  `), as Exact holds them and the Python
    client sends them.
  - VAT percentages and discounts are fractions: Exact's own example sets a 5% discount as
    `0.05`, and clients multiply `VATPercentage` by 100.
  - A credit note's amounts are negative.
  - Document type 20 is a purchase invoice and 10 a sales invoice. This comes from a community
    example; Exact doesn't list them.
  - The MatchSets XML: a `MatchLine` per entry, with the entry's amount on the receivables or
    payables account. Two independent examples agree: Brixxs' upload, and a MatchSets download
    recorded in the Go XML client's source. Exact publishes its schema only to signed-in users.

Still to check on a test company:

- Whether a 429 carries `X-RateLimit-Minutely-Reset`, and in which unit.
  - Without it, the server waits out a full minute before retrying once.
  - It tells the per-minute and daily limits apart by `X-RateLimit-Remaining`.
- The sign convention of `ReportingBalance.Amount` and `TransactionLines.AmountDC` (assumed:
  debit is positive).
- The sign of bank statement lines (assumed, from another integration: money in is positive),
  and of the revenue, costs and result that `ProfitLossOverview` and `RevenueListByYear` report.
- That `RevenueListByYear` takes the year as a `year` query parameter, as Invantive documents it.
- The sign of `OpeningBalance.Amount` and `Budgets.AmountDC` (assumed: debit is positive). An
  opening balance's `BalanceSide` is the account's normal side, so it doesn't settle this.
- That `ReportingBalance` leaves out opening balances, as XLReporting says, and whether a
  division's first year also has them as type 10 entries. `balance_sheet` leaves type 10 out of
  the movements, so they aren't counted twice.
- `customer_payment_behaviour` counts credit notes like invoices: `cashflow/Receivables` has no
  type to tell them apart.
- That a subscription line's `AmountDC` is its amount per invoice period, as `subscription_mrr`
  assumes.
- That `DocumentAttachments` returns the Base64 `Attachment` in a list request, and that
  `FileSize` is in bytes.
- That `ItemWarehouses`, `Projects`, `TimeTransactions`, `Quotations`, `Opportunities`,
  `Subscriptions` and `Documents` filter on the fields the tools use. Their pages show no limits.
- That every service answers `$metadata`, which Exact's knowledge base documents and
  `describe_resource` needs, including `read/`, `bulk/` and `sync/` ones.
- Whether bulk endpoints take `$top` and `$orderby`. The server never sends them, and sorts
  transaction lines itself.
- Whether `$expand` of invoice lines works, as some third-party clients use it. It would make
  the `get_` invoice tools one call.
- What the bookkeeping toolset assumes beyond that, none of it tried on a live company yet:
  - That `VATCodes.Percentage` is a fraction like other percentages. The server reads a value
    above 1 as a percent anyway, since no VAT rate reaches 100%.
  - That Exact accepts `Type` on a new entry, which its docs list but don't mark as settable.
  - That `FinancialTransactionEntryID eq null` picks out documents no entry was booked from, and
    that booking an entry with a `Document` fills it.
  - That an unmatched payment shows in `ReceivablesList` and `PayablesList` with the opposite
    sign of the invoices.
- How long an unused refresh token lives. Reports say 30 days, which would mean a server
  idle for a month needs reconnecting.
