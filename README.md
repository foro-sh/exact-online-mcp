# Exact Online MCP

A read-only [MCP](https://modelcontextprotocol.io) server for [Exact Online](https://www.exact.com),
built to deploy on [foro.sh](https://foro.sh). It lets an agent work with customers and suppliers,
open receivables and payables, sales invoices and orders, items, and profit and loss, without
being able to change anything in the books.

## Tools

| Tool | What it answers |
| --- | --- |
| `list_divisions` | Which administrations this connection can read, and the default one |
| `search_accounts` / `get_account` | Customers and suppliers by name; one account's details |
| `open_receivables` / `open_payables` | Unpaid sales / purchase invoices, with totals per currency |
| `receivables_aging` | Outstanding receivables per customer in Exact's age buckets |
| `search_sales_invoices` / `get_sales_invoice` | Sales invoices by customer, status and date; one invoice with its lines |
| `search_sales_orders` | Sales orders by customer, status and date |
| `search_items` | Products and services by code or description |
| `profit_and_loss` | P&L per ledger account over a range of periods |

Every tool takes an optional `division`. Without it, the server uses `EXACT_DIVISION`, or else
the user's current division in Exact.

## How the connection works

Exact Online uses OAuth 2 with a 10-minute access token, and it issues a new refresh token on
every refresh. The old one stops working. A foro container has no disk that survives a
restart, and secrets are fixed at deploy time, so the server keeps its tokens in a Postgres
database you provide:

- The tokens live in one row (`exact_online_tokens`), encrypted with `TOKEN_STORE_KEY`
  (libsodium secretbox).
- A refresh happens inside a row lock, so two replicas, or a redeploy overlapping the old
  container, never spend the same refresh token twice.
- The access token is cached in memory, so the database is only touched about every 10 minutes.

A one-time `connect` command does the browser authorisation and puts the first tokens in that
row. From then on the deployed server refreshes them itself.

## Setup

**1. Register an Exact app.** In the [Exact App Center](https://apps.exactonline.com), under
*Manage* → *Apps*, register an app for your region. Set a redirect URI; it only has to be a URL
you can copy from the address bar, such as `https://localhost/callback`. Note the client ID and
secret.

**2. Get a Postgres database.** Any Postgres 13+ works, and a free tier is enough. For EU data
residency, pick an EU region. Use a database (or role) dedicated to this server.

**3. Generate the encryption key.**

```sh
uv sync
uv run python -m exact_mcp.connect keygen
```

**4. Authorise once, locally.**

```sh
export EXACT_CLIENT_ID=... EXACT_CLIENT_SECRET=... EXACT_REGION=nl
export EXACT_REDIRECT_URI=https://localhost/callback
export TOKEN_STORE_URL=postgresql://... TOKEN_STORE_KEY=...
uv run python -m exact_mcp.connect
```

Open the printed URL, approve access, and paste the address Exact redirects to.

**5. Deploy on foro.** Add these as secrets in the project's dashboard, then redeploy:

| Secret | Value |
| --- | --- |
| `EXACT_CLIENT_ID`, `EXACT_CLIENT_SECRET` | From step 1 |
| `EXACT_REGION` | `nl` (default), `be`, `de`, `uk`, `fr`, `es` or `us` |
| `EXACT_DIVISION` | Optional: the default division number |
| `TOKEN_STORE_URL` | The Postgres connection string from step 2 |
| `TOKEN_STORE_KEY` | The key from step 3 |

Until these are set, the server still deploys, and every tool answers with the list of
missing secrets.

## Limits

- Exact allows about 60 calls a minute and 5,000 a day per company.
  - The server waits out the per-minute limit inside a call.
  - When the daily limit is hit, it says when the limit resets.
- Most endpoints return at most 60 rows per request, so a large `limit` costs several calls.
- Exact has no `$expand`, so `get_sales_invoice` makes two calls.

## Development

```sh
uv sync
uv run pytest                     # Postgres store tests skip without TEST_DATABASE_URL
TEST_DATABASE_URL=postgresql://... uv run pytest
uv run ruff format . && uv run ruff check .
```

## Status: not yet run against a live Exact company

The tests run against recorded API shapes, not a real administration.

Checked against [Exact's REST reference](https://start.exactonline.nl/docs/HlpRestAPIResources.aspx):

- Every endpoint path and `$select` field in `exact_mcp/tools.py`, including the
  `AgingReceivablesList` age groups and `ReportingBalance` (`BalanceType` `W` = profit & loss).
- The sales invoice status codes (10 draft, 20 open, 50 processed) and the sales order status
  codes (12 open, 20 partial, 21 complete, 45 cancelled).

Consistent across third-party sources, but not in Exact's own docs:

- The access token lasts 10 minutes, and Exact refuses a refresh before its last ~30 seconds.
- Every refresh rotates the refresh token, and the old one stops working.

Still to check on a test company:

- Whether a 429 carries `X-RateLimit-Minutely-Reset`, and in which unit.
  - Without it, the server waits out a full minute before retrying once.
  - It tells the per-minute and daily limits apart by `X-RateLimit-Remaining`.
- The sign convention of `ReportingBalance.Amount` (assumed: debit is positive).
- How long an unused refresh token lives. Reports say 30 days, which would mean a server
  idle for a month needs reconnecting.
