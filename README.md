# Exact Online MCP

A read-only [MCP](https://modelcontextprotocol.io) server for [Exact Online](https://www.exact.com),
built to deploy on [foro.sh](https://foro.sh). It lets an agent work with customers and suppliers,
sales and purchase invoices, open receivables and payables, the general ledger and bank
transactions, profit and loss, revenue, VAT returns, sales orders and items, without being able
to change anything in the books.

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
| `tax_returns` | VAT and other tax returns, with their due date and status |

Every tool takes an optional `division`. Without it, the server uses `EXACT_DIVISION`, or else
the user's current division in Exact.

### Toolsets

The tools above are always on. These groups stay off until `EXACT_TOOLSETS` on the project's Env
tab names them, such as `orders,projects`, or `all`. Most need a module that only some Exact
packages have.

| Toolset | Tools | Needs |
| --- | --- | --- |
| `orders` | `get_sales_order`, `search_purchase_orders`, `stock_levels` | Wholesale & Distribution or Manufacturing; stock from Plus up |
| `projects` | `search_projects`, `project_hours`, `project_result`, `upcoming_project_billing` | The project module |
| `crm` | `search_quotations`, `search_opportunities`, `list_contacts` | |
| `subscriptions` | `subscription_mrr` | The subscription module |
| `documents` | `search_documents`, `get_document` | |
| `changes` | `recent_changes` | |
| `query` | `describe_resource`, `query` | |

`list_contacts` only reads a contact's name, job title and business email and phone.
`project_hours` adds hours up per project and hour type, not per person.

`query` reads any other Exact resource with OData's `$select`, `$filter` and `$orderby`, and
`describe_resource` lists a resource's fields. Neither reads payroll, HR, user or mailbox
resources, or a field that identifies a person, names a user or holds a file (see
`exact_mcp/scope.py`).

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

A one-time `connect` command does the browser authorisation and prints the grant to add in the
dashboard. From then on foro refreshes it.

## Setup

**1. Register an Exact app.** In the [Exact App Center](https://apps.exactonline.com), under
*Manage* → *Apps*, register an app for your region. Set a redirect URI; it only has to be a URL
you can copy from the address bar, such as `https://localhost/callback`. Note the client ID and
secret.

**2. Add the secrets on foro.** In the project's Env tab:

| Name | Value |
| --- | --- |
| `EXACT_CLIENT_SECRET` | From step 1, saved as a secret. foro sends it when it refreshes |
| `EXACT_REGION` | `nl` (default), `be`, `de`, `uk`, `fr`, `es` or `us` |
| `EXACT_DIVISION` | Optional: the default division number |

**3. Authorise once, locally.**

```sh
uv sync
export EXACT_CLIENT_ID=... EXACT_CLIENT_SECRET=... EXACT_REGION=nl
export EXACT_REDIRECT_URI=https://localhost/callback
uv run python -m exact_mcp.connect
```

Open the printed URL, approve access, and paste the address Exact redirects to. The command
then waits about 10 minutes, until the first access token is nearly expired, because Exact
refuses an earlier refresh. Leave it running.

**4. Add the grant.** Under *Upstream grants* on the same Env tab, add the grant `connect`
printed: name `exact`, the token endpoint, the client ID, `EXACT_CLIENT_SECRET` as the client
secret, and the refresh token. Redeploy if you changed a secret in step 2.

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
- `get_document` returns at most 10 MB of files, which go into the agent's context.

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

Consistent across third-party sources, but not in Exact's own docs:

- The access token lasts 10 minutes, and Exact refuses a refresh before its last ~30 seconds.
  foro hands a token out as expired 30 seconds early, so its refreshes land right at the
  start of that window. Unverified: whether Exact answers a refresh that comes too early with
  `invalid_grant`, which makes foro mark the grant dead, or with an error foro retries.
- Every refresh rotates the refresh token, and the old one stops working.

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
- How long an unused refresh token lives. Reports say 30 days, which would mean a server
  idle for a month needs reconnecting.
