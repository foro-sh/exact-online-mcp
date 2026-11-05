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
`https://foro.sh/api/upstream-grants/callback`. Note the client ID and secret.

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
- Exact has no `$expand`, so `get_sales_invoice` makes two calls.

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
- The sign convention of `ReportingBalance.Amount` (assumed: debit is positive).
- How long an unused refresh token lives. Reports say 30 days, which would mean a server
  idle for a month needs reconnecting.
